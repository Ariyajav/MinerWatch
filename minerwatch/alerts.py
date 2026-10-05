"""Email alerts for the conditions that have cost this fleet real production.

Every one of these was silent before: a latch writes one row and then nothing,
a stopped supervisor writes nothing at all, and a miner that stops hashing
inside its window only shows up if someone runs `status`. The monitor runs in
the dashboard process, not the supervisor, for two reasons:

* it has to keep working when the supervisor does not - "no polls for ten
  minutes" can only be reported by something that is not the poller; and
* it sends nothing to a miner and writes nothing to the events table, so it
  adds no risk to the process that actuates hardware.

Settings live in their own JSON file beside ``miners.yaml`` (default
``alerts.json``), editable from the dashboard. They are kept out of
``miners.yaml`` so that saving a form can never corrupt the fleet config.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from minerwatch.compat import read_text, write_text_atomic

logger = logging.getLogger("minerwatch")

SECURITY_MODES = ("starttls", "ssl", "none")
SMTP_TIMEOUT_SECONDS = 20


@dataclass
class AlertSettings:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    #: ``starttls`` (port 587), ``ssl`` (implicit TLS, port 465) or ``none``.
    security: str = "starttls"
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    #: Prefix on every subject line, so a filter can route them.
    subject_prefix: str = "[MinerWatch]"

    # Which conditions are worth an email. All on by default: each one is a
    # failure that has already gone unnoticed on this fleet for hours or days.
    on_latched: bool = True
    on_supervisor_stale: bool = True
    on_sleep_unsure: bool = True
    #: Minutes a miner may be not mining inside its window before an email.
    #: 0 turns it off. 45 sits past the watchdog's 30-minute restart clock, so
    #: a miner the watchdog recovers on its own never pages anyone.
    not_mining_minutes: int = 45
    #: Also send an email when a condition clears.
    send_resolved: bool = True
    #: Re-send a still-active alert every N hours. 0 sends it once. A latch is
    #: silent forever otherwise, which is how s19-07 sat dead for two days.
    repeat_hours: float = 12.0

    def validate(self) -> list[str]:
        problems = []
        if self.security not in SECURITY_MODES:
            problems.append(f"security must be one of {', '.join(SECURITY_MODES)}")
        if not 0 < int(self.smtp_port) < 65536:
            problems.append("smtp_port must be between 1 and 65535")
        if self.not_mining_minutes < 0 or self.repeat_hours < 0:
            problems.append("minutes and hours cannot be negative")
        if self.enabled:
            if not self.smtp_host:
                problems.append("smtp_host is required when alerts are enabled")
            if not self.from_addr or "@" not in self.from_addr:
                problems.append("from_addr must be an email address")
            if not self.to_addrs or any("@" not in a for a in self.to_addrs):
                problems.append("to_addrs needs at least one email address")
        return problems

    def public(self) -> dict:
        """Settings safe to send to a browser: the password never leaves the host."""
        data = asdict(self)
        data["password_set"] = bool(data.pop("password"))
        return data


_FIELDS = {f for f in AlertSettings.__dataclass_fields__}


def load_settings(path: str) -> AlertSettings:
    if not Path(path).exists():
        return AlertSettings()
    raw = json.loads(read_text(path))
    return AlertSettings(**{k: v for k, v in raw.items() if k in _FIELDS})


def save_settings(path: str, settings: AlertSettings) -> None:
    write_text_atomic(path, json.dumps(asdict(settings), indent=2) + "\n")


def merge_update(current: AlertSettings, update: dict) -> AlertSettings:
    """Apply a form submission. An empty password keeps the stored one."""
    data = asdict(current)
    for key, value in update.items():
        if key not in _FIELDS:
            continue
        if key == "password" and not value:
            continue
        data[key] = value
    if isinstance(data["to_addrs"], str):
        data["to_addrs"] = data["to_addrs"].replace(";", ",").split(",")
    data["to_addrs"] = [a.strip() for a in data["to_addrs"] if a and a.strip()]
    for key in ("smtp_port", "not_mining_minutes"):
        data[key] = int(data[key])
    data["repeat_hours"] = float(data["repeat_hours"])
    for key in ("enabled", "on_latched", "on_supervisor_stale", "on_sleep_unsure",
                "send_resolved"):
        data[key] = bool(data[key])
    for key in ("smtp_host", "username", "from_addr", "subject_prefix", "security"):
        data[key] = str(data[key]).strip()
    return AlertSettings(**data)


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Condition:
    key: str
    title: str
    detail: str


def evaluate(snap: dict, settings: AlertSettings, conn, now: datetime) -> dict[str, Condition]:
    """Every alert-worthy condition true right now, keyed for de-duplication."""
    active: dict[str, Condition] = {}
    if settings.on_supervisor_stale and snap["stale"]:
        age = snap["poll_age_seconds"]
        detail = (f"No poll has been recorded for {age / 60:.0f} minutes "
                  f"(expected every {snap['poll_interval']}s)." if age is not None
                  else "No poll has ever been recorded in the events database.")
        active["stale"] = Condition(
            "stale", "Supervisor is not polling",
            detail + " MinerWatch may not be running, so nothing is being watched, "
            "slept, woken or restarted. Check the scheduled task and the "
            "'Starting:' lines in logs\\minerwatch.log.",
        )
        # Every other condition is computed from the same stale rows, so it
        # would describe the last thing the supervisor saw, not the fleet now.
        return active

    for m in snap["miners"]:
        if settings.on_latched and m["latches"]:
            active[f"latched:{m['id']}"] = Condition(
                f"latched:{m['id']}", f"{m['id']} is latched ({' + '.join(m['latches'])})",
                f"Latched since {_utc(m['latched_since'])}. MinerWatch will take no "
                f"further action on {m['id']} until a human clears it, and "
                f"clear-attention only reaches the supervisor after a restart. "
                f"Current state: {m['state']}.",
            )
        if settings.on_sleep_unsure and m["power"] == "unsure":
            active[f"unsure:{m['id']}"] = Condition(
                f"unsure:{m['id']}", f"{m['id']}: sleep outcome unknown",
                "A sleep command failed or was not confirmed, so the miner may be "
                "asleep without MinerWatch knowing. Check its work mode in the "
                "miner's own web UI. A miner lost 14 hours this way on 15 Sep 2026.",
            )
        minutes = settings.not_mining_minutes
        if minutes and _not_mining_for(conn, m, minutes, snap["poll_interval"], now):
            why = m["diagnosis"] or m["reason"] or ""
            active[f"down:{m['id']}"] = Condition(
                f"down:{m['id']}", f"{m['id']} not mining for {minutes}+ minutes",
                f"{m['id']} has been {m['state']} inside its window for at least "
                f"{minutes} minutes." + (f" Reason: {why}" if why else ""),
            )
    return active


def _utc(ts: str | None) -> str:
    if not ts:
        return "an unknown time"
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _not_mining_for(conn, miner: dict, minutes: int, poll_interval: int,
                    now: datetime) -> bool:
    """True when every poll in the last *minutes* was an in-window failure.

    Requires polls covering the whole span, so a window that opened ten
    minutes ago, or a supervisor that only just started, does not count as
    forty-five minutes of failure. A miner MinerWatch slept on purpose is
    excluded: it is not mining because it was told not to.
    """
    if miner["state"] == "mining" or miner["window"] != "open" or miner["power"] == "asleep":
        return False
    since = now - timedelta(minutes=minutes)
    rows = conn.execute(
        "SELECT ts, action FROM events WHERE miner = ? AND ts >= ? "
        "AND action IN ('none', 'alert', 'expected_off') ORDER BY ts",
        (miner["id"], since.isoformat()),
    ).fetchall()
    if not rows or any(action != "alert" for _, action in rows):
        return False
    first = datetime.fromisoformat(rows[0][0])
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    return first <= since + timedelta(seconds=2 * max(poll_interval, 15))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_email(settings: AlertSettings, subject: str, body: str) -> None:
    """Send one message. Raises on any failure; the caller decides what that means."""
    msg = EmailMessage()
    msg["Subject"] = f"{settings.subject_prefix} {subject}".strip()
    msg["From"] = settings.from_addr
    msg["To"] = ", ".join(settings.to_addrs)
    msg.set_content(body)
    context = ssl.create_default_context()
    if settings.security == "ssl":
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                  timeout=SMTP_TIMEOUT_SECONDS, context=context)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                              timeout=SMTP_TIMEOUT_SECONDS)
    with server:
        if settings.security == "starttls":
            server.starttls(context=context)
        if settings.username:
            server.login(settings.username, settings.password)
        server.send_message(msg)


class AlertMonitor:
    """Evaluate conditions every poll interval and email the changes.

    One email per check, however many conditions changed: twelve miners losing
    power together is one event, not twelve messages. What has been sent is
    persisted beside the settings so that restarting the dashboard does not
    re-send every active alert.
    """

    def __init__(self, settings_path: str, db_path: str, miners: dict,
                 poll_interval: int, snapshot_fn, open_fn, sender=send_email):
        self.settings_path = settings_path
        self.state_path = str(Path(settings_path).with_suffix(".state.json"))
        self.db_path = db_path
        self.miners = miners
        self.interval = max(int(poll_interval), 15)
        self._snapshot = snapshot_fn
        self._open = open_fn
        self._send = sender
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.status: dict = {"last_check": None, "last_sent": None, "last_error": None,
                             "active": []}
        self._sent: dict[str, dict] = self._load_state()

    def _load_state(self) -> dict:
        try:
            return json.loads(read_text(self.state_path)).get("sent", {})
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            write_text_atomic(self.state_path, json.dumps({"sent": self._sent}, indent=2))
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("alerts: could not save %s: %s", self.state_path, exc)

    def check_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            settings = load_settings(self.settings_path)
            conn = self._open(self.db_path)
            try:
                snap = self._snapshot(conn, self.miners, self.interval, now=now)
                active = evaluate(snap, settings, conn, now)
            finally:
                conn.close()
            self.status["last_check"] = now.isoformat()
            self.status["active"] = [{"key": c.key, "title": c.title} for c in active.values()]
            if not settings.enabled or not settings.to_addrs:
                return

            new, reminders, resolved = [], [], []
            for key, cond in active.items():
                sent = self._sent.get(key)
                if sent is None:
                    new.append(cond)
                elif settings.repeat_hours and now - datetime.fromisoformat(
                        sent["last_sent"]) >= timedelta(hours=settings.repeat_hours):
                    reminders.append(cond)
            for key, sent in self._sent.items():
                # While the supervisor is stale every per-miner condition drops
                # out of `active` because it is not evaluated, not because it
                # cleared. Reporting those as resolved would be a false all-clear
                # at exactly the moment nothing is being watched.
                if key not in active and "stale" not in active:
                    resolved.append((key, sent))
            if not (new or reminders or (resolved and settings.send_resolved)):
                for key, _ in resolved:
                    self._sent.pop(key, None)
                if resolved:
                    self._save_state()
                return

            subject, body = _compose(new, reminders, resolved if settings.send_resolved else [],
                                     now)
            try:
                self._send(settings, subject, body)
            except Exception as exc:
                # Leave the bookkeeping untouched so the next check retries.
                # An alert that silently failed to send is the failure mode
                # this whole module exists to remove.
                self.status["last_error"] = f"{now.isoformat()}: {exc}"
                logger.warning("alerts: email failed: %s", exc)
                return
            self.status["last_sent"] = now.isoformat()
            self.status["last_error"] = None
            for cond in new + reminders:
                entry = self._sent.setdefault(cond.key, {"since": now.isoformat(),
                                                         "title": cond.title})
                entry["last_sent"] = now.isoformat()
            for key, _ in resolved:
                self._sent.pop(key, None)
            self._save_state()
            logger.info("alerts: sent '%s'", subject)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # never let one bad check end alerting
                self.status["last_error"] = f"check failed: {exc}"
                logger.warning("alerts: check failed: %s", exc)
            self._stop.wait(self.interval)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="minerwatch-alerts", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


def _compose(new, reminders, resolved, now: datetime) -> tuple[str, str]:
    parts = []
    if new:
        parts.append("NEW\n" + "\n\n".join(f"* {c.title}\n  {c.detail}" for c in new))
    if reminders:
        parts.append("STILL ACTIVE\n" + "\n\n".join(f"* {c.title}\n  {c.detail}"
                                                    for c in reminders))
    if resolved:
        parts.append("RESOLVED\n" + "\n".join(
            f"* {s.get('title', k)} (active since {_utc(s.get('since'))})" for k, s in resolved))
    first = (new or reminders)
    if first:
        subject = first[0].title
        others = len(new) + len(reminders) - 1
        if others:
            subject += f" (+{others} more)"
    else:
        subject = f"Resolved: {resolved[0][1].get('title', resolved[0][0])}"
        if len(resolved) > 1:
            subject += f" (+{len(resolved) - 1} more)"
    body = "\n\n".join(parts) + (
        f"\n\nChecked {now.strftime('%Y-%m-%d %H:%M')} UTC by the MinerWatch dashboard.\n")
    return subject, body
