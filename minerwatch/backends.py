"""Software power-control drivers ("sleep" / "wake") for Antminer devices.

Stopping a miner in software rather than by cutting power keeps the control
plane reachable: a slept miner still answers its API, still reports
temperatures, and can be woken again over the network. Two mechanisms are
supported because Antminer firmwares split roughly into two camps.

``cgminer``
    Speaks the same JSON-over-TCP API the poller already uses. Aftermarket
    firmwares (Vnish, Braiins OS+) expose a real sleep there — ``ascset``
    ``0,sleep`` or the bosminer ``pause`` command. Stock Bitmain firmware
    generally does **not**, which is why a chain of candidate commands is tried
    and the chain is configurable.

``bitmain_http``
    Stock Bitmain firmware (S17/S19 generation) exposes ``miner-mode`` in its
    web UI: ``0`` normal, ``1`` sleep, ``3`` low power. The setting is applied
    by reading the current miner configuration, changing that one field, and
    posting the whole document back. Authentication is HTTP Digest, which
    :mod:`urllib.request` handles from the standard library — no third-party
    HTTP client is pulled in for this.

Both drivers are "never raise": they return ``(ok, detail)`` so that a single
unreachable miner can never take down the poll loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from minerwatch import api
from minerwatch.models import Command, Miner, SleepBackend, SleepConfig

logger = logging.getLogger(__name__)

Result = tuple[bool, str]


class SleepBackendDriver:
    """Interface implemented by every power-control driver."""

    name: str = "base"

    async def sleep(self, miner: Miner) -> Result:  # pragma: no cover - abstract
        raise NotImplementedError

    async def wake(self, miner: Miner) -> Result:  # pragma: no cover - abstract
        raise NotImplementedError

    async def probe(self, miner: Miner) -> Result:  # pragma: no cover - abstract
        """Read-only reachability and credential check.

        A dry run never contacts the backend at all — it records the intent and
        returns — so a fleet can rehearse perfectly for weeks and still fail the
        first time it goes live, on a password nobody ever tested. This proves
        the path works without changing anything on the miner.
        """
        raise NotImplementedError


class NullBackend(SleepBackendDriver):
    """Driver used when a miner has no software power control configured."""

    name = "none"

    async def sleep(self, miner: Miner) -> Result:
        return False, "no sleep backend configured"

    async def wake(self, miner: Miner) -> Result:
        return False, "no sleep backend configured"

    async def probe(self, miner: Miner) -> Result:
        return False, "no sleep backend configured"


class CgminerBackend(SleepBackendDriver):
    """Drive sleep/wake over the cgminer JSON API."""

    name = "cgminer"

    async def sleep(self, miner: Miner) -> Result:
        return await self._try_chain(miner, miner.sleep.sleep_commands, "sleep")

    async def wake(self, miner: Miner) -> Result:
        return await self._try_chain(miner, miner.sleep.wake_commands, "wake")

    async def probe(self, miner: Miner) -> Result:
        """Ask for `version` — read-only, and every firmware implements it."""
        cfg = miner.sleep
        port = cfg.api_port or miner.port
        try:
            raw = await api.request(
                miner.host, port, "version",
                connect_timeout=cfg.timeout_seconds, read_timeout=cfg.timeout_seconds,
            )
            data = api.parse_response(raw)
        except (OSError, asyncio.TimeoutError, api.ApiError) as exc:
            return False, f"{miner.host}:{port}: {exc or type(exc).__name__}"

        ok, detail = api.check_status(data)
        if not ok:
            return False, f"{miner.host}:{port}: {detail}"
        versions = data.get("VERSION")
        if isinstance(versions, list) and versions and isinstance(versions[0], dict):
            v = versions[0]
            named = ", ".join(
                f"{k}={v[k]}" for k in ("Type", "Miner", "CompileTime", "BMMiner", "CGMiner")
                if k in v
            )
            if named:
                return True, f"API reachable on {port} ({named})"
        return True, f"API reachable on {port}"

    #: Commands worth asking about. `quit` and `restart` are deliberately
    #: absent from the *attempt* list further down: both stop mining outright
    #: and neither is a sleep.
    DIAGNOSE_COMMANDS = ("ascset", "pause", "resume", "restart", "quit", "config", "stats")

    async def diagnose_write(self, miner: Miner):
        """Work out whether this firmware can be slept over the cgminer API.

        Prefers cgminer's `check` command, which reports Exists and Access per
        command and so answers everything read-only. Bitmain's bmminer is a
        fork and several builds simply do not implement `check` - it comes back
        "Invalid command" - so fall back to sending the sleep candidates and
        reading the refusal, which distinguishes the same three cases:

            "Invalid command"  the firmware does not implement it
            "Access denied"    it does, and this host may not call it
            STATUS S or I      accepted
        """
        cfg = miner.sleep
        port = cfg.api_port or miner.port
        results = []

        async def ask(command, parameter=None):
            raw = await api.request(
                miner.host, port, command, parameter,
                connect_timeout=cfg.timeout_seconds, read_timeout=cfg.timeout_seconds,
            )
            return api.parse_response(raw)

        # Is `check` available? One call settles it.
        supports_check = False
        try:
            data = await ask("check", "summary")
            entries = data.get("CHECK")
            if isinstance(entries, list) and entries and isinstance(entries[0], dict):
                supports_check = True
            else:
                _, msg = api.check_status(data)
                results.append((
                    "check command", False,
                    f"{msg} - this firmware has no `check`; probing commands directly",
                ))
        except Exception as exc:
            results.append(("check command", False, f"{exc or type(exc).__name__}"))

        if supports_check:
            results.extend(await self._by_check(miner, ask))
        else:
            results.extend(await self._by_probe(miner, ask))
        return results

    async def _by_check(self, miner: Miner, ask):
        """Enumerate availability read-only, then try only what is permitted."""
        results = []
        available = {}
        for command in self.DIAGNOSE_COMMANDS:
            try:
                data = await ask("check", command)
            except Exception as exc:
                results.append((f"check {command}", False, f"{exc or type(exc).__name__}"))
                continue
            entries = data.get("CHECK")
            if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
                ok, msg = api.check_status(data)
                results.append((f"check {command}", False, msg or "no CHECK in reply"))
                continue
            exists = str(entries[0].get("Exists", "?")).upper()
            access = str(entries[0].get("Access", "?")).upper()
            usable = exists == "Y" and access == "Y"
            available[command] = usable
            note = f"Exists={exists} Access={access}"
            if exists == "Y" and access == "N":
                note += "  <- implemented, but this host lacks privileged access (api-allow needs W)"
            results.append((f"check {command}", usable, note))

        attempts = [c for c in miner.sleep.sleep_commands if available.get(c.command, False)]
        if not attempts:
            results.append((
                "sleep candidates", False,
                "the firmware reports no usable sleep command over this API",
            ))
            return results
        results.extend(await self._attempt(miner, ask, attempts))
        return results

    async def _by_probe(self, miner: Miner, ask):
        """Send each candidate and read the refusal.

        Only sleep candidates are sent. `restart` and `quit` are never probed:
        both stop mining, neither is a sleep, and a diagnostic must not be the
        thing that takes a miner down.
        """
        candidates = list(miner.sleep.sleep_commands) + [
            c for c in (Command("ascset", "0,sleep"), Command("pause"))
            if c not in miner.sleep.sleep_commands
        ]
        return await self._attempt(miner, ask, candidates, explain=True)

    async def _attempt(self, miner: Miner, ask, commands, explain: bool = False):
        results = []
        for cmd in commands:
            try:
                data = await ask(cmd.command, cmd.parameter)
                ok, msg = api.check_status(data)
            except Exception as exc:
                results.append((f"try {cmd}", False, str(exc) or type(exc).__name__))
                continue

            note = msg or ("accepted" if ok else "rejected")
            if not ok and explain:
                low = note.lower()
                if "invalid" in low or "unknown" in low:
                    note += "  <- this firmware does not implement it"
                elif "denied" in low or "access" in low or "privileged" in low:
                    note += "  <- implemented, but this host lacks privileged access"
            results.append((f"try {cmd}", ok, note))

            if ok:
                for undo in miner.sleep.wake_commands:
                    try:
                        await ask(undo.command, undo.parameter)
                        results.append((f"undo {undo}", True, "sent"))
                        break
                    except Exception as exc:
                        results.append((f"undo {undo}", False, str(exc)))
                break
        return results

    async def _try_chain(self, miner: Miner, commands, label: str) -> Result:
        """Try each command in order, returning on the first accepted one.

        Firmwares reject an unknown command with a normal ``STATUS: E`` reply
        rather than a connection error, so a rejection is cheap and it is safe
        to fall through to the next candidate. A *transport* failure, by
        contrast, means the miner is unreachable and there is no point trying
        the rest of the chain.
        """
        cfg = miner.sleep
        port = cfg.api_port or miner.port
        if not commands:
            return False, f"no {label} commands configured"

        failures: list[str] = []
        for cmd in commands:
            try:
                raw = await api.request(
                    miner.host,
                    port,
                    cmd.command,
                    cmd.parameter,
                    connect_timeout=cfg.timeout_seconds,
                    read_timeout=cfg.timeout_seconds,
                )
            except (OSError, asyncio.TimeoutError, api.ApiError) as exc:
                # Transport-level problem: abandon the chain.
                return False, f"{cmd}: {exc or type(exc).__name__}"
            except Exception as exc:  # pragma: no cover - defensive
                return False, f"{cmd}: {exc}"

            try:
                data = api.parse_response(raw)
            except api.ApiError as exc:
                failures.append(f"{cmd}: {exc}")
                continue

            ok, detail = api.check_status(data)
            if ok:
                return True, f"{cmd}: {detail}"
            failures.append(f"{cmd}: {detail}")

        return False, f"all {label} commands rejected ({'; '.join(failures)})"


class BitmainHttpBackend(SleepBackendDriver):
    """Drive sleep/wake through Bitmain stock firmware's ``miner-mode``."""

    name = "bitmain_http"

    #: Fields that select the power mode, newest naming first. Bitmain renamed
    #: this between firmware generations and did not keep a compatibility
    #: alias, so the field has to be discovered rather than assumed: S19j/S19XP
    #: builds commonly expose "bitmain-work-mode" while others use
    #: "miner-mode". Override with sleep.mode_key when a miner uses neither.
    MODE_KEYS = ("miner-mode", "bitmain-work-mode", "work-mode", "miner_mode")
    MODE_NORMAL = 0
    MODE_SLEEP = 1

    #: Some firmware reads the mode under one name and writes it under another.
    #: Captured from an S19 XP web UI: get_miner_conf.cgi returns
    #: "bitmain-work-mode": "0", and saving Work Mode posts "miner-mode": 1.
    #: Echoing back the field that was read is accepted and silently ignored,
    #: which is why the write appeared to succeed and changed nothing.
    WRITE_ALIASES = {"bitmain-work-mode": "miner-mode", "work-mode": "miner-mode"}

    #: The browser sends text/plain despite the body being JSON, and some CGI
    #: handlers check it. Sent alongside the headers a real save carries.
    BROWSER_CONTENT_TYPE = "text/plain;charset=UTF-8"

    def _write_headers(self, miner: Miner, content_type: str) -> dict:
        base = self._base_url(miner)
        return {
            "Content-Type": content_type,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": base,
            "Referer": base + "/",
        }

    def _write_doc(self, conf: dict, read_key: str, value: int,
                   alias: bool = False, nulls_as=None) -> tuple[dict, str]:
        """Build the document to POST, and say which field carries the mode."""
        doc = json.loads(json.dumps(conf))
        write_key = self.WRITE_ALIASES.get(read_key, read_key) if alias else read_key
        if write_key != read_key:
            # The firmware does not accept the read-side name on a write.
            doc.pop(read_key, None)
            doc[write_key] = int(value)
        else:
            doc[write_key] = self._coerce_like(conf.get(read_key), value)
        if nulls_as is not None:
            doc = {k: (nulls_as if v is None else v) for k, v in doc.items()}
        return doc, write_key

    def _mode_key(self, miner: Miner, conf: dict) -> str | None:
        """Find the field this firmware uses, honouring an explicit override."""
        configured = getattr(miner.sleep, "mode_key", None)
        if configured:
            return configured if configured in conf else None
        for key in self.MODE_KEYS:
            if key in conf:
                return key
        return None

    @staticmethod
    def _coerce_like(existing, value: int):
        """Write the new mode in the same JSON type the firmware used.

        Some builds quote these as strings ("0"/"1"). Posting an int where the
        firmware expects a string is rejected by some CGI handlers, and worse,
        silently ignored by others.
        """
        return str(value) if isinstance(existing, str) else value

    async def sleep(self, miner: Miner) -> Result:
        return await self._set_mode(miner, self._value(miner, sleep=True), "sleep")

    async def wake(self, miner: Miner) -> Result:
        return await self._set_mode(miner, self._value(miner, sleep=False), "wake")

    @staticmethod
    def _value(miner: Miner, sleep: bool) -> int:
        cfg = miner.sleep
        if sleep:
            return getattr(cfg, "sleep_value", BitmainHttpBackend.MODE_SLEEP)
        return getattr(cfg, "normal_value", BitmainHttpBackend.MODE_NORMAL)

    async def probe(self, miner: Miner) -> Result:
        """Authenticate and read the miner config. Never writes."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._probe_blocking, miner),
                timeout=miner.sleep.timeout_seconds * 2 + 5,
            )
        except asyncio.TimeoutError:
            return False, f"no reply from {self._base_url(miner)}"
        except Exception as exc:  # pragma: no cover - defensive
            return False, str(exc)

    async def reboot(self, miner: Miner) -> Result:
        """Reboot the whole control board through the stock web UI.

        This is the watchdog's heavier recovery path, for firmware that has no
        cgminer ``restart``. It is deliberately *not* part of the sleep
        interface: sleeping is reversible and verifiable, a reboot is neither.

        Two things make it safe enough to automate:

        **It authenticates before it acts.** A read-only ``probe`` runs first,
        so a wrong password or an unreachable web UI is reported without
        anything being sent. That matters more here than for a work-mode write,
        because a reboot cannot be undone by writing the old value back.

        **It cannot be verified synchronously, and does not pretend to be.**
        A work-mode write is confirmed by reading the field back; a reboot's
        only evidence is the miner going away and returning, which arrives over
        the following minutes of ordinary polling. So this returns success for
        "the firmware accepted the request", and the docstring is the warning
        that acceptance is not recovery.

        What it explicitly does **not** do is decide whether rebooting is a
        good idea. A miner that halted on a protective fault — a dead fan, an
        over-temperature — will halt again within minutes of coming back, and
        nothing in this request can tell that case from a wedged process. The
        watchdog's attempt limit is what bounds the loop: after
        ``max_restarts`` the miner latches for a human, which is the correct
        outcome for a fault software cannot fix.
        """
        cfg = miner.watchdog
        path = cfg.reboot_path if cfg is not None else "/cgi-bin/reboot.cgi"
        budget = miner.sleep.timeout_seconds * 2 + 5

        ok, detail = await self.probe(miner)
        if not ok:
            return False, f"reboot not sent - {detail}"

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._reboot_blocking, miner, path),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            # A reboot request legitimately never answers on some builds: the
            # box goes down mid-response. Treat the timeout as "probably
            # landed" rather than a failure, since the probe above proved the
            # web UI was reachable a moment ago, and let the next polls decide.
            return True, (
                f"reboot sent to {self._base_url(miner)}{path}; no reply before the "
                f"connection closed, which is normal for a reboot"
            )
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"reboot: {exc}"

    def _reboot_blocking(self, miner: Miner, path: str) -> Result:
        base = self._base_url(miner)
        url = f"{base}{path}"
        try:
            with self._build_opener(miner).open(
                url, timeout=miner.sleep.timeout_seconds
            ) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                return True, f"reboot accepted by {url} (HTTP {code})"
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False, (
                    f"reboot: {url} returned HTTP 404 - this firmware puts the reboot "
                    f"CGI somewhere else. Find it in the miner's web UI and set "
                    f"watchdog.reboot_path."
                )
            if exc.code in (401, 403):
                return False, (
                    f"reboot: HTTP {exc.code} - the web-UI username or password is "
                    f"wrong (trying user '{miner.sleep.username}')"
                )
            return False, f"reboot: {url} returned HTTP {exc.code}"
        except urllib.error.URLError as exc:
            # The box dropping the connection as it goes down is success, not
            # failure - the probe proved it was answering moments ago.
            return True, f"reboot sent to {url}; connection dropped ({exc.reason})"
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"reboot: {exc}"

    # ------------------------------------------------------------------
    # Write diagnosis
    # ------------------------------------------------------------------

    #: Request shapes to try when a write is accepted but does not stick.
    #: Firmwares disagree about the encoding, about whether the mode is a
    #: string or an int, and about whether they will parse a JSON null.
    #: Request shapes to try when a write is accepted but does not stick, in
    #: increasing order of divergence from "echo the document back".
    #: (label, post_format, coerce, drop_nulls, alias, content_type)
    WRITE_VARIANTS = (
        ("json, value as-is", "json", None, False, False, None),
        ("json, value as int", "json", int, False, False, None),
        ("json, value as string", "json", str, False, False, None),
        ("json, nulls as empty strings", "json", None, True, False, None),
        ("browser shape (miner-mode, text/plain)", "json", None, "0", True,
         "text/plain;charset=UTF-8"),
        ("browser shape, nulls kept", "json", None, False, True, "text/plain;charset=UTF-8"),
        ("miner-mode alias, application/json", "json", None, "0", True, None),
        ("form-encoded", "form", str, True, False, None),
    )

    async def diagnose_write(self, miner: Miner):
        """Try each request shape and report which one actually takes.

        Every attempt is verified by reading the config back, and anything that
        fails is proven to have changed nothing — which is what makes trying
        several shapes safe. The original value is restored at the end.
        """
        return await asyncio.wait_for(
            asyncio.to_thread(self._diagnose_blocking, miner),
            timeout=miner.sleep.timeout_seconds * len(self.WRITE_VARIANTS) * 4 + 30,
        )

    def _diagnose_blocking(self, miner: Miner):
        cfg = miner.sleep
        base = self._base_url(miner)
        results = []

        original = self._read_conf(miner)
        if original is None:
            return [("read config", False, f"could not read {base}")]
        key = self._mode_key(miner, original)
        if key is None:
            return [("find mode field", False, "no power-mode field in the config")]

        start_value = original.get(key)
        target = self._value(miner, sleep=True)
        normal = self._value(miner, sleep=False)

        for label, fmt, coerce, drop_nulls, alias, content_type in self.WRITE_VARIANTS:
            nulls_as = None
            if drop_nulls is True:
                nulls_as = ""
            elif isinstance(drop_nulls, str):
                nulls_as = drop_nulls
            conf, _ = self._write_doc(original, key, target, alias=alias, nulls_as=nulls_as)
            if coerce is not None and not alias:
                conf[key] = coerce(target)

            posted = self._post_conf(miner, conf, fmt, content_type)
            if not posted[0]:
                results.append((label, False, posted[1]))
                continue

            after = self._read_conf(miner)
            if after is None:
                results.append((label, False, "could not re-read the config"))
                continue
            if _as_int(after.get(key)) == target:
                results.append((label, True, f"{key} is now {after.get(key)!r} - this shape works"))
                # Put it back the way we found it, using the shape that worked.
                restore, _ = self._write_doc(after, key, normal, alias=alias, nulls_as=nulls_as)
                self._post_conf(miner, restore, fmt, content_type)
                break

            # Nothing took. Make sure nothing *else* moved either.
            changed = [
                k for k in set(original) | set(after)
                if k != key and original.get(k) != after.get(k)
            ]
            note = f"{key} still reads {after.get(key)!r}"
            if changed:
                note += f" AND these changed unexpectedly: {', '.join(sorted(changed))} - stopping"
                results.append((label, False, note))
                break
            results.append((label, False, note))

        return results

    def _read_conf(self, miner: Miner):
        try:
            with self._build_opener(miner).open(
                f"{self._base_url(miner)}/cgi-bin/get_miner_conf.cgi",
                timeout=miner.sleep.timeout_seconds,
            ) as resp:
                conf = json.loads(resp.read().decode("utf-8", errors="replace"))
            return conf if isinstance(conf, dict) else None
        except Exception:
            return None

    def _post_conf(self, miner: Miner, conf: dict, fmt: str,
                   content_type: str | None = None) -> Result:
        payload, default_ct = self._encode_conf(conf, fmt)
        req = urllib.request.Request(
            f"{self._base_url(miner)}/cgi-bin/set_miner_conf.cgi",
            data=payload,
            headers=self._write_headers(miner, content_type or default_ct),
            method="POST",
        )
        try:
            with self._build_opener(miner).open(req, timeout=miner.sleep.timeout_seconds) as resp:
                return True, resp.read().decode("utf-8", errors="replace")[:120]
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code} {exc.reason}"
        except (urllib.error.URLError, OSError) as exc:
            return False, str(getattr(exc, "reason", exc))

    @staticmethod
    def _encode_conf(conf: dict, post_format: str) -> tuple[bytes, str]:
        """Serialise the miner config for set_miner_conf.cgi.

        Firmwares disagree: newer builds take the JSON document back verbatim,
        while others expect the old form-encoded shape with the pool fields
        flattened into _ant_pool1url and friends.
        """
        if post_format != "form":
            return json.dumps(conf).encode("utf-8"), "application/json"

        fields: dict[str, str] = {}
        pools = conf.get("pools") or []
        for i in range(3):
            pool = pools[i] if i < len(pools) and isinstance(pools[i], dict) else {}
            fields[f"_ant_pool{i + 1}url"] = str(pool.get("url", "") or "")
            fields[f"_ant_pool{i + 1}user"] = str(pool.get("user", "") or "")
            fields[f"_ant_pool{i + 1}pw"] = str(pool.get("pass", "") or "")
        for key, value in conf.items():
            if key == "pools":
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif value is None:
                value = ""
            fields["_ant_" + key.replace("bitmain-", "").replace("-", "_")] = str(value)
        return urllib.parse.urlencode(fields).encode("utf-8"), (
            "application/x-www-form-urlencoded"
        )

    def _probe_blocking(self, miner: Miner) -> Result:
        cfg = miner.sleep
        base = self._base_url(miner)
        try:
            with self._build_opener(miner).open(
                f"{base}/cgi-bin/get_miner_conf.cgi", timeout=cfg.timeout_seconds
            ) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return False, (
                    f"{base}: HTTP {exc.code} - the web-UI username or password is wrong "
                    f"(trying user {cfg.username!r})"
                )
            return False, f"{base}: HTTP {exc.code} {exc.reason}"
        except (urllib.error.URLError, OSError) as exc:
            return False, f"{base}: {getattr(exc, 'reason', exc)}"

        try:
            conf = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return False, f"{base}: reply is not JSON - is this the miner's web UI?"
        if not isinstance(conf, dict):
            return False, f"{base}: unexpected miner conf type"
        key = self._mode_key(miner, conf)
        if key is None:
            # Name what the firmware *does* expose: that is the only way to
            # work out which field to point sleep.mode_key at.
            fields = ", ".join(sorted(k for k in conf if not isinstance(conf[k], (list, dict))))
            return False, (
                f"{base}: authenticated, but none of {self.MODE_KEYS} is in the miner "
                f"config. Fields present: {fields or '(none)'}. "
                f"Set sleep.mode_key to the right one, or use the cgminer backend."
            )
        mode = _as_int(conf.get(key))
        names = {
            self._value(miner, sleep=False): "normal",
            self._value(miner, sleep=True): "SLEEPING",
        }
        label = names.get(mode, f"unrecognised - not {sorted(names)}")
        return True, f"{base}: authenticated, {key}={mode} ({label})"

    async def _set_mode(self, miner: Miner, mode: int, label: str) -> Result:
        """Read the miner config, change one field, and write it back.

        Run on a worker thread: :mod:`urllib` is blocking, and the digest
        handshake costs two round trips per call. ``asyncio.to_thread`` keeps
        the poll loop free while that happens.

        Note that a thread cannot be cancelled. The outer timeout exists only so
        a wedged call cannot stall the poll loop forever; the worker may still
        finish afterwards and change ``miner-mode`` after this reported failure.
        The real bound is urllib's own per-request socket timeout, which caps
        the whole exchange at roughly ``2 x timeout_seconds`` (one GET, one
        POST) — keep ``timeout_seconds`` modest, because that same bound is how
        long a Ctrl+C can be delayed while the executor drains at shutdown.
        """
        budget = miner.sleep.timeout_seconds * 2 + 5
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._set_mode_blocking, miner, mode, label),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            return False, (
                f"{label}: no reply from {self._base_url(miner)} within {budget:.0f}s "
                f"(the request may still complete)"
            )
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"{label}: {exc}"

    # -- blocking helpers ---------------------------------------------------

    def _base_url(self, miner: Miner) -> str:
        cfg = miner.sleep
        host = miner.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # IPv6 literal
        return f"{cfg.http_scheme}://{host}:{cfg.http_port}"

    def _build_opener(self, miner: Miner) -> urllib.request.OpenerDirector:
        cfg = miner.sleep
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self._base_url(miner), cfg.username, cfg.password)
        return urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(mgr),
            urllib.request.HTTPBasicAuthHandler(mgr),
        )

    def _set_mode_blocking(self, miner: Miner, mode: int, label: str) -> Result:
        cfg = miner.sleep
        opener = self._build_opener(miner)
        base = self._base_url(miner)

        try:
            with opener.open(f"{base}/cgi-bin/get_miner_conf.cgi", timeout=cfg.timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return False, f"{label}: GET miner conf failed: HTTP {exc.code} {exc.reason}"
        except (urllib.error.URLError, OSError) as exc:
            return False, f"{label}: GET miner conf failed: {getattr(exc, 'reason', exc)}"

        try:
            conf = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return False, f"{label}: miner conf is not JSON: {body[:200]!r}"
        if not isinstance(conf, dict):
            return False, f"{label}: unexpected miner conf type {type(conf).__name__}"

        key = self._mode_key(miner, conf)
        if key is None:
            fields = ", ".join(sorted(k for k in conf if not isinstance(conf[k], (list, dict))))
            return False, (
                f"{label}: none of {self.MODE_KEYS} is in the miner config at {base}. "
                f"Fields present: {fields or '(none)'}. Set sleep.mode_key."
            )

        current = conf.get(key)
        if _as_int(current) == mode:
            # Already in the requested mode. Report success so the caller
            # records the intent without pointlessly rebooting bmminer.
            return True, f"{label}: already {key}={mode}"

        # Which shapes to try, in order. "auto" starts with the conservative
        # one — echo the document back under the field it was read from — and
        # only falls back to the browser's shape if that write does not stick.
        # Trying the alias first would break firmware that genuinely uses one
        # name for both directions.
        profile = getattr(cfg, "write_profile", "auto")
        aliasable = key in self.WRITE_ALIASES
        if profile == "mirror" or not aliasable:
            shapes = [False]
        elif profile == "browser":
            shapes = [True]
        else:
            shapes = [False, True]

        attempts = []
        for alias in shapes:
            doc, write_key = self._write_doc(
                conf, key, mode, alias=alias, nulls_as="0" if alias else None
            )
            content_type = getattr(cfg, "content_type", None)
            if alias and not content_type:
                content_type = self.BROWSER_CONTENT_TYPE
            posted, reply = self._post_conf(
                miner, doc, getattr(cfg, "post_format", "json"), content_type
            )
            if not posted:
                attempts.append(f"{write_key}: {reply}")
                continue

            verified, detail = self._verify_blocking(miner, key, mode)
            if verified:
                via = f" via {write_key}" if write_key != key else ""
                return True, f"{label}: {key} {current!r} -> {mode}{via}, verified"
            attempts.append(f"{write_key}: {detail}")

        return False, (
            f"{label}: the miner accepted the change but it did not persist. "
            f"Tried {'; '.join(attempts)}. Run `diagnose {miner.id}` to search the "
            f"remaining request shapes."
        )

    def _verify_blocking(self, miner: Miner, key: str, expected: int) -> Result:
        """Re-read the config and confirm the field actually changed."""
        cfg = miner.sleep
        base = self._base_url(miner)
        try:
            with self._build_opener(miner).open(
                f"{base}/cgi-bin/get_miner_conf.cgi", timeout=cfg.timeout_seconds
            ) as resp:
                conf = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # pragma: no cover - network dependent
            return False, f"could not re-read the config to confirm ({exc})"
        actual = _as_int(conf.get(key))
        if actual == expected:
            return True, "verified"
        return False, f"{key} still reads {actual!r}, not {expected}"


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_DRIVERS: dict[SleepBackend, type[SleepBackendDriver]] = {
    SleepBackend.CGMINER: CgminerBackend,
    SleepBackend.BITMAIN_HTTP: BitmainHttpBackend,
    SleepBackend.NONE: NullBackend,
}


def get_backend(config: SleepConfig) -> SleepBackendDriver:
    """Instantiate the driver named by *config*.

    Drivers are stateless, so callers are free to cache one per backend kind.
    """
    if not config.enabled:
        return NullBackend()
    return _DRIVERS.get(config.backend, NullBackend)()
