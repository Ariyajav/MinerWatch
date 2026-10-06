"""Edit ``miners.yaml`` from the dashboard without being able to break it.

The editor works on the file's own text, not on a form that regenerates it,
so comments, ordering and the global -> group -> miner inheritance survive a
save exactly as the operator wrote them. What it adds over a text editor:

* **Check before save.** A candidate is parsed by the same :func:`load_config`
  the supervisor uses, so anything the editor accepts the supervisor will
  start with. A candidate that does not parse cannot be saved.
* **A summary of what actually changes,** after inheritance, per miner. A
  one-word edit to a group block can switch sleep to LIVE on ten miners; the
  summary says so and marks it, and the page asks before saving it.
* **A backup on every save,** next to the config in ``config-backups/``, and
  a conflict check so a save never overwrites an edit made in Notepad since
  the page loaded the file.

Saving never touches the running supervisor. It reads the file once at
startup, so a change takes effect when the MinerWatch task is restarted, and
the page says that after every save.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from minerwatch.compat import read_text, write_text_atomic
from minerwatch.config import ConfigError, load_config
from minerwatch.models import Miner, RecoverWith

logger = logging.getLogger("minerwatch")

BACKUP_DIR = "config-backups"
#: Backups kept per config file; the oldest are removed after a save.
KEEP_BACKUPS = 50
#: Largest config accepted. A 100-miner file is about 20 KB.
MAX_CONFIG_BYTES = 512 * 1024
_BACKUP_RE = re.compile(r"^[A-Za-z0-9_.-]+-\d{8}-\d{6}(-\d+)?\.yaml$")


class EditConflict(Exception):
    """The file on disk changed after the editor loaded it."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current(config_path: str) -> dict:
    text = read_text(config_path)
    return {"path": config_path, "text": text, "sha": digest(text)}


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------


class _Capture(logging.Handler):
    """Collect the warnings load_config logs, from this thread only."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.thread = threading.get_ident()
        self.messages: list[str] = []

    def emit(self, record):
        if record.thread == self.thread:
            self.messages.append(record.getMessage())


def parse_text(text: str, config_path: str):
    """``load_config`` on *text* as if it were saved at *config_path*.

    The candidate goes to a temporary file in the config's own directory, so
    relative paths in it (``db_path``) resolve exactly as they will after the
    save. Returns ``(config, warnings)``; raises ConfigError.
    """
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ConfigError(f"the file is larger than {MAX_CONFIG_BYTES // 1024} KB")
    folder = os.path.dirname(os.path.abspath(config_path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".minerwatch-check-", suffix=".yaml")
    capture = _Capture()
    logger.addHandler(capture)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        try:
            config = load_config(tmp)
        except ConfigError:
            raise
        except Exception as exc:  # YAML syntax errors, bad types deep in a block
            raise ConfigError(_yaml_message(exc)) from exc
    finally:
        logger.removeHandler(capture)
        try:
            os.unlink(tmp)
        except OSError:
            pass
    # Each lint note is logged once by load_config; keep the order, drop repeats.
    warnings = list(dict.fromkeys(capture.messages))
    return config, warnings


def _yaml_message(exc: Exception) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None)
    if mark is not None and problem:
        return f"YAML error on line {mark.line + 1}, column {mark.column + 1}: {problem}"
    return f"{type(exc).__name__}: {exc}"


def check(text: str, config_path: str) -> dict:
    """Everything the page shows about a candidate, without saving it."""
    try:
        new, warnings = parse_text(text, config_path)
    except ConfigError as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": [], "changes": [],
                "miners": [], "diff": _text_diff(config_path, text)}
    try:
        old = load_config(config_path)
    except Exception:  # the file on disk is already broken: everything is new
        old = None
    changes = summarise_changes(old, new)
    return {
        "ok": True,
        "errors": [],
        "warnings": warnings,
        "changes": changes,
        "risky": any(c["risky"] for c in changes),
        "miners": resolved(new),
        "diff": _text_diff(config_path, text),
    }


def _text_diff(config_path: str, text: str) -> str:
    try:
        before = read_text(config_path)
    except OSError:
        before = ""
    lines = difflib.unified_diff(before.splitlines(), text.splitlines(),
                                 "saved", "edited", n=2, lineterm="")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# What each miner resolves to, and what changed
# ---------------------------------------------------------------------------


def _sleep_text(m: Miner) -> str:
    if not m.sleep.enabled:
        return "off (monitor only)"
    return f"{m.sleep.backend.value} {'dry-run' if m.sleep.dry_run else 'LIVE'}"


def _facts(m: Miner) -> dict:
    from minerwatch.cli import _fmt_schedule, _fmt_watchdog

    hours = _fmt_schedule(m.schedule)
    tz = str(m.schedule.timezone) if m.schedule is not None else ""
    return {
        "address": f"{m.host}:{m.port}",
        "group": m.group or "",
        "running hours": "; ".join(hours) + (f" ({tz})" if tz else ""),
        "sleep": _sleep_text(m),
        "restart": _fmt_watchdog(m),
    }


def resolved(config) -> list[dict]:
    """One row per miner: the settings it really ends up with."""
    poll_interval, db_path, default_tz, miners = config
    return [{"id": m.id, **_facts(m),
             "live_sleep": m.sleep.enabled and not m.sleep.dry_run,
             "watchdog_on": m.watchdog is None or m.watchdog.enabled}
            for m in miners.values()]


def _risk(field: str, old: Miner | None, new: Miner | None) -> str | None:
    """Why a change deserves a second look before saving, or None."""
    if new is None:
        return "no longer watched: no polls, restarts or sleep for this miner"
    if old is None:
        live = []
        if new.sleep.enabled and not new.sleep.dry_run:
            live.append("LIVE sleep")
        if new.watchdog is None or new.watchdog.enabled:
            live.append("restarts")
        return f"new miner with {' and '.join(live)} on" if live else None
    if field == "sleep":
        if new.sleep.enabled and not new.sleep.dry_run and not (
                old.sleep.enabled and not old.sleep.dry_run):
            return "sends real sleep and wake commands once the task restarts"
        if old.sleep.enabled and not new.sleep.enabled:
            return "stops putting this miner to sleep outside its hours"
    if field == "restart":
        was, now = old.watchdog, new.watchdog
        if now is not None and now.enabled and (was is not None and not was.enabled):
            return "starts sending restarts"
        if now is not None and now.recover_with is not RecoverWith.CGMINER and (
                was is None or was.recover_with is not now.recover_with):
            return "recovery reboots the whole control board"
    if field == "running hours" and new.sleep.enabled:
        return "changes when this miner is put to sleep"
    if field == "address":
        return "polls a different device"
    return None


def summarise_changes(old, new) -> list[dict]:
    """Settings that differ after inheritance, as rows for the page."""
    rows: list[dict] = []

    def add(miner, field, before, after, risk=None):
        rows.append({"miner": miner, "field": field, "before": before, "after": after,
                     "risky": bool(risk), "why": risk or ""})

    new_interval, new_db, new_tz, new_miners = new
    if old is None:
        add("(file)", "config", "unreadable", "valid")
        old_miners = {}
    else:
        old_interval, old_db, old_tz, old_miners = old
        if old_interval != new_interval:
            add("(all)", "poll interval", f"{old_interval}s", f"{new_interval}s")
        if old_db != new_db:
            add("(all)", "database", old_db, new_db,
                "the supervisor starts a new event history in another file; "
                "the dashboard keeps reading the old one until it is restarted")
        if old_tz != new_tz:
            add("(all)", "default time zone", old_tz, new_tz)

    for mid in old_miners:
        if mid not in new_miners:
            add(mid, "miner", "watched", "removed", _risk("miner", old_miners[mid], None))
    for mid, m in new_miners.items():
        if mid not in old_miners:
            add(mid, "miner", "-", "added", _risk("miner", None, m))
            continue
        before, after = _facts(old_miners[mid]), _facts(m)
        for field, value in after.items():
            if before[field] != value:
                add(mid, field, before[field], value, _risk(field, old_miners[mid], m))
        # Settings the table does not show still matter: say that something
        # changed rather than hiding it.
        if before == after and (old_miners[mid].sleep != m.sleep
                                or old_miners[mid].watchdog != m.watchdog):
            add(mid, "other settings", "", "changed (timeouts, commands or credentials)")
    return rows


# ---------------------------------------------------------------------------
# Save and backups
# ---------------------------------------------------------------------------


def backup_dir(config_path: str) -> Path:
    return Path(os.path.abspath(config_path)).parent / BACKUP_DIR


def save(text: str, config_path: str, base_sha: str, now: datetime | None = None) -> dict:
    """Write *text* to the config, keeping a copy of what it replaces.

    Raises ConfigError if the candidate does not parse, and EditConflict if
    the file no longer matches *base_sha* (someone saved it in the meantime).
    """
    config, warnings = parse_text(text, config_path)  # raises ConfigError
    try:
        on_disk = read_text(config_path)
    except FileNotFoundError:
        on_disk = ""
    if digest(on_disk) != base_sha:
        raise EditConflict(
            f"{config_path} was changed by something else after the editor loaded it. "
            "Reload to see the current file; your edits are still in the editor.")
    backup = None
    if on_disk:
        backup = _write_backup(config_path, on_disk, now or datetime.now())
    write_text_atomic(config_path, text)
    _prune(config_path)
    return {"config": config, "warnings": warnings, "sha": digest(text),
            "backup": backup.name if backup else None}


def _write_backup(config_path: str, text: str, now: datetime) -> Path:
    folder = backup_dir(config_path)
    folder.mkdir(exist_ok=True)
    stem = Path(config_path).stem
    name = f"{stem}-{now:%Y%m%d-%H%M%S}.yaml"
    path, n = folder / name, 1
    while path.exists():  # two saves in the same second
        path = folder / f"{stem}-{now:%Y%m%d-%H%M%S}-{n}.yaml"
        n += 1
    write_text_atomic(path, text)
    return path


def _prune(config_path: str) -> None:
    for old in list_backups(config_path)[KEEP_BACKUPS:]:
        try:
            (backup_dir(config_path) / old["name"]).unlink()
        except OSError:
            pass


def list_backups(config_path: str) -> list[dict]:
    """This config's backups, newest first."""
    folder = backup_dir(config_path)
    if not folder.is_dir():
        return []
    stem = Path(config_path).stem
    found = [p for p in folder.iterdir()
             if p.is_file() and _BACKUP_RE.match(p.name) and p.name.startswith(stem + "-")]
    found.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return [{"name": p.name, "saved": datetime.fromtimestamp(p.stat().st_mtime).isoformat(
        timespec="seconds"), "bytes": p.stat().st_size} for p in found]


def read_backup(config_path: str, name: str) -> str:
    """The text of one backup. *name* must be a bare file name from the list."""
    if not _BACKUP_RE.match(name or "") or name not in {b["name"] for b in list_backups(config_path)}:
        raise FileNotFoundError(name)
    return read_text(backup_dir(config_path) / name)
