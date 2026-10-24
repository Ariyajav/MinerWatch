"""Power maintenance windows: periods in which MinerWatch watches but never acts.

While the power supply is being worked on, every miner on it will stop,
disappear and come back, and every one of those readings is expected. Left to
itself the supervisor treats them as failures: the watchdog spends its restart
budget on miners that have no power and latches the lot (which is exactly what
happened to ten miners at once on 12 Oct 2026), the sleep controller counts
failed commands toward its own latch, and the dashboard emails about all of it.

Inside a window the poller still reads each miner and records what it saw, so
the history shows the outage, but:

* the sleep controller and the watchdog are not consulted at all - no sleep,
  wake or restart is sent and no latch can be set;
* the poll is recorded as ``maintenance`` instead of ``alert``, and that action
  resets the watchdog's failure clock, so a miner that is still down when the
  window ends gets the full ``fail_after_seconds`` to boot before anything is
  sent to it;
* the dashboard sends no email about miners in maintenance.

Windows come from two places:

``maintenance:`` in ``miners.yaml``
    Planned work, one-off (``start``/``end``) or recurring (``days``/``ranges``,
    the same syntax as a schedule). Read when the supervisor starts, like the
    rest of that file.
``maintenance.json`` beside ``miners.yaml``
    Started and stopped by hand from ``minerwatch maintenance`` or the
    dashboard. The supervisor re-reads this file whenever it changes, so unlike
    ``clear-attention`` it takes effect on the next poll without a restart.

Either kind covers the whole fleet, or only the ``groups`` (farms) and
``miners`` it names.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from minerwatch.compat import read_text, resolve_path, write_text_atomic
from minerwatch.models import Miner, Window

logger = logging.getLogger(__name__)

#: File name of the hand-started windows, resolved beside the config.
MANUAL_FILE = "maintenance.json"

#: Ended hand-started windows are kept this long so `maintenance list` and the
#: dashboard can show what happened, then dropped.
KEEP_ENDED = timedelta(days=7)

#: Poll action written for a non-mining reading inside a window.
MAINTENANCE_ACTION = "maintenance"


@dataclass(frozen=True)
class MaintenanceWindow:
    """One maintenance period and the miners it covers.

    Exactly one of the two time forms is used: a one-off ``start``/``end``
    (aware datetimes; ``end`` may be ``None`` for "until stopped"), or a weekly
    ``windows`` list evaluated in ``timezone``.
    """

    name: str
    groups: frozenset[str] = frozenset()
    miners: frozenset[str] = frozenset()
    start: datetime | None = None
    end: datetime | None = None
    timezone: ZoneInfo | None = None
    windows: tuple[Window, ...] = ()
    #: ``config`` (miners.yaml) or ``manual`` (maintenance.json).
    source: str = "config"
    #: Identifier of a manual window, used to stop it.
    id: str | None = None
    reason: str = ""
    started_by: str = ""

    @property
    def fleet_wide(self) -> bool:
        return not self.groups and not self.miners

    @property
    def recurring(self) -> bool:
        return bool(self.windows)

    def covers(self, miner: Miner) -> bool:
        if self.fleet_wide:
            return True
        return miner.id in self.miners or (miner.group is not None and miner.group in self.groups)

    def active(self, now: datetime) -> bool:
        if self.recurring:
            return _in_weekly(self.windows, self.timezone or ZoneInfo("UTC"), now)
        if self.start is None or now < self.start:
            return False
        return self.end is None or now < self.end

    def scope_text(self) -> str:
        if self.fleet_wide:
            return "whole fleet"
        parts = []
        if self.groups:
            parts.append(("group " if len(self.groups) == 1 else "groups ")
                         + ", ".join(sorted(self.groups)))
        if self.miners:
            parts.append(", ".join(sorted(self.miners)))
        return "; ".join(parts)

    def when_text(self) -> str:
        if self.recurring:
            from minerwatch.cli import _fmt_days, _fmt_hhmm

            rows = []
            for w in self.windows:
                ranges = ", ".join(f"{_fmt_hhmm(r.start)}-{_fmt_hhmm(r.end)}" for r in w.ranges)
                rows.append(f"{_fmt_days(w.days)} {ranges}")
            return "; ".join(rows) + f" ({self.timezone})"
        start = _fmt_local(self.start, self.timezone)
        if self.end is None:
            return f"from {start} until stopped"
        return f"{start} to {_fmt_local(self.end, self.timezone)}"

    def to_public(self, now: datetime) -> dict:
        """JSON-able description for the dashboard."""
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "scope": self.scope_text(),
            "fleet_wide": self.fleet_wide,
            "groups": sorted(self.groups),
            "miners": sorted(self.miners),
            "when": self.when_text(),
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "recurring": self.recurring,
            "active": self.active(now),
            "reason": self.reason,
            "started_by": self.started_by,
        }


def _in_weekly(windows: Iterable[Window], tz: ZoneInfo, now: datetime) -> bool:
    # Same rule as schedule.is_working_time, including ranges that cross
    # midnight belonging to the day they start on.
    local = now.astimezone(tz)
    t = local.hour * 60 + local.minute
    weekday = local.weekday()
    for window in windows:
        for r in window.ranges:
            if r.crosses_midnight:
                if weekday in window.days and t >= r.start:
                    return True
                if ((weekday - 1) % 7) in window.days and t < r.end:
                    return True
            elif weekday in window.days and r.start <= t < r.end:
                return True
    return False


def _fmt_local(ts: datetime | None, tz: ZoneInfo | None) -> str:
    if ts is None:
        return "?"
    shown = ts.astimezone(tz) if tz is not None else ts
    return shown.strftime("%Y-%m-%d %H:%M") + (f" {tz}" if tz is not None else " UTC")


# ---------------------------------------------------------------------------
# miners.yaml
# ---------------------------------------------------------------------------

_KNOWN_KEYS = {"name", "groups", "miners", "start", "end", "days", "ranges", "timezone"}


def parse_datetime(value: Any, tz: ZoneInfo, what: str) -> datetime:
    """Read a config or CLI time. A time without an offset is local to *tz*.

    YAML turns ``2026-10-26 09:00:00`` into a datetime but leaves
    ``2026-10-26 09:00`` as a string, so both are accepted.
    """
    from minerwatch.config import ConfigError

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        raise ConfigError(f"{what} needs a time as well as a date, got {value}")
    elif isinstance(value, str):
        text = value.strip().replace("T", " ")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ConfigError(
                f"{what} must look like 2026-10-26 09:00, got {value!r}"
            ) from None
    else:
        raise ConfigError(f"{what} must be a date and time, got {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


def parse_config(raw: Any, default_tz: str, groups: Iterable[str],
                 miner_ids: Iterable[str]) -> list[MaintenanceWindow]:
    """Parse the top-level ``maintenance:`` list of ``miners.yaml``."""
    from minerwatch.config import ConfigError, _parse_window, _zoneinfo

    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(f"'maintenance' must be a list, got {type(raw).__name__}")
    known_groups, known_miners = set(groups), set(miner_ids)
    out: list[MaintenanceWindow] = []
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise ConfigError(f"maintenance entry {i} must be a mapping")
        name = str(entry.get("name") or f"maintenance {i}").strip()
        label = f"maintenance {name!r}"
        unknown = set(entry) - _KNOWN_KEYS
        if unknown:
            raise ConfigError(
                f"{label}: unknown setting(s) {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(_KNOWN_KEYS))}"
            )
        target_groups = _names(entry.get("groups"), f"{label}: 'groups'")
        target_miners = _names(entry.get("miners"), f"{label}: 'miners'")
        # A misspelt farm name would otherwise cover nothing, and the fleet
        # would be restarted and latched during the work it was meant to cover.
        bad = sorted(target_groups - known_groups)
        if bad:
            raise ConfigError(f"{label}: unknown group(s) {', '.join(bad)}")
        bad = sorted(target_miners - known_miners)
        if bad:
            raise ConfigError(f"{label}: unknown miner(s) {', '.join(bad)}")

        tz = _zoneinfo(entry.get("timezone") or default_tz, "timezone")
        one_off = "start" in entry or "end" in entry
        weekly = "days" in entry or "ranges" in entry
        if one_off == weekly:
            raise ConfigError(
                f"{label}: give either start and end (one-off) or days and ranges "
                f"(every week), not {'both' if one_off else 'neither'}"
            )
        if one_off:
            if "start" not in entry or "end" not in entry:
                raise ConfigError(f"{label}: a one-off window needs both start and end")
            start = parse_datetime(entry["start"], tz, f"{label}: 'start'")
            end = parse_datetime(entry["end"], tz, f"{label}: 'end'")
            if end <= start:
                raise ConfigError(f"{label}: end must be after start")
            out.append(MaintenanceWindow(name=name, groups=target_groups,
                                         miners=target_miners, start=start, end=end,
                                         timezone=tz))
        else:
            window = _parse_window({"days": entry.get("days", []),
                                    "ranges": entry.get("ranges", [])})
            if not window.days or not window.ranges:
                raise ConfigError(f"{label}: a weekly window needs both days and ranges")
            out.append(MaintenanceWindow(name=name, groups=target_groups,
                                         miners=target_miners, timezone=tz,
                                         windows=(window,)))
    return out


def _names(value: Any, what: str) -> frozenset[str]:
    from minerwatch.config import ConfigError

    if value is None:
        return frozenset()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{what} must be a list of names")
    return frozenset(v.strip() for v in value if v.strip())


# ---------------------------------------------------------------------------
# maintenance.json (hand-started windows)
# ---------------------------------------------------------------------------

def manual_path(config_path: str) -> str:
    return resolve_path(MANUAL_FILE, os.path.abspath(config_path))


def _from_record(rec: dict) -> MaintenanceWindow:
    start = datetime.fromisoformat(rec["start"])
    end = datetime.fromisoformat(rec["end"]) if rec.get("end") else None
    return MaintenanceWindow(
        name=str(rec.get("name") or "manual maintenance"),
        groups=frozenset(rec.get("groups") or ()),
        miners=frozenset(rec.get("miners") or ()),
        start=start, end=end, source="manual", id=str(rec["id"]),
        reason=str(rec.get("reason") or ""), started_by=str(rec.get("started_by") or ""),
    )


def _read_records(path: str) -> list[dict]:
    if not Path(path).exists():
        return []
    data = json.loads(read_text(path))
    records = data.get("windows", []) if isinstance(data, dict) else []
    return [r for r in records if isinstance(r, dict) and r.get("id") and r.get("start")]


def load_manual(path: str | None) -> list[MaintenanceWindow]:
    """Every hand-started window in *path*. Raises on a damaged file."""
    if not path:
        return []
    return [_from_record(r) for r in _read_records(path)]


def _write_records(path: str, records: list[dict], now: datetime) -> None:
    keep = []
    for r in records:
        end = r.get("end")
        if end and now - datetime.fromisoformat(end) > KEEP_ENDED:
            continue
        keep.append(r)
    write_text_atomic(path, json.dumps({"windows": keep}, indent=2) + "\n")


def start_manual(path: str, *, miners: dict[str, Miner], groups: Iterable[str] = (),
                 miner_ids: Iterable[str] = (), duration: timedelta | None = None,
                 until: datetime | None = None, name: str = "", reason: str = "",
                 started_by: str = "", now: datetime | None = None) -> MaintenanceWindow:
    """Start a window now. Raises ValueError on an unknown target or a bad time."""
    now = now or datetime.now(timezone.utc)
    groups, miner_ids = sorted(set(groups)), sorted(set(miner_ids))
    known_groups = {m.group for m in miners.values() if m.group}
    bad = [g for g in groups if g not in known_groups]
    if bad:
        raise ValueError(f"unknown group(s): {', '.join(bad)} "
                         f"(known: {', '.join(sorted(known_groups)) or 'none'})")
    bad = [m for m in miner_ids if m not in miners]
    if bad:
        raise ValueError(f"unknown miner(s): {', '.join(bad)}")
    if duration is not None and until is not None:
        raise ValueError("give a duration or an end time, not both")
    end = until if until is not None else (now + duration if duration is not None else None)
    if end is not None and end <= now:
        raise ValueError("the end time is already in the past")
    record = {
        "id": secrets.token_hex(3),
        "name": name.strip() or "manual maintenance",
        "groups": groups,
        "miners": miner_ids,
        "start": now.isoformat(),
        "end": end.isoformat() if end else None,
        "reason": reason.strip(),
        "started_by": started_by,
    }
    records = _read_records(path)
    records.append(record)
    _write_records(path, records, now)
    return _from_record(record)


def stop_manual(path: str, window_id: str | None = None,
                now: datetime | None = None) -> list[MaintenanceWindow]:
    """End one hand-started window now, or every active one when *window_id* is None."""
    now = now or datetime.now(timezone.utc)
    records = _read_records(path)
    stopped = []
    for r in records:
        if window_id is not None and r["id"] != window_id:
            continue
        if not _from_record(r).active(now):
            continue
        r["end"] = now.isoformat()
        stopped.append(_from_record(r))
    if window_id is not None and not stopped:
        if any(r["id"] == window_id for r in records):
            raise ValueError(f"maintenance {window_id} has already ended")
        raise ValueError(f"no hand-started maintenance with id {window_id!r}")
    if stopped:
        _write_records(path, records, now)
    return stopped


@dataclass
class ManualWindows:
    """Hand-started windows, re-read from disk only when the file changes.

    A damaged file must not stop supervision, and must not silently end a
    window either. The last good copy is kept and the problem is logged once
    per change of the file.
    """

    path: str | None
    _stamp: tuple | None = None
    _windows: list[MaintenanceWindow] = field(default_factory=list)
    error: str | None = None

    def get(self) -> list[MaintenanceWindow]:
        if not self.path:
            return []
        try:
            st = os.stat(self.path)
            stamp: tuple | None = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        except OSError as exc:  # pragma: no cover - filesystem dependent
            self.error = str(exc)
            return self._windows
        if stamp == self._stamp:
            return self._windows
        self._stamp = stamp
        try:
            self._windows = load_manual(self.path) if stamp else []
            self.error = None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.error = f"{self.path} could not be read: {exc}"
            logger.error("maintenance: %s - keeping the previous windows", self.error)
        return self._windows


def active_for(miner: Miner, now: datetime,
               manual: Iterable[MaintenanceWindow] = ()) -> MaintenanceWindow | None:
    """The window covering *miner* at *now*, if any. Config windows first."""
    for window in (*miner.maintenance, *manual):
        if window.covers(miner) and window.active(now):
            return window
    return None


def all_windows(miners: dict[str, Miner],
                manual: Iterable[MaintenanceWindow] = ()) -> list[MaintenanceWindow]:
    """Every distinct window from the config, then the hand-started ones."""
    seen: list[MaintenanceWindow] = []
    for m in miners.values():
        for w in m.maintenance:
            if w not in seen:
                seen.append(w)
    return seen + list(manual)


_DURATION_RE = re.compile(r"^\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*$", re.I)


def parse_duration(text: str) -> timedelta:
    """``90`` (minutes), ``90m``, ``2h``, ``1h30m`` or ``1d``."""
    text = (text or "").strip()
    if text.isdigit():
        minutes = int(text)
    else:
        m = _DURATION_RE.match(text)
        if not text or not m or not any(m.groups()):
            raise ValueError(f"cannot read duration {text!r}; use e.g. 90m, 2h or 1h30m")
        d, h, mi = (int(g or 0) for g in m.groups())
        minutes = d * 1440 + h * 60 + mi
    if minutes <= 0:
        raise ValueError("the duration must be more than zero")
    return timedelta(minutes=minutes)
