"""Web dashboard: the `status` and `history` views in a browser, plus alerts.

Nothing here can change a miner or the events database. Every request opens
its own SQLite connection in read-only mode (``mode=ro``), so a bug in this
module can at worst show the wrong thing, never send a command or write a row.
That is also why there are no miner controls: the CLI cannot change the
running supervisor's mind (its latches are in-memory, hydrated once at
startup), and a web control that wrote to the events table would have exactly
the same blind spot while looking far more authoritative.

The page can write two files: the email alert settings (see
:mod:`minerwatch.alerts`) and ``miners.yaml`` itself (see
:mod:`minerwatch.config_editor`). Both are accepted only from this PC unless
``--allow-remote-settings`` is given, and only with a custom header that a
cross-site form cannot send, so another web page open in the same browser
cannot quietly redirect the alerts or rewrite the fleet. The config is also
*read* only under those rules, because it holds the miners' web passwords.

The standard library is enough for a page that one operator refreshes every
few seconds, and it keeps the Windows install to the one ``pip install`` that
already works there.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from minerwatch import alerts as alerts_mod
from minerwatch import config_editor
from minerwatch.config import ConfigError
from minerwatch.compat import ALLOW_REUSE_ADDRESS
from minerwatch.models import Miner, State
from minerwatch.schedule import is_working_time
from minerwatch.sleeper import SLEEP_ACTIONS, UNCERTAIN_ACTIONS, WAKE_ACTIONS

logger = logging.getLogger("minerwatch")

#: Poller actions: the only rows that record an *observation* of the miner.
#: Mirrors ``cli.POLL_ACTIONS``; duplicated rather than imported so the CLI
#: can import this module lazily without a cycle.
POLL_ACTIONS = ("none", "alert", "expected_off")

#: Most hashrate points sent for one chart. A day of 15s polls is ~5800 rows;
#: a sparkline a few hundred pixels wide cannot show more than this anyway.
MAX_CHART_POINTS = 360


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open the events database so that writing is impossible, not just avoided.

    ``Path.as_uri()`` produces ``file:///D:/...`` on Windows, which SQLite's
    URI parser accepts; a hand-built ``file:D:\\...`` string does not survive
    the backslashes.
    """
    if db_path.startswith(":"):
        raise ValueError("the dashboard needs a database file, not an in-memory one")
    uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30.0, check_same_thread=False)


class FleetState:
    """What the dashboard needs from the events table, kept up to date incrementally.

    The events table has no index on ``action``, so every "latest row with
    action X" question is a scan of that miner's whole history. ``status``
    asks a few dozen of them and takes ~10s on six weeks of a 12-miner fleet;
    the first version of this page asked them on every refresh, from every
    open tab and the alert monitor at once, and on the real host never
    answered at all.

    Instead: one pass over the rare non-poll rows at startup, then on each
    refresh only the rows added since (``id`` is the table's integer primary
    key, so ``id > ?`` is a range read however large the table grows). The
    supervisor's database is never written - adding the missing index would be
    the obvious fix, but this process opens the file read-only on purpose.
    """

    def __init__(self, miners: dict[str, Miner]):
        self.miners = miners
        self._lock = threading.RLock()
        self.last_id: int | None = None
        self.poll: dict[str, tuple] = {}        # newest poll row
        self.latest: dict[str, tuple] = {}      # newest row of any kind
        self.decision: dict[str, tuple] = {}    # newest non-poll row
        self.power: dict[str, str] = {}         # asleep / unsure / awake
        self.watchdog_latch: dict[str, str | None] = {}  # ts latched, or None
        self.sleep_latch: dict[str, str | None] = {}

    # Row layout everywhere below: (id, ts, miner, state, action, reason, ghs)
    _COLUMNS = "id, ts, miner, state, action, reason, ghs"

    def replace_miners(self, miners: dict[str, Miner]) -> None:
        """Switch to a new miner list (after the config editor saved one).

        The dict is shared with the request handlers and the alert monitor,
        so it is changed in place, and the next refresh re-reads the history
        so a newly added miner shows its past polls.
        """
        with self._lock:
            self.miners.clear()
            self.miners.update(miners)
            self.last_id = None

    def refresh(self, conn) -> None:
        with self._lock:
            top = conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
            if self.last_id is None or top < self.last_id:
                self._load(conn, top)  # first call, or the file was replaced
            elif top > self.last_id:
                rows = conn.execute(
                    f"SELECT {self._COLUMNS} FROM events WHERE id > ? AND id <= ? ORDER BY id",
                    (self.last_id, top),
                ).fetchall()
                for row in rows:
                    self._apply(row)
            self.last_id = top

    def _load(self, conn, top: int) -> None:
        for name in ("poll", "latest", "decision", "power", "watchdog_latch", "sleep_latch"):
            getattr(self, name).clear()
        placeholders = ",".join("?" for _ in POLL_ACTIONS)
        # The one full scan: every decision ever made, oldest first. These are a
        # tiny fraction of the table (polls are the other 99%).
        for row in conn.execute(
            f"SELECT {self._COLUMNS} FROM events WHERE id <= ? AND "
            f"(action IS NULL OR action NOT IN ({placeholders})) ORDER BY id",
            (top, *POLL_ACTIONS),
        ):
            self._apply(row)
        # The newest poll is near the end of each miner's history, so these
        # walk the (miner, ts) index backwards and stop almost at once.
        for miner_id in self.miners:
            row = conn.execute(
                f"SELECT {self._COLUMNS} FROM events WHERE miner = ? AND id <= ? "
                f"AND action IN ({placeholders}) ORDER BY ts DESC, id DESC LIMIT 1",
                (miner_id, top, *POLL_ACTIONS),
            ).fetchone()
            if row is not None:
                self._apply(row)

    def _apply(self, row: tuple) -> None:
        _, ts, miner, state, action, _, _ = row
        if miner not in self.latest or ts >= self.latest[miner][1]:
            self.latest[miner] = row
        if action in POLL_ACTIONS:
            if miner not in self.poll or ts >= self.poll[miner][1]:
                self.poll[miner] = row
            return
        if action is None:
            return
        self.decision[miner] = row
        # Same rules the controllers hydrate by: the most recent of each family wins.
        if action in SLEEP_ACTIONS:
            self.power[miner] = "asleep"
        elif action in UNCERTAIN_ACTIONS:
            self.power[miner] = "unsure"
        elif action in WAKE_ACTIONS:
            self.power[miner] = "awake"
        if action == "needs_attention":
            self.watchdog_latch[miner] = ts
        elif action == "attention_cleared":
            self.watchdog_latch[miner] = None
        if action == "sleep_needs_attention":
            self.sleep_latch[miner] = ts
        elif action in ("sleep_attention_cleared",):
            self.sleep_latch[miner] = None


def snapshot(conn, miners: dict[str, Miner], poll_interval: int,
             now: datetime | None = None, fleet: FleetState | None = None) -> dict:
    """Everything the dashboard's main table shows, as plain JSON-able data.

    Follows ``cmd_status``: the state and hashrate come from the newest *poll*
    row, and the latches and power mode from the same "most recent of each
    family" rules the controllers hydrate from.
    """
    from minerwatch.cli import ACTION_MEANING, _diagnose

    now = now or datetime.now(timezone.utc)
    if fleet is None:
        fleet = FleetState(miners)
    with fleet._lock:  # the alert monitor refreshes the same state from its own thread
        fleet.refresh(conn)
        return _build_snapshot(fleet, miners, poll_interval, now)


def _build_snapshot(fleet: FleetState, miners: dict[str, Miner], poll_interval: int,
                    now: datetime) -> dict:
    from minerwatch.cli import ACTION_MEANING, _diagnose

    rows = []
    newest_poll: datetime | None = None
    for miner in miners.values():
        poll = fleet.poll.get(miner.id)
        last = poll or fleet.latest.get(miner.id)
        decision = fleet.decision.get(miner.id)
        power = "manual" if not miner.sleep.enabled else fleet.power.get(miner.id, "awake")
        latches, since = [], []
        if fleet.watchdog_latch.get(miner.id):
            latches.append("watchdog")
            since.append(fleet.watchdog_latch[miner.id])
        if fleet.sleep_latch.get(miner.id):
            latches.append("sleep")
            since.append(fleet.sleep_latch[miner.id])

        seen = _parse(last[1]) if last else None
        if poll is not None:
            polled = _parse(poll[1])
            if polled and (newest_poll is None or polled > newest_poll):
                newest_poll = polled

        reason = poll[5] if poll is not None and poll[3] != State.MINING.value else None
        rows.append({
            "id": miner.id,
            "group": miner.group,
            "host": miner.host,
            "state": last[3] if last else "unknown",
            "ghs": poll[6] if poll is not None else None,
            "window": "open" if is_working_time(miner, now) else "closed",
            "power": power,
            "latches": latches,
            "latched_since": max(since) if since else None,
            "last_seen": seen.isoformat() if seen else None,
            "reason": reason,
            "diagnosis": _diagnose(reason) if reason else None,
            "last_decision": decision and {
                "ts": decision[1], "action": decision[4],
                "meaning": decision[5] or ACTION_MEANING.get(decision[4], ""),
            },
        })

    # A latch is silent by design, and so is a supervisor that has stopped
    # polling: `status` happily prints the last rows it has. Both have cost
    # this fleet days of unnoticed downtime, so the page says so at the top
    # rather than leaving it to someone reading timestamps.
    stale_after = max(3 * poll_interval, 60)
    poll_age = (now - newest_poll).total_seconds() if newest_poll else None
    return {
        "now": now.isoformat(),
        "poll_interval": poll_interval,
        "newest_poll": newest_poll.isoformat() if newest_poll else None,
        "stale": poll_age is None or poll_age > stale_after,
        "poll_age_seconds": poll_age,
        "miners": rows,
    }


def history(conn, miner_id: str, hours: float, now: datetime | None = None) -> dict:
    """Hashrate series and the controllers' decisions for one miner."""
    from minerwatch.cli import ACTION_MEANING

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()
    placeholders = ",".join("?" for _ in POLL_ACTIONS)
    polls = conn.execute(
        f"SELECT ts, state, ghs FROM events WHERE miner = ? AND ts >= ? "
        f"AND action IN ({placeholders}) ORDER BY ts, id",
        (miner_id, since, *POLL_ACTIONS),
    ).fetchall()
    step = max(1, -(-len(polls) // MAX_CHART_POINTS))
    # Keep the minimum of each bucket, not every n-th row: a short drop to
    # zero is the thing the chart exists to show, and sampling would skip it.
    points = []
    for i in range(0, len(polls), step):
        bucket = polls[i:i + step]
        low = min(bucket, key=lambda r: r[2] if r[2] is not None else -1)
        points.append({"ts": bucket[0][0], "state": low[1], "ghs": low[2]})

    decisions = conn.execute(
        f"SELECT ts, state, action, reason FROM events WHERE miner = ? AND ts >= ? "
        f"AND action IS NOT NULL AND action NOT IN ({placeholders}) "
        f"ORDER BY ts DESC, id DESC LIMIT 200",
        (miner_id, since, *POLL_ACTIONS),
    ).fetchall()
    return {
        "miner": miner_id,
        "hours": hours,
        "points": points,
        "decisions": [
            {"ts": ts, "state": state, "action": action,
             "why": reason or ACTION_MEANING.get(action, "")}
            for ts, state, action, reason in decisions
        ],
    }


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

#: Largest settings body accepted. The form is a few hundred bytes.
MAX_BODY_BYTES = 64 * 1024

LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
#: Host headers a page served from this PC sends. Anything else, from a
#: loopback client, is a DNS-rebinding page and is refused the settings.
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "[::1]")


def make_server(host: str, port: int, db_path: str, miners: dict[str, Miner],
                poll_interval: int, alerts_path: str | None = None,
                monitor: "alerts_mod.AlertMonitor | None" = None,
                allow_remote_settings: bool = False,
                fleet: FleetState | None = None,
                config_path: str | None = None) -> ThreadingHTTPServer:
    fleet = fleet or FleetState(miners)

    class Handler(BaseHTTPRequestHandler):
        server_version = "MinerWatch"

        def do_GET(self):  # noqa: N802 - http.server's naming
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == "/":
                    return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                if url.path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if url.path == "/api/status":
                    with _closing(open_readonly(db_path)) as conn:
                        return self._json(200, snapshot(conn, miners, poll_interval, fleet=fleet))
                if url.path == "/api/history":
                    miner_id = (query.get("miner") or [""])[0]
                    if miner_id not in miners:
                        return self._json(404, {"error": f"unknown miner {miner_id!r}"})
                    try:
                        hours = min(max(float((query.get("hours") or ["24"])[0]), 0.25), 24 * 14)
                    except ValueError:
                        return self._json(400, {"error": "hours must be a number"})
                    with _closing(open_readonly(db_path)) as conn:
                        return self._json(200, history(conn, miner_id, hours))
                if url.path == "/api/alerts":
                    if alerts_path is None:
                        return self._json(200, {"available": False})
                    try:
                        settings = alerts_mod.load_settings(alerts_path)
                    except (ValueError, TypeError) as exc:
                        return self._json(500, {"error": f"{alerts_path} is not valid: {exc}"})
                    return self._json(200, {
                        "available": True,
                        "path": alerts_path,
                        "settings": settings.public(),
                        "status": monitor.status if monitor else None,
                        "can_edit": allow_remote_settings or self._is_local(),
                    })
                if url.path.startswith("/api/config"):
                    return self._config_get(url.path, query)
                return self._json(404, {"error": "not found"})
            except sqlite3.OperationalError as exc:
                # Usually "unable to open database file": the supervisor has not
                # created it yet, or the dashboard was pointed at the wrong config.
                return self._json(503, {"error": f"cannot read {db_path}: {exc}"})
            except (BrokenPipeError, ConnectionResetError):
                return None  # the browser went away mid-reply; nothing to tell it
            except Exception as exc:
                # Without this the server drops the connection and the page can
                # only say "Failed to fetch". Send the actual error to the page
                # and the full traceback to the console.
                logger.exception("web: %s failed", url.path)
                return self._json(500, {"error": f"{type(exc).__name__}: {exc} "
                                                 f"(full details in the console window)"})

        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/api/config/check", "/api/config/save"):
                try:
                    return self._config_post(url.path)
                except (BrokenPipeError, ConnectionResetError):
                    return None
                except Exception as exc:
                    logger.exception("web: %s failed", url.path)
                    return self._json(500, {"error": f"{type(exc).__name__}: {exc} "
                                                     f"(full details in the console window)"})
            if alerts_path is None or url.path not in ("/api/alerts", "/api/alerts/test"):
                return self._json(405, {"error": "the dashboard is read-only"})
            refusal = self._refuse_write()
            if refusal:
                return self._json(403, {"error": refusal})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    return self._json(413, {"error": "request too large"})
                update = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(update, dict):
                    raise ValueError("expected a JSON object")
                current = alerts_mod.load_settings(alerts_path)
                candidate = alerts_mod.merge_update(current, update)
            except (ValueError, TypeError, KeyError) as exc:
                return self._json(400, {"error": f"bad settings: {exc}"})

            if url.path == "/api/alerts/test":
                # Test exactly what is in the form, saved or not, so a typo is
                # found before it is relied on.
                problems = [p for p in alerts_mod.AlertSettings(
                    **{**candidate.__dict__, "enabled": True}).validate()]
                if problems:
                    return self._json(400, {"error": "; ".join(problems)})
                try:
                    alerts_mod.send_email(
                        candidate, "Test email",
                        "This is a test from the MinerWatch dashboard. If you can read "
                        "it, alert emails will reach you.\n")
                except Exception as exc:
                    return self._json(502, {"error": f"sending failed: {exc}"})
                return self._json(200, {"ok": True, "sent_to": candidate.to_addrs})

            problems = candidate.validate()
            if problems:
                return self._json(400, {"error": "; ".join(problems)})
            alerts_mod.save_settings(alerts_path, candidate)
            logger.info("alerts: settings saved to %s by %s", alerts_path, self.client_address[0])
            return self._json(200, {"ok": True, "settings": candidate.public()})

        def do_PUT(self):  # noqa: N802
            self._json(405, {"error": "the dashboard is read-only"})

        do_DELETE = do_PATCH = do_PUT

        def _is_local(self) -> bool:
            return self.client_address[0] in LOOPBACK

        def _refuse_write(self) -> str | None:
            refusal = self._refuse_remote()
            if refusal:
                return refusal
            # A cross-site <form> cannot set a custom header or a JSON content
            # type without a CORS preflight, which this server never approves.
            if self.headers.get("X-MinerWatch") != "1":
                return "missing X-MinerWatch header"
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return "expected application/json"
            return None

        def _refuse_remote(self) -> str | None:
            if allow_remote_settings:
                return None
            if not self._is_local():
                return ("settings can only be changed from the MinerWatch PC itself "
                        "(start the dashboard with --allow-remote-settings to change that)")
            host = (self.headers.get("Host") or "").lower()
            name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            if name not in LOOPBACK_NAMES:
                return f"open the dashboard as http://localhost to change settings (not {host})"
            return None

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > config_editor.MAX_CONFIG_BYTES + MAX_BODY_BYTES:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            return body

        # --- miners.yaml editor ---------------------------------------------

        def _config_get(self, path: str, query: dict):
            if config_path is None:
                return self._json(200, {"available": False})
            refusal = self._refuse_remote()
            if refusal:
                return self._json(403, {"available": True, "can_edit": False, "error": refusal})
            # Reads hand out passwords, so they need the header too: a page on
            # another site cannot add it without a preflight this server refuses.
            if self.headers.get("X-MinerWatch") != "1":
                return self._json(403, {"error": "missing X-MinerWatch header"})
            if path == "/api/config":
                cur = config_editor.current(config_path)
                result = config_editor.check(cur["text"], config_path)
                return self._json(200, {"available": True, "can_edit": True, **cur,
                                        "check": result})
            if path == "/api/config/backups":
                return self._json(200, {"backups": config_editor.list_backups(config_path)})
            if path == "/api/config/backup":
                name = (query.get("name") or [""])[0]
                try:
                    return self._json(200, {"name": name,
                                            "text": config_editor.read_backup(config_path, name)})
                except FileNotFoundError:
                    return self._json(404, {"error": f"no backup named {name!r}"})
            return self._json(404, {"error": "not found"})

        def _config_post(self, path: str):
            if config_path is None:
                return self._json(404, {"error": "the config editor is not available"})
            refusal = self._refuse_write()
            if refusal:
                return self._json(403, {"error": refusal})
            try:
                body = self._read_body()
                text = body["text"]
                if not isinstance(text, str):
                    raise ValueError("text must be a string")
            except (ValueError, KeyError, TypeError) as exc:
                return self._json(400, {"error": f"bad request: {exc}"})
            if path == "/api/config/check":
                return self._json(200, config_editor.check(text, config_path))
            try:
                saved = config_editor.save(text, config_path, str(body.get("base_sha") or ""))
            except ConfigError as exc:
                return self._json(400, {"error": f"not saved, the config is not valid: {exc}"})
            except config_editor.EditConflict as exc:
                return self._json(409, {"error": str(exc)})
            new_interval, new_db, _, new_miners = saved["config"]
            fleet.replace_miners(new_miners)
            logger.info("config: %s saved by %s (backup %s)", config_path,
                        self.client_address[0], saved["backup"])
            notes = []
            if new_db != db_path:
                notes.append("The database path changed. Restart the dashboard too, "
                             "so it reads the same file as the supervisor.")
            if new_interval != poll_interval:
                notes.append("The poll interval changed. Restart the dashboard too, "
                             "so it judges the supervisor stale on the new interval.")
            return self._json(200, {"ok": True, "sha": saved["sha"], "backup": saved["backup"],
                                    "warnings": saved["warnings"], "notes": notes})

        def _json(self, code: int, body: dict):
            self._send(code, json.dumps(body).encode("utf-8"), "application/json")

        def _send(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            logger.debug("web: " + fmt, *args)

    class Server(ThreadingHTTPServer):
        # HTTPServer turns SO_REUSEADDR on, which on Windows lets a second
        # process take over a port that is already serving. See AGENTS.md.
        allow_reuse_address = ALLOW_REUSE_ADDRESS
        daemon_threads = True

    return Server((host, port), Handler)


class _closing:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()


def serve(host: str, port: int, db_path: str, miners: dict[str, Miner],
          poll_interval: int, alerts_path: str | None = None,
          allow_remote_settings: bool = False, config_path: str | None = None) -> int:
    fleet = FleetState(miners)
    # Read the history once before listening, so the first page load is not
    # the one that waits for it - and so a slow or unreadable database shows
    # up here, in the console, rather than as a page stuck on "loading".
    print(f"Reading {db_path} ...", flush=True)
    started = time.monotonic()
    try:
        with _closing(open_readonly(db_path)) as conn:
            fleet.refresh(conn)
    except sqlite3.OperationalError as exc:
        print(f"Cannot read {db_path}: {exc}")
        print("Is this the config the supervisor uses, and has it run at least once?")
        return 2
    print(f"  done in {time.monotonic() - started:.1f}s "
          f"({len(fleet.poll)} of {len(miners)} miners have been polled)")

    monitor = None
    if alerts_path is not None:
        monitor = alerts_mod.AlertMonitor(alerts_path, db_path, miners, poll_interval,
                                          functools.partial(snapshot, fleet=fleet),
                                          open_readonly)
    server = make_server(host, port, db_path, miners, poll_interval, alerts_path,
                         monitor, allow_remote_settings, fleet, config_path)
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    print(f"MinerWatch dashboard on http://{shown}:{server.server_address[1]}/")
    if host not in ("127.0.0.1", "::1", "localhost"):
        print("Listening beyond this PC: anyone on the network can see the fleet's state.")
    if monitor is not None:
        print(f"Email alerts: settings in {alerts_path} (edit them on the page).")
        monitor.start()
    if config_path is not None:
        print(f"Configuration: {config_path} can be edited on the page "
              f"(backups in {config_editor.backup_dir(config_path)}).")
    print("Ctrl+C to stop. The supervisor is not affected either way.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if monitor is not None:
            monitor.stop()
        server.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MinerWatch</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #fff; --text: #1b1f24; --muted: #5f6b7a; --line: #e2e6eb;
  --ok: #1f8a4c; --warn: #b26b00; --bad: #c62828; --info: #3559c7; --idle: #7a8594;
  --okbg: #e7f5ec; --warnbg: #fdf3e1; --badbg: #fdeaea; --infobg: #e9eefb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151a; --panel: #1b2027; --text: #e6e9ed; --muted: #97a3b2; --line: #2c333d;
    --ok: #4cc27f; --warn: #e6a23c; --bad: #ef6b6b; --info: #7d9bf0; --idle: #8792a2;
    --okbg: #17301f; --warnbg: #352915; --badbg: #3a1d1d; --infobg: #1d2640;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { display: flex; align-items: baseline; gap: 16px; padding: 16px 20px 8px; flex-wrap: wrap; }
h1 { font-size: 20px; margin: 0; }
.muted { color: var(--muted); }
main { padding: 0 20px 24px; max-width: 1200px; }
.banner { border-radius: 8px; padding: 10px 14px; margin: 8px 0; }
.banner.bad { background: var(--badbg); color: var(--bad); }
.banner.warn { background: var(--warnbg); color: var(--warn); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin: 12px 0; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.tile .n { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.wrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; min-width: 760px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr { cursor: pointer; }
tbody tr:hover, tbody tr.sel { background: var(--infobg); }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.mining { background: var(--okbg); color: var(--ok); }
.stopped, .unreachable, .latched { background: var(--badbg); color: var(--bad); }
.asleep, .unsure { background: var(--infobg); color: var(--info); }
.other { background: var(--warnbg); color: var(--warn); }
.why { white-space: normal; color: var(--muted); font-size: 12px; max-width: 360px; }
#detail { margin-top: 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
#detail h2 { font-size: 16px; margin: 0 0 8px; }
#detail .controls { float: right; }
select { background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; }
svg { width: 100%; height: 140px; display: block; }
.dec td { font-size: 13px; }
.dec td.why { max-width: none; }
header button, .btn { background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 5px 12px; font: inherit; cursor: pointer; }
.btn.primary { background: #3559c7; border-color: #3559c7; color: #fff; }
.btn:disabled { opacity: .5; cursor: default; }
header .spacer { flex: 1; }
#alerts { margin: 12px 0; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
#alerts h2 { font-size: 16px; margin: 0 0 4px; }
#alerts h3 { font-size: 13px; margin: 16px 0 6px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px 16px; }
.grid label, .checks label { display: block; font-size: 13px; color: var(--muted); }
.grid input, .grid select { display: block; width: 100%; margin-top: 3px; padding: 6px 8px; font: inherit;
  background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 6px; }
.checks label { color: var(--text); margin: 6px 0; }
.checks input[type=number] { width: 70px; padding: 3px 6px; background: var(--bg); color: var(--text);
  border: 1px solid var(--line); border-radius: 6px; font: inherit; }
.actions { display: flex; gap: 8px; align-items: center; margin-top: 14px; flex-wrap: wrap; }
#amsg.ok, #cmsg.ok { color: var(--ok); } #amsg.bad, #cmsg.bad { color: var(--bad); }
#config { margin: 12px 0; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
#config h2 { font-size: 16px; margin: 0 0 4px; }
#config h3 { font-size: 13px; margin: 16px 0 6px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
#ctext { width: 100%; min-height: 420px; resize: vertical; margin-top: 10px; padding: 10px; tab-size: 2;
  font: 13px/1.5 ui-monospace, "Cascadia Mono", Consolas, monospace; white-space: pre; overflow: auto;
  background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 6px; }
#ctext.dirty { border-color: var(--warn); }
#config table { min-width: 0; }
#config td { white-space: normal; vertical-align: top; }
#config tbody tr { cursor: default; }
#config tbody tr:hover { background: none; }
#config tr.risky td { background: var(--warnbg); }
#config tr.risky td.why { color: var(--warn); font-weight: 600; }
#config pre { background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 8px;
  overflow: auto; max-height: 360px; font: 12px/1.45 ui-monospace, Consolas, monospace; margin: 6px 0; }
#config pre .add { color: var(--ok); } #config pre .del { color: var(--bad); }
#config details { margin-top: 10px; } #config summary { cursor: pointer; color: var(--muted); }
#crestart { white-space: normal; }
#crestart code { background: var(--bg); padding: 1px 5px; border-radius: 4px; }
</style>
</head>
<body>
<header>
  <h1>MinerWatch</h1>
  <span class="muted" id="updated">loading…</span>
  <span class="muted">refreshes every 15s</span>
  <span class="spacer"></span>
  <button id="configBtn" hidden>Configuration</button>
  <button id="alertsBtn" hidden>Email alerts</button>
</header>
<main>
  <section id="config" hidden>
    <h2>Configuration</h2>
    <div class="muted" id="cpath"></div>
    <div id="cview"></div>
    <div id="ceditor" hidden>
      <div class="muted">This is the file itself, comments and all. Changes are checked with the same
        rules the supervisor uses before they can be saved, the old file is kept as a backup, and they
        take effect when the MinerWatch task is restarted.</div>
      <textarea id="ctext" spellcheck="false" autocomplete="off" aria-label="miners.yaml"></textarea>
      <div class="actions">
        <button class="btn" type="button" id="ccheck">Check</button>
        <button class="btn primary" type="button" id="csave" disabled>Save</button>
        <button class="btn" type="button" id="crevert">Discard edits</button>
        <span class="spacer" style="flex:1"></span>
        <select id="cbackups" aria-label="backups"><option value="">Backups…</option></select>
        <button class="btn" type="button" id="cload" disabled>Load into editor</button>
      </div>
      <div id="cmsg" style="margin-top:8px"></div>
      <div id="crestart" class="banner warn" hidden></div>
      <div id="cresult"></div>
    </div>
  </section>
  <section id="alerts" hidden>
    <h2>Email alerts</h2>
    <div class="muted" id="astatus"></div>
    <form id="aform" autocomplete="off">
      <h3>SMTP server</h3>
      <div class="grid">
        <label>Host<input name="smtp_host" placeholder="smtp.gmail.com"></label>
        <label>Port<input name="smtp_port" type="number" min="1" max="65535"></label>
        <label>Security<select name="security">
          <option value="starttls">STARTTLS (usually port 587)</option>
          <option value="ssl">SSL/TLS (usually port 465)</option>
          <option value="none">None (local relay only)</option></select></label>
        <label>Username<input name="username" autocomplete="off"></label>
        <label>Password<input name="password" type="password" autocomplete="new-password"></label>
        <label>From address<input name="from_addr" placeholder="minerwatch@example.com"></label>
        <label>Send to (comma-separated)<input name="to_addrs" placeholder="you@example.com"></label>
        <label>Subject prefix<input name="subject_prefix"></label>
      </div>
      <h3>When to email</h3>
      <div class="checks">
        <label><input type="checkbox" name="enabled"> <b>Alerts on</b></label>
        <label><input type="checkbox" name="on_latched"> A miner is latched and MinerWatch has stopped acting on it</label>
        <label><input type="checkbox" name="on_supervisor_stale"> The supervisor stops polling</label>
        <label><input type="checkbox" name="on_sleep_unsure"> A sleep failed or was not confirmed, so the miner may be asleep unnoticed</label>
        <label>A miner is not mining inside its window for <input type="number" name="not_mining_minutes" min="0"> minutes (0 = off)</label>
        <label>Repeat an active alert every <input type="number" name="repeat_hours" min="0" step="0.5"> hours (0 = once)</label>
        <label><input type="checkbox" name="send_resolved"> Email again when it clears</label>
      </div>
      <div class="actions">
        <button class="btn primary" type="submit" id="asave">Save</button>
        <button class="btn" type="button" id="atest">Send test email</button>
        <span id="amsg"></span>
      </div>
    </form>
  </section>
  <div id="banners"></div>
  <div class="tiles" id="tiles"></div>
  <div class="wrap">
    <table>
      <thead><tr>
        <th>Miner</th><th>Group</th><th>State</th><th class="num">Hashrate</th>
        <th>Window</th><th>Power</th><th>Attention</th><th>Last seen</th><th>Last decision</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <section id="detail" hidden>
    <div class="controls">
      <select id="hours">
        <option value="6">6 hours</option><option value="24" selected>24 hours</option>
        <option value="72">3 days</option><option value="168">7 days</option>
      </select>
    </div>
    <h2 id="dtitle"></h2>
    <div id="chart"></div>
    <div class="muted" id="dwhy"></div>
    <table class="dec"><thead><tr><th>When</th><th>State</th><th>Decision</th><th>Why</th></tr></thead>
      <tbody id="decisions"></tbody></table>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let selected = null;

function rate(ghs) {
  if (ghs == null) return "–";
  if (ghs === 0) return "0";
  return ghs >= 1000 ? (ghs / 1000).toFixed(1) + " TH/s" : Math.round(ghs) + " GH/s";
}
function when(ts) {
  if (!ts) return "never";
  const d = new Date(ts);
  return d.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit"});
}
function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, (Date.now() - new Date(ts)) / 1000);
  if (s < 90) return Math.round(s) + "s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 172800) return (s / 3600).toFixed(1) + " h ago";
  return Math.round(s / 86400) + " days ago";
}
function pill(text, cls) { return `<span class="pill ${cls}">${esc(text)}</span>`; }
function stateCls(s) { return ["mining","stopped","unreachable"].includes(s) ? s : "other"; }

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try { await doRefresh(); } finally { refreshing = false; }
}

async function doRefresh() {
  let data;
  // Never sit on "loading" silently: say so if the server is slow, and give up
  // with a reason rather than waiting forever.
  const slow = setTimeout(() => {
    $("banners").innerHTML = `<div class="banner warn">Still waiting for the dashboard data. Check the window running "minerwatch web" for errors.</div>`;
  }, 8000);
  const abort = new AbortController();
  const giveUp = setTimeout(() => abort.abort(), 60000);
  try {
    const r = await fetch("api/status", {cache: "no-store", signal: abort.signal});
    data = await r.json();
    if (!r.ok) throw new Error(data.error || r.status);
  } catch (e) {
    const why = e.name === "AbortError" ? "no answer from the server after 60s" : e.message;
    $("banners").innerHTML = `<div class="banner bad">Cannot read the dashboard data: ${esc(why)}</div>`;
    return;
  } finally {
    clearTimeout(slow); clearTimeout(giveUp);
  }
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();
  const banners = [];
  if (data.stale) {
    banners.push(data.newest_poll
      ? `No poll recorded for ${esc(ago(data.newest_poll).replace(" ago", ""))} (expected every ${data.poll_interval}s). The supervisor may not be running.`
      : "No poll has ever been recorded in this database. Is the supervisor running with this config?");
  }
  const latched = data.miners.filter(m => m.latches.length);
  for (const m of latched) {
    banners.push(`<b>${esc(m.id)}</b> is latched (${esc(m.latches.join(" + "))}) since ${esc(when(m.latched_since))}, ${esc(ago(m.latched_since))}. MinerWatch will not act on it until cleared, and clearing needs a supervisor restart.`);
  }
  $("banners").innerHTML = banners.map(b => `<div class="banner ${data.stale && b === banners[0] ? "warn" : "bad"}">${b}</div>`).join("");

  const ms = data.miners;
  const total = ms.reduce((a, m) => a + (m.state === "mining" && m.ghs ? m.ghs : 0), 0);
  const count = (f) => ms.filter(f).length;
  const tiles = [
    ["Fleet hashrate", rate(total)],
    ["Mining", `${count(m => m.state === "mining")} / ${ms.length}`],
    ["Not mining in window", count(m => m.state !== "mining" && m.window === "open")],
    ["Asleep", count(m => m.power === "asleep")],
    ["Latched", latched.length],
  ];
  $("tiles").innerHTML = tiles.map(([k, v]) => `<div class="tile"><div class="muted">${k}</div><div class="n">${esc(v)}</div></div>`).join("");

  $("rows").innerHTML = ms.map(m => `
    <tr data-id="${esc(m.id)}" class="${m.id === selected ? "sel" : ""}">
      <td><b>${esc(m.id)}</b><div class="muted" style="font-size:12px">${esc(m.host)}</div></td>
      <td>${esc(m.group || "–")}</td>
      <td>${pill(m.state, stateCls(m.state))}${m.diagnosis || m.reason ? `<div class="why">${esc(m.diagnosis || m.reason)}</div>` : ""}</td>
      <td class="num">${rate(m.ghs)}</td>
      <td>${esc(m.window)}</td>
      <td>${["asleep","unsure"].includes(m.power) ? pill(m.power, m.power) : esc(m.power)}</td>
      <td>${m.latches.length ? pill(m.latches.join(" + "), "latched") : "–"}</td>
      <td title="${esc(m.last_seen)}">${esc(ago(m.last_seen) || "never")}</td>
      <td>${m.last_decision ? `${esc(m.last_decision.action)}<div class="why">${esc(ago(m.last_decision.ts))}</div>` : "–"}</td>
    </tr>`).join("");
  for (const tr of $("rows").querySelectorAll("tr")) tr.onclick = () => select(tr.dataset.id);
  if (selected) loadHistory();
}

function select(id) { selected = id; refresh(); }
$("hours").onchange = () => loadHistory();

async function loadHistory() {
  const hours = $("hours").value;
  const r = await fetch(`api/history?miner=${encodeURIComponent(selected)}&hours=${hours}`, {cache: "no-store"});
  const h = await r.json();
  $("detail").hidden = false;
  $("dtitle").textContent = `${h.miner}: last ${$("hours").selectedOptions[0].text}`;
  $("chart").innerHTML = chart(h.points, h.hours);
  $("dwhy").textContent = h.points.length ? "" : "No polls recorded in this period.";
  $("decisions").innerHTML = h.decisions.length ? h.decisions.map(d => `
    <tr><td>${esc(when(d.ts))}</td><td>${pill(d.state, stateCls(d.state))}</td><td>${esc(d.action)}</td><td class="why">${esc(d.why)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">No decisions in this period: neither the sleep controller nor the watchdog acted on this miner.</td></tr>`;
}

function chart(points, hours) {
  if (!points.length) return "";
  const W = 1000, H = 140, pad = 4;
  const end = Date.now(), start = end - hours * 3600e3;
  const max = Math.max(1, ...points.map(p => p.ghs || 0));
  const x = (ts) => pad + (W - 2 * pad) * (new Date(ts) - start) / (end - start);
  const y = (g) => H - pad - (H - 2 * pad) * (g || 0) / max;
  const gaps = points.filter(p => p.state !== "mining").map(p =>
    `<rect x="${x(p.ts).toFixed(1)}" y="0" width="3" height="${H}" fill="var(--bad)" opacity=".18"/>`).join("");
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(p.ts).toFixed(1)},${y(p.ghs).toFixed(1)}`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="hashrate">
    ${gaps}<path d="${line}" fill="none" stroke="var(--ok)" stroke-width="2" vector-effect="non-scaling-stroke"/>
    </svg><div class="muted" style="font-size:12px">peak ${rate(max)} · red bands: not mining</div>`;
}

// ---- email alerts -------------------------------------------------------
const form = $("aform");
const BOOLS = ["enabled","on_latched","on_supervisor_stale","on_sleep_unsure","send_resolved"];
let alertsInfo = null;

async function loadAlerts(fillForm) {
  const r = await fetch("api/alerts", {cache: "no-store"});
  const a = await r.json();
  if (!a.available) return;
  alertsInfo = a;
  $("alertsBtn").hidden = false;
  const st = a.status || {};
  const bits = [a.settings.enabled ? "On" : "Off", `settings file: ${a.path}`];
  if (st.last_sent) bits.push("last email " + ago(st.last_sent));
  if (st.active && st.active.length) bits.push(`${st.active.length} active: ${st.active.map(x => x.title).join("; ")}`);
  $("astatus").innerHTML = esc(bits.join(" · ")) +
    (st.last_error ? `<div style="color:var(--bad)">Last error: ${esc(st.last_error)}</div>` : "") +
    (a.can_edit ? "" : `<div style="color:var(--warn)">View only: settings can be changed from the MinerWatch PC itself.</div>`);
  if (!fillForm) return;
  const s = a.settings;
  for (const el of form.elements) {
    if (!el.name) continue;
    if (BOOLS.includes(el.name)) el.checked = !!s[el.name];
    else if (el.name === "to_addrs") el.value = (s.to_addrs || []).join(", ");
    else if (el.name === "password") { el.value = ""; el.placeholder = s.password_set ? "saved - leave blank to keep" : ""; }
    else el.value = s[el.name] ?? "";
    el.disabled = !a.can_edit;
  }
  $("asave").disabled = $("atest").disabled = !a.can_edit;
}

function formData() {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    out[el.name] = BOOLS.includes(el.name) ? el.checked : el.value;
  }
  return out;
}

async function post(path, body) {
  const r = await fetch(path, {method: "POST", body: JSON.stringify(body),
    headers: {"Content-Type": "application/json", "X-MinerWatch": "1"}});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}
function say(text, ok) { $("amsg").textContent = text; $("amsg").className = ok ? "ok" : "bad"; }

form.onsubmit = async (e) => {
  e.preventDefault();
  try { await post("api/alerts", formData()); say("Saved.", true); loadAlerts(true); }
  catch (err) { say(err.message, false); }
};
$("atest").onclick = async () => {
  say("Sending…", true);
  try { const d = await post("api/alerts/test", formData()); say("Test email sent to " + d.sent_to.join(", ") + ".", true); }
  catch (err) { say(err.message, false); }
};
$("alertsBtn").onclick = () => {
  const sec = $("alerts");
  sec.hidden = !sec.hidden;
  if (!sec.hidden) loadAlerts(true);
};

// ---- configuration editor ----------------------------------------------
const ctext = $("ctext");
let cfg = {sha: null, saved: "", checkedText: null, checkOk: false, risky: []};
let checkTimer = null, checkSeq = 0;

async function getJson(path) {
  const r = await fetch(path, {cache: "no-store", headers: {"X-MinerWatch": "1"}});
  const data = await r.json();
  if (!r.ok) { const e = new Error(data.error || r.status); e.data = data; throw e; }
  return data;
}
function csay(text, ok) { $("cmsg").textContent = text; $("cmsg").className = ok ? "ok" : "bad"; }
function dirty() { return ctext.value !== cfg.saved; }
function setSaveState() {
  ctext.classList.toggle("dirty", dirty());
  $("csave").disabled = !(dirty() && cfg.checkOk && cfg.checkedText === ctext.value);
}

async function probeConfig() {
  try { const d = await getJson("api/config"); if (d.available) $("configBtn").hidden = false; }
  catch (e) { if (e.data && e.data.available) $("configBtn").hidden = false; }
}

async function loadConfig() {
  let d;
  try { d = await getJson("api/config"); }
  catch (e) {
    $("cpath").textContent = "";
    $("ceditor").hidden = true;
    $("cview").innerHTML = `<div class="banner warn">${esc(e.message)}</div>`;
    return;
  }
  $("cview").innerHTML = "";
  $("ceditor").hidden = false;
  $("cpath").textContent = d.path;
  cfg.sha = d.sha; cfg.saved = d.text;
  ctext.value = d.text;
  showCheck(d.check, d.text);
  csay("", true);
  loadBackups();
}

async function loadBackups() {
  try {
    const d = await getJson("api/config/backups");
    $("cbackups").innerHTML = `<option value="">Backups (${d.backups.length})…</option>` +
      d.backups.map(b => `<option value="${esc(b.name)}">${esc(when(b.saved))} · ${esc(b.name)}</option>`).join("");
  } catch (e) { /* the editor still works without the list */ }
  $("cload").disabled = true;
}
$("cbackups").onchange = () => { $("cload").disabled = !$("cbackups").value; };
$("cload").onclick = async () => {
  const name = $("cbackups").value;
  if (!name) return;
  if (dirty() && !confirm("Replace your unsaved edits with this backup?")) return;
  try {
    const d = await getJson("api/config/backup?name=" + encodeURIComponent(name));
    ctext.value = d.text;
    csay(`Loaded ${name} into the editor. It is not saved until you check and save it.`, true);
    runCheck();
  } catch (e) { csay(e.message, false); }
};

function showCheck(c, text) {
  cfg.checkedText = text; cfg.checkOk = c.ok;
  cfg.risky = (c.changes || []).filter(x => x.risky);
  const out = [];
  for (const e of c.errors) out.push(`<div class="banner bad"><b>Cannot be saved:</b> ${esc(e)}</div>`);
  for (const w of c.warnings) out.push(`<div class="banner warn">${esc(w)}</div>`);
  if (c.ok && text !== cfg.saved) {
    out.push(`<h3>What changes${cfg.risky.length ? ` · ${cfg.risky.length} to double-check` : ""}</h3>`);
    out.push(c.changes.length ? `<div class="wrap"><table><thead><tr><th>Miner</th><th>Setting</th><th>Before</th><th>After</th><th>Note</th></tr></thead><tbody>` +
      c.changes.map(x => `<tr class="${x.risky ? "risky" : ""}"><td><b>${esc(x.miner)}</b></td><td>${esc(x.field)}</td><td>${esc(x.before)}</td><td>${esc(x.after)}</td><td class="why">${esc(x.why)}</td></tr>`).join("") +
      `</tbody></table></div>` : `<div class="muted">No setting changes after inheritance (comments or layout only).</div>`);
  }
  if (c.ok) {
    out.push(`<details${text === cfg.saved ? " open" : ""}><summary>Each miner's resolved settings (${c.miners.length})</summary><div class="wrap" style="margin-top:6px"><table><thead><tr><th>Miner</th><th>Group</th><th>Address</th><th>Running hours</th><th>Sleep</th><th>Restart</th></tr></thead><tbody>` +
      c.miners.map(m => `<tr><td><b>${esc(m.id)}</b></td><td>${esc(m.group || "–")}</td><td>${esc(m.address)}</td><td>${esc(m["running hours"])}</td><td>${m.live_sleep ? pill(m.sleep, "other") : esc(m.sleep)}</td><td>${esc(m.restart)}</td></tr>`).join("") +
      `</tbody></table></div></details>`);
  }
  if (c.diff) {
    const lines = c.diff.split("\n").map(l => {
      const cls = l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : "";
      return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
    });
    out.push(`<details><summary>Line-by-line difference from the saved file</summary><pre>${lines.join("\n")}</pre></details>`);
  }
  $("cresult").innerHTML = out.join("");
  setSaveState();
}

async function runCheck() {
  clearTimeout(checkTimer);
  const text = ctext.value, seq = ++checkSeq;
  try {
    const c = await post("api/config/check", {text});
    if (seq !== checkSeq) return;  // a newer edit is already being checked
    showCheck(c, text);
    if (text === ctext.value && dirty()) csay(c.ok ? "Valid. Review the changes below, then save." : "", true);
  } catch (e) { if (seq === checkSeq) csay(e.message, false); }
}
$("ccheck").onclick = runCheck;
ctext.addEventListener("input", () => {
  setSaveState();
  clearTimeout(checkTimer);
  checkTimer = setTimeout(runCheck, 700);
});
ctext.addEventListener("keydown", (e) => {
  if (e.key === "Tab" && !e.shiftKey) {  // YAML forbids tabs: indent with spaces
    e.preventDefault();
    document.execCommand("insertText", false, "  ");
  }
});

$("csave").onclick = async () => {
  const text = ctext.value;
  if (text !== cfg.checkedText || !cfg.checkOk) return;
  if (cfg.risky.length && !confirm("These changes need a second look:\n\n" +
      cfg.risky.map(x => `• ${x.miner} ${x.field}: ${x.why}`).join("\n") + "\n\nSave anyway?")) return;
  $("csave").disabled = true;
  try {
    const d = await post("api/config/save", {text, base_sha: cfg.sha});
    cfg.sha = d.sha; cfg.saved = text;
    csay(`Saved${d.backup ? `. The previous version is kept as ${d.backup}` : ""}.`, true);
    $("crestart").hidden = false;
    $("crestart").innerHTML = `<b>Not in effect yet.</b> The supervisor reads the file only when it starts. ` +
      `Restart it from an administrator PowerShell with <code>Stop-ScheduledTask -TaskName MinerWatch; Start-ScheduledTask -TaskName MinerWatch</code> ` +
      `(or End, then Run, in Task Scheduler). The dashboard already shows the new miner list.` +
      d.notes.map(n => `<div>${esc(n)}</div>`).join("");
    runCheck();
    loadBackups();
    refresh();
  } catch (e) { csay(e.message, false); setSaveState(); }
};
$("crevert").onclick = () => {
  if (dirty() && !confirm("Discard your edits and reload the saved file?")) return;
  loadConfig();
};
$("configBtn").onclick = () => {
  const sec = $("config");
  if (!sec.hidden && dirty() && !confirm("Close the editor? Your unsaved edits stay until you reload the page.")) return;
  sec.hidden = !sec.hidden;
  if (!sec.hidden && !dirty()) loadConfig();
};
window.addEventListener("beforeunload", (e) => {
  if (!$("config").hidden && dirty()) { e.preventDefault(); e.returnValue = ""; }
});

refresh();
loadAlerts(false);
probeConfig();
setInterval(() => { refresh(); if (!$("alerts").hidden) loadAlerts(false); }, 15000);
</script>
</body>
</html>
"""
