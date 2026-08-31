import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from minerwatch.compat import (
    TimezoneDataMissing,
    missing_dependency_message,
    read_text,
    require_tzdata,
    resolve_path,
)

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - depends on the interpreter
    # Almost always the wrong interpreter rather than a missing install: typing
    # a bare `python -m minerwatch` picks the system Python, which cannot see
    # anything installed into .venv. A raw ModuleNotFoundError sends people
    # off to re-run pip, which does not help.
    raise ModuleNotFoundError(missing_dependency_message("PyYAML", exc.name or "yaml")) from exc

from minerwatch.models import (
    DEFAULT_SLEEP_COMMANDS,
    DEFAULT_WAKE_COMMANDS,
    Command,
    Miner,
    Range,
    Schedule,
    SleepBackend,
    SleepConfig,
    RecoverWith,
    WatchdogConfig,
    Window,
)

logger = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")
_DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_MINUTES_PER_DAY = 1440


class ConfigError(Exception):
    pass


def _parse_time(t: str) -> int:
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _parse_range(text: str) -> Range:
    m = _RANGE_RE.match(text)
    if not m:
        raise ConfigError(f"Invalid range format: {text!r}")
    start = int(m.group(1)) * 60 + int(m.group(2))
    end = int(m.group(3)) * 60 + int(m.group(4))
    if not 0 <= start < _MINUTES_PER_DAY:
        raise ConfigError(f"Range out of bounds: {text!r}")
    # "24:00" is accepted as an end only: it is the natural way to write a
    # window that runs to midnight, and rejecting it forces the confusing
    # "23:59" workaround that silently drops a minute of runtime every day.
    if not 0 <= end <= _MINUTES_PER_DAY:
        raise ConfigError(f"Range out of bounds: {text!r}")
    if start == end:
        raise ConfigError(f"Empty range: {text!r}")
    return Range(start=start, end=end)


def _parse_window(raw: dict) -> Window:
    if not isinstance(raw, dict):
        raise ConfigError(f"Each window must be a mapping, got {type(raw).__name__}")
    days_raw = raw.get("days", [])
    if not isinstance(days_raw, (list, tuple)):
        raise ConfigError(f"'days' must be a list, got {type(days_raw).__name__}")
    days = []
    for d in days_raw:
        if not isinstance(d, str):
            raise ConfigError(f"Unknown day: {d!r}")
        d_clean = d.strip().lower()
        if d_clean not in _DAY_MAP:
            raise ConfigError(f"Unknown day: {d!r}")
        days.append(_DAY_MAP[d_clean])
    ranges_raw = raw.get("ranges", [])
    if not isinstance(ranges_raw, (list, tuple)):
        raise ConfigError(f"'ranges' must be a list, got {type(ranges_raw).__name__}")
    ranges = [_parse_range(r) for r in ranges_raw]
    return Window(days=frozenset(days), ranges=ranges)


def _zoneinfo(name: str, what: str) -> ZoneInfo:
    """Look up a time zone, reporting a missing tz database distinctly.

    On Windows there is no system IANA database, so *every* zone lookup fails
    identically until the ``tzdata`` package is installed. Reporting that as
    "Unknown timezone 'UTC'" sends the operator hunting for a typo that is not
    there, so the two failures are separated.
    """
    try:
        return require_tzdata(name)
    except TimezoneDataMissing:
        raise
    except (ZoneInfoNotFoundError, KeyError, TypeError, ValueError):
        raise ConfigError(f"Unknown {what}: {name!r}")


def _parse_schedule(raw: Any, default_tz: str = "UTC") -> Schedule | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"'schedule' must be a mapping, got {type(raw).__name__}")
    tz_name = raw.get("timezone")
    if tz_name is None:
        tz_name = default_tz
    tz = _zoneinfo(tz_name, "timezone")
    windows_raw = raw.get("windows", [])
    if not isinstance(windows_raw, (list, tuple)):
        raise ConfigError(f"'windows' must be a list, got {type(windows_raw).__name__}")
    windows = [_parse_window(w) for w in windows_raw]
    return Schedule(timezone=tz, windows=windows)


def _resolve_schedule(base: Schedule | None, override: Schedule | None) -> Schedule | None:
    return override if override is not None else base


# ---------------------------------------------------------------------------
# Sleep (software power control) configuration
# ---------------------------------------------------------------------------

_COMMAND_SEP = ":"


def _parse_command(raw: Any) -> Command:
    """Accept either ``"ascset:0,sleep"`` or ``{command: ascset, parameter: "0,sleep"}``.

    The string form is split on the *first* colon only, because cgminer
    parameters routinely contain commas and colons of their own.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ConfigError("Empty command in sleep command list")
        name, sep, param = text.partition(_COMMAND_SEP)
        return Command(command=name.strip(), parameter=param.strip() if sep else None)
    if isinstance(raw, dict):
        name = raw.get("command")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Sleep command entry needs a 'command' string: {raw!r}")
        param = raw.get("parameter")
        if param is not None and not isinstance(param, str):
            param = str(param)
        return Command(command=name.strip(), parameter=param)
    raise ConfigError(f"Invalid sleep command entry: {raw!r}")


def _parse_commands(raw: Any, field: str) -> tuple[Command, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(f"'{field}' must be a list, got {type(raw).__name__}")
    return tuple(_parse_command(item) for item in raw)


def _coerce_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    # YAML already maps yes/no/true/false to booleans; anything else here is a
    # mistake worth reporting rather than silently truthy-testing.
    raise ConfigError(f"'{field}' must be true or false, got {value!r}")


def _coerce_int(value: Any, field: str, minimum: int = 0) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{field}' must be an integer, got {value!r}")
    if out < minimum:
        raise ConfigError(f"'{field}' must be >= {minimum}, got {out}")
    return out


def _parse_watchdog(raw: Any, base: WatchdogConfig) -> WatchdogConfig:
    """Merge a ``watchdog:`` block onto *base*.

    Same global → group → miner chain as sleep and schedules, so an operator
    only has to learn one override rule.
    """
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError(f"'watchdog' must be a mapping, got {type(raw).__name__}")

    known = {"enabled", "fail_after_seconds", "cooldown_seconds", "rate_window_seconds",
             "max_restarts", "recover_with", "reboot_path"}
    unknown = set(raw) - known
    if unknown:
        # A typo here silently leaves a miner on the default policy, which for
        # a restart policy means "reboots hardware on terms nobody chose".
        raise ConfigError(
            f"Unknown watchdog setting(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(sorted(known))}"
        )

    changes: dict[str, Any] = {}
    if "enabled" in raw:
        changes["enabled"] = _coerce_bool(raw["enabled"], "watchdog.enabled")
    for field, minimum in (
        ("fail_after_seconds", 0),
        ("cooldown_seconds", 0),
        ("rate_window_seconds", 1),
        ("max_restarts", 1),
    ):
        if field in raw:
            changes[field] = _coerce_int(raw[field], f"watchdog.{field}", minimum)

    if "recover_with" in raw:
        value = raw["recover_with"]
        try:
            changes["recover_with"] = RecoverWith(str(value).strip().lower())
        except ValueError:
            valid = ", ".join(m.value for m in RecoverWith)
            raise ConfigError(
                f"Unknown watchdog.recover_with '{value}'. Valid: {valid}. "
                f"'cgminer' restarts the mining process over the API port; "
                f"'bitmain_reboot' reboots the control board through the stock web UI; "
                f"'auto' tries the restart first and escalates only when the firmware "
                f"answers that the command does not exist."
            ) from None

    if "reboot_path" in raw:
        path = raw["reboot_path"]
        if not isinstance(path, str) or not path.startswith("/"):
            raise ConfigError(
                f"'watchdog.reboot_path' must be an absolute path beginning with '/', "
                f"got {path!r}"
            )
        changes["reboot_path"] = path

    merged = replace(base, **changes)

    # A reboot is a heavier action than a restart and it cannot be undone, so
    # spacing it like a process restart is a mistake worth refusing rather than
    # documenting. A rebooting S19 is unreachable for minutes and then needs
    # several more to reach full hashrate; at a 600s cooldown the second attempt
    # lands while the first is still booting, which reads as a failure and
    # spends the retry budget on a miner that was recovering normally.
    if merged.recover_with is not RecoverWith.CGMINER and merged.cooldown_seconds < 900:
        raise ConfigError(
            f"'watchdog.cooldown_seconds' is {merged.cooldown_seconds}s, too short for "
            f"recover_with: {merged.recover_with.value}. A rebooted miner is down for "
            f"minutes and then spins up for several more, so a second attempt inside "
            f"900s would fire at a miner that is already recovering. Raise it to at "
            f"least 900 (and rate_window_seconds with it)."
        )
    # The latch needs `max_restarts` attempts alive in the rate window at once,
    # and attempts are `cooldown_seconds` apart, so the span of the last
    # max_restarts-1 gaps has to fit inside the window. When it does not, the
    # oldest attempt is always evicted before the newest arrives, the count
    # never reaches the limit, and a miner that cannot be fixed by restarting -
    # an unreachable one, say - is restarted every cooldown forever instead of
    # being handed to a human. The failure is silent: nothing in the log says
    # "this will never latch".
    span = merged.cooldown_seconds * max(merged.max_restarts - 1, 0)
    if span >= merged.rate_window_seconds:
        raise ConfigError(
            f"'watchdog' can never latch: {merged.max_restarts} attempts "
            f"{merged.cooldown_seconds}s apart span {span}s, which does not fit in "
            f"'rate_window_seconds' ({merged.rate_window_seconds}). The oldest attempt "
            f"is always evicted before the limit is reached, so a miner that restarts "
            f"cannot fix would be restarted forever instead of latching for a human. "
            f"Raise rate_window_seconds above {span}, or lower cooldown_seconds or "
            f"max_restarts."
        )
    return merged


def _parse_sleep(raw: Any, base: SleepConfig) -> SleepConfig:
    """Merge a ``sleep:`` block onto *base*, leaving unspecified fields alone.

    Merge rather than replace so that a group can set credentials once and a
    single miner can override only ``backend`` without having to restate them.
    """
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError(f"'sleep' must be a mapping, got {type(raw).__name__}")

    changes: dict[str, Any] = {}

    if "enabled" in raw:
        changes["enabled"] = _coerce_bool(raw["enabled"], "sleep.enabled")
    if "dry_run" in raw:
        changes["dry_run"] = _coerce_bool(raw["dry_run"], "sleep.dry_run")
    if "backend" in raw:
        value = raw["backend"]
        try:
            changes["backend"] = SleepBackend(str(value).strip().lower())
        except ValueError:
            valid = ", ".join(b.value for b in SleepBackend)
            raise ConfigError(f"Unknown sleep backend {value!r} (expected one of: {valid})")
    for field, minimum in (
        ("cooldown_seconds", 0),
        ("grace_seconds", 0),
        ("max_failures", 1),
        ("http_port", 1),
    ):
        if field in raw:
            changes[field] = _coerce_int(raw[field], f"sleep.{field}", minimum)
    if "http_port" in changes and not 1 <= changes["http_port"] <= 65535:
        raise ConfigError(f"'sleep.http_port' out of range 1-65535: {changes['http_port']}")
    if "api_port" in raw and raw["api_port"] is not None:
        port = _coerce_int(raw["api_port"], "sleep.api_port", 1)
        if not 1 <= port <= 65535:
            raise ConfigError(f"'sleep.api_port' out of range 1-65535: {port}")
        changes["api_port"] = port
    if "timeout_seconds" in raw:
        try:
            timeout = float(raw["timeout_seconds"])
        except (TypeError, ValueError):
            raise ConfigError(f"'sleep.timeout_seconds' must be a number, got {raw['timeout_seconds']!r}")
        if timeout <= 0:
            raise ConfigError("'sleep.timeout_seconds' must be positive")
        changes["timeout_seconds"] = timeout
    if "http_scheme" in raw:
        scheme = str(raw["http_scheme"]).strip().lower()
        if scheme not in ("http", "https"):
            raise ConfigError(f"'sleep.http_scheme' must be http or https, got {scheme!r}")
        changes["http_scheme"] = scheme
    for field in ("username", "password"):
        if field in raw:
            changes[field] = str(raw[field])
    for field in ("normal_value", "sleep_value"):
        if field in raw:
            changes[field] = _coerce_int(raw[field], f"sleep.{field}", 0)
    if "post_format" in raw:
        fmt = str(raw["post_format"]).strip().lower()
        if fmt not in ("json", "form"):
            raise ConfigError(f"'sleep.post_format' must be json or form, got {fmt!r}")
        changes["post_format"] = fmt
    if "write_profile" in raw:
        profile = str(raw["write_profile"]).strip().lower()
        if profile not in ("auto", "mirror", "browser"):
            raise ConfigError(
                f"'sleep.write_profile' must be auto, mirror or browser, got {profile!r}"
            )
        changes["write_profile"] = profile
    if "content_type" in raw:
        value = raw["content_type"]
        changes["content_type"] = None if value is None else str(value)
    if "mode_key" in raw:
        value = raw["mode_key"]
        changes["mode_key"] = None if value is None else str(value).strip() or None
    if "sleep_commands" in raw:
        changes["sleep_commands"] = _parse_commands(raw["sleep_commands"], "sleep.sleep_commands")
    if "wake_commands" in raw:
        changes["wake_commands"] = _parse_commands(raw["wake_commands"], "sleep.wake_commands")

    merged = replace(base, **changes)
    if merged.enabled and merged.backend is SleepBackend.NONE:
        raise ConfigError("sleep.enabled is true but sleep.backend is 'none'")
    if merged.enabled and not merged.sleep_commands and merged.backend is SleepBackend.CGMINER:
        raise ConfigError("sleep.sleep_commands is empty for the cgminer backend")
    return merged


#: Ports that answer HTTP on an Antminer, not the cgminer JSON API. Polling one
#: of these produces a bare timeout, which reads as "unreachable" and sends the
#: operator hunting for a network fault that is not there.
_WEB_UI_PORTS = {80, 443, 8080, 8443}
#: The conventional cgminer/bmminer API port.
CGMINER_PORT = 4028


def lint_miners(miners: dict[str, Miner]) -> list[str]:
    """Report configurations that parse cleanly but cannot work.

    These are not errors — a deliberately unusual setup is allowed — but each
    one has a failure mode that looks like something else, so saying nothing
    costs more than a warning does.
    """
    notes: list[str] = []

    for m in miners.values():
        if m.port in _WEB_UI_PORTS:
            note = (
                f"{m.id}: port {m.port} is a web-UI port. 'port' is the cgminer API "
                f"that MinerWatch polls, normally {CGMINER_PORT}; polling a web "
                f"server times out and reports the miner as unreachable."
            )
            if m.sleep.backend is SleepBackend.BITMAIN_HTTP and m.sleep.enabled:
                note += (
                    f" The bitmain_http backend has its own 'sleep.http_port' "
                    f"(currently {m.sleep.http_port}), so set 'port: {CGMINER_PORT}'."
                )
            notes.append(note)

    seen: dict[tuple[str, int], str] = {}
    for m in miners.values():
        key = (m.host, m.port)
        if key in seen:
            notes.append(
                f"{m.id}: same address as {seen[key]} ({m.host}:{m.port}) - "
                f"both entries poll one device."
            )
        else:
            seen[key] = m.id

    return notes


def load_config(path: str) -> tuple[int, str, str, dict[str, Miner]]:
    """Parse *path* into ``(poll_interval, db_path, default_timezone, miners)``.

    ``db_path`` comes back resolved against the config file's directory so the
    database does not follow the working directory around — which on Windows is
    ``C:\\Windows\\System32`` when the poller is started by Task Scheduler.
    """
    # Explicit UTF-8: the locale default is cp1252 on the Windows host, which
    # mangles or rejects any non-ASCII miner name or comment in the file.
    raw = yaml.safe_load(read_text(path))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping, got {type(raw).__name__}")

    poll_interval = raw.get("poll_interval_seconds", 15)
    try:
        poll_interval = int(poll_interval)
    except (TypeError, ValueError):
        raise ConfigError(f"Invalid poll_interval_seconds: {poll_interval!r}")
    if poll_interval < 1:
        raise ConfigError(f"poll_interval_seconds must be >= 1, got {poll_interval}")

    db_path = raw.get("db_path", "minerwatch.db")
    default_tz = raw.get("default_timezone", "UTC")
    if default_tz is None:
        default_tz = "UTC"

    _zoneinfo(default_tz, "default timezone")

    # Global sleep defaults, then group, then miner — same override chain as
    # schedules, so operators only have to learn one rule.
    global_sleep = _parse_sleep(raw.get("sleep"), SleepConfig())
    global_watchdog = _parse_watchdog(raw.get("watchdog"), WatchdogConfig())

    groups = raw.get("groups", {}) or {}
    if not isinstance(groups, dict):
        raise ConfigError(f"'groups' must be a mapping, got {type(groups).__name__}")

    # Parse group schedules once and cache them
    group_schedules: dict[str, Schedule | None] = {}
    group_sleep: dict[str, SleepConfig] = {}
    group_watchdog: dict[str, WatchdogConfig] = {}
    for gname, gdata in groups.items():
        if gdata is not None and not isinstance(gdata, dict):
            raise ConfigError(f"Group {gname!r} must be a mapping, got {type(gdata).__name__}")
        if gdata is not None and "schedule" in gdata:
            group_schedules[gname] = _parse_schedule(gdata["schedule"], default_tz)
        else:
            group_schedules[gname] = None
        group_sleep[gname] = _parse_sleep((gdata or {}).get("sleep"), global_sleep)
        try:
            group_watchdog[gname] = _parse_watchdog((gdata or {}).get("watchdog"), global_watchdog)
        except ConfigError as exc:
            raise ConfigError(f"Group {gname!r}: {exc}")

    miners_raw = raw.get("miners", []) or []
    if not isinstance(miners_raw, (list, tuple)):
        raise ConfigError(f"'miners' must be a list, got {type(miners_raw).__name__}")
    seen_ids: set[str] = set()
    miners: dict[str, Miner] = {}

    for m in miners_raw:
        if not isinstance(m, dict):
            raise ConfigError(f"Each miner must be a mapping, got {type(m).__name__}")
        mid = m.get("id")
        if not isinstance(mid, str) or not mid.strip():
            raise ConfigError(f"Miner entry is missing a string 'id': {m!r}")
        if mid in seen_ids:
            raise ConfigError(f"Duplicate miner id: {mid!r}")
        seen_ids.add(mid)

        port_raw = m.get("port", 4028)
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            raise ConfigError(f"Invalid port {port_raw!r} for miner {mid!r}")
        if not (1 <= port <= 65535):
            raise ConfigError(f"Port {port} out of range 1-65535 for miner {mid!r}")

        group_name = m.get("group")
        if group_name is not None and group_name not in groups:
            raise ConfigError(f"Unknown group {group_name!r} for miner {mid!r}")

        effective = None  # effective schedule
        base_sleep = global_sleep
        base_watchdog = global_watchdog
        if group_name is not None:
            effective = group_schedules.get(group_name)
            base_sleep = group_sleep.get(group_name, global_sleep)
            base_watchdog = group_watchdog.get(group_name, global_watchdog)
        if "schedule" in m:
            miner_schedule = _parse_schedule(m.get("schedule"), default_tz)
            effective = _resolve_schedule(effective, miner_schedule)

        try:
            sleep_cfg = _parse_sleep(m.get("sleep"), base_sleep)
        except ConfigError as exc:
            raise ConfigError(f"Miner {mid!r}: {exc}")

        try:
            watchdog_cfg = _parse_watchdog(m.get("watchdog"), base_watchdog)
        except ConfigError as exc:
            raise ConfigError(f"Miner {mid!r}: {exc}")

        if effective is None:
            # ASCII only: the Windows console runs on a legacy code page by
            # default and a non-ASCII log record can raise UnicodeEncodeError.
            logger.warning(
                "Miner %s has no schedule - it will never be considered working time", mid
            )
            if sleep_cfg.enabled:
                logger.warning(
                    "Miner %s has sleep enabled but no schedule - "
                    "automatic sleep/wake is inactive; use the sleep/wake commands", mid
                )

        miner = Miner(
            id=mid,
            host=m.get("host", "127.0.0.1"),
            port=port,
            group=group_name,
            schedule=effective,
            sleep=sleep_cfg,
            watchdog=watchdog_cfg,
        )
        miners[mid] = miner

    for note in lint_miners(miners):
        logger.warning("%s", note)

    return poll_interval, resolve_path(str(db_path), path), default_tz, miners
