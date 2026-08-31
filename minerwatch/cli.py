"""Command line interface.

Subcommands:

``run``
    The supervision loop: poll every miner, drive scheduled sleep/wake, and
    let the watchdog restart genuine failures.
``sleep`` / ``wake``
    One-shot manual power control for a named miner, ignoring the schedule.
``status``
    Print the last known state of each miner and whether MinerWatch believes
    it is asleep or latched for attention.
``clear-attention``
    Release a watchdog or sleep failure latch.

Everything defaults to a rehearsal: no restart and no sleep command reaches a
miner unless ``--live`` (or the config's ``dry_run: false``) says so.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event

from minerwatch.compat import (
    TimezoneDataMissing,
    configure_console,
    install_signal_handlers,
)
from minerwatch.config import ConfigError, lint_miners, load_config
from minerwatch.backends import get_backend
from minerwatch.models import RecoverWith, SleepBackend, State
from minerwatch.poller import Poller
from minerwatch.schedule import is_working_time
from minerwatch.sleeper import SleepController
from minerwatch.store import init_db, is_needs_attention, last_action_in, last_state
from minerwatch.watchdog import Watchdog

logger = logging.getLogger("minerwatch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minerwatch",
        description="Monitor, schedule, and software-sleep Antminer devices.",
    )
    parser.add_argument(
        "-c", "--config", default="miners.yaml", help="Path to miners.yaml (default: miners.yaml)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        help="Also append logs to PATH, rotating at 5 MB (keeps 5). "
             "Required for unattended runs: a scheduled task has no console.",
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run the poll/schedule/watchdog loop")
    run.add_argument(
        "--live",
        action="store_true",
        help="Actually send restart and sleep/wake commands (default: rehearse only)",
    )
    run.add_argument(
        "--live-watchdog", action="store_true", help="Send restarts, but rehearse sleep/wake"
    )
    run.add_argument(
        "--live-sleep", action="store_true", help="Send sleep/wake, but rehearse restarts"
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Force a rehearsal even where the config sets sleep.dry_run: false",
    )
    run.add_argument(
        "--once", action="store_true", help="Run a single poll cycle and exit (useful for testing)"
    )

    for name, help_text in (("sleep", "Put a miner to sleep now"), ("wake", "Wake a miner now")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("miner", help="Miner id from the config, or 'all'")
        p.add_argument(
            "--live",
            action="store_true",
            help="Actually send the command (default: rehearse only)",
        )

    sub.add_parser("status", help="Show the last known state of every miner")

    cfg = sub.add_parser(
        "config",
        help="Show the resolved settings for every miner, after group inheritance",
    )
    cfg.add_argument(
        "--hours",
        action="store_true",
        help="Also draw a 7-day x 24-hour running/asleep map per schedule",
    )

    sub.add_parser(
        "check",
        help="Read-only probe of each miner's sleep backend: reachability and credentials",
    )

    diag = sub.add_parser(
        "diagnose",
        help="Work out which request shape a stubborn bitmain_http miner accepts",
    )
    diag.add_argument("miner", help="Miner id to test against")

    clear = sub.add_parser("clear-attention", help="Release a failure latch")
    clear.add_argument("miner", help="Miner id")

    hist = sub.add_parser(
        "history",
        help="Replay what MinerWatch saw and decided for a miner",
    )
    hist.add_argument("miner", help="Miner id, or 'all'")
    hist.add_argument(
        "--hours", type=float, default=24.0,
        help="How far back to look (default: 24)",
    )
    hist.add_argument(
        "--decisions", action="store_true",
        help="Only the controllers' decisions - hide the routine per-poll readings",
    )
    hist.add_argument(
        "--limit", type=int, default=5000,
        help="Most recent N rows to read (default: 5000)",
    )

    return parser


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
#: Rotate at 5 MB and keep 5 files: at a 15-second poll interval a fleet
#: generates a few MB a month, so this is roughly half a year of history with a
#: hard ceiling on disk use.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5


def _setup_logging(verbose: bool, log_file: str | None = None) -> None:
    # Force UTF-8 on the console before the first log record: a fresh cmd.exe
    # runs on a legacy code page and a non-ASCII character would raise
    # UnicodeEncodeError from inside the logging handler.
    configure_console()
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)

    if not log_file:
        return

    # A scheduled task has no console, so without this an unattended run leaves
    # nothing to diagnose from except the events database.
    path = Path(log_file).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",  # never the Windows locale codepage
            delay=True,
        )
    except OSError as exc:
        # Losing the log file must not stop the fleet from being supervised.
        logging.getLogger("minerwatch").error(
            "Could not open log file %s: %s (continuing without it)", path, exc
        )
        return
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.setLevel(level)
    logging.getLogger().addHandler(handler)


def _select(miners: dict, name: str) -> list:
    if name == "all":
        return list(miners.values())
    miner = miners.get(name)
    if miner is None:
        raise SystemExit(f"Unknown miner id: {name!r} (known: {', '.join(sorted(miners))})")
    return [miner]


def cmd_run(args, config, conn) -> int:
    poll_interval, _, _, miners = config
    stop_event = Event()

    def handle_signal(signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        stop_event.set()

    install_signal_handlers(handle_signal)

    watchdog_dry = not (args.live or args.live_watchdog)
    # Sleep has a per-miner setting in the config, so the CLI only *overrides*
    # it: --dry-run forces a rehearsal, --live/--live-sleep force actuation,
    # and neither flag leaves each miner's configured dry_run in charge.
    if args.dry_run:
        sleep_dry = True
    elif args.live or args.live_sleep:
        sleep_dry = False
    else:
        sleep_dry = None

    sleep_mode = {True: "dry-run (forced)", False: "LIVE", None: "per-config"}[sleep_dry]
    logger.info(
        "Starting: %d miner(s), poll every %ds, watchdog=%s, sleep=%s",
        len(miners),
        poll_interval,
        "dry-run" if watchdog_dry else "LIVE",
        sleep_mode,
    )
    if sleep_dry is None:
        live = [m.id for m in miners.values() if m.sleep.enabled and not m.sleep.dry_run]
        if live:
            logger.warning("Sleep is LIVE for: %s", ", ".join(live))

    poller = Poller(config, conn, stop_event, dry_run=watchdog_dry, sleep_dry_run=sleep_dry)

    async def _main() -> None:
        if args.once:
            await asyncio.gather(
                *(poller._poll_one(m) for m in miners.values()), return_exceptions=True
            )
            return
        await poller.run()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        # Expected on Ctrl+C; the signal handler has already set stop_event.
        pass
    logger.info("Shutdown complete")
    return 0


def cmd_power(args, config, conn, to_sleep: bool) -> int:
    _, _, _, miners = config
    targets = _select(miners, args.miner)
    controller = SleepController(conn, miners, dry_run=not args.live)
    label = "sleep" if to_sleep else "wake"

    async def _main() -> int:
        failures = 0
        for miner in targets:
            if miner.sleep.backend is SleepBackend.NONE or not miner.sleep.enabled:
                print(f"{miner.id}: skipped (no sleep backend configured)")
                continue
            if to_sleep:
                ok, detail = await controller.sleep_now(miner)
            else:
                ok, detail = await controller.wake_now(miner)
            print(f"{miner.id}: {label} {'OK' if ok else 'FAILED'} - {detail}")
            failures += 0 if ok else 1
        return 1 if failures else 0

    return asyncio.run(_main())


#: Actions written by the poller itself, as opposed to the watchdog or the
#: sleep controller. Only these carry the connection error.
POLL_ACTIONS = ("none", "alert", "expected_off")


def _fmt_hashrate(event) -> str:
    """Render GH/s at a scale an operator reads at a glance.

    A modern S19 is ~95,000 GH/s, which is unreadable as a raw number; TH/s is
    what the miner's own page shows. Zero is printed as "0" rather than blank,
    because "reporting zero" and "did not report" are different facts and the
    difference is what tells a real sleep from a low-power mode.
    """
    ghs = getattr(event, "ghs", None) if event is not None else None
    if ghs is None:
        return "-"
    if ghs >= 1000:
        return f"{ghs / 1000:,.1f} TH/s"
    if ghs == 0:
        return "0"
    return f"{ghs:,.0f} GH/s"


def cmd_status(args, config, conn) -> int:
    _, _, _, miners = config
    controller = SleepController(conn, miners)
    now = datetime.now(timezone.utc)

    problems: list[tuple] = []
    # Widen the id column to fit the fleet rather than truncating or wrapping.
    w = max([len(m.id) for m in miners.values()] + [len("MINER")]) + 1
    header = (f"{'MINER':<{w}} {'STATE':<12} {'HASHRATE':>11} {'WINDOW':<8} "
              f"{'POWER':<10} {'ATTENTION':<10} LAST SEEN")
    print(header)
    print("-" * len(header))
    for miner in miners.values():
        # Every column on this line must describe the same moment. The poll
        # event is the only row that records an *observation*; the controllers
        # write rows too, carrying whatever state they assumed at the time.
        # Taking the state from the newest row of any kind and the hashrate
        # from the newest poll produced lines reading "unreachable ... 74.5
        # TH/s", which is not a state a miner can be in and tells an operator
        # nothing about which half to believe.
        #
        # It also keeps the connection error attached to the failure that
        # caused it, rather than to a controller's description of its own
        # decision.
        poll = last_action_in(conn, miner.id, POLL_ACTIONS)
        last = poll or last_state(conn, miner.id)
        state = last.state if last else "unknown"
        seen = last.ts if last else "never"
        window = "open" if is_working_time(miner, now) else "closed"
        if not miner.sleep.enabled:
            power = "manual"
        elif controller.is_asleep(miner.id):
            power = "asleep"
        else:
            power = "awake"
        flags = []
        if is_needs_attention(conn, miner.id):
            flags.append("watchdog")
        if controller.needs_attention(miner.id):
            flags.append("sleep")
        print(
            f"{miner.id:<{w}} {state:<12} {_fmt_hashrate(poll):>11} {window:<8} "
            f"{power:<10} {(','.join(flags) or '-'):<10} {seen}"
        )
        if poll is not None and poll.reason and poll.state != State.MINING.value:
            problems.append((miner, poll.reason))

    # The reason is the whole diagnosis for an unreachable miner, and it was
    # previously only visible by querying the database by hand. Group identical
    # reasons: a fleet that fails the same way has one cause, not twelve.
    if problems:
        # Group by *diagnosis*, not by the raw message: several of these embed
        # the address or port, so twelve miners failing one way would otherwise
        # print twelve near-identical paragraphs and hide the single cause.
        groups: dict[str, list] = {}
        raw_by_group: dict[str, list[str]] = {}
        for miner, reason in problems:
            key = _diagnose(reason) or reason
            groups.setdefault(key, []).append(miner)
            seen = raw_by_group.setdefault(key, [])
            if reason not in seen:
                seen.append(reason)
        print()
        print("WHY:")
        for key, members in groups.items():
            ids = ", ".join(m.id for m in members)
            count = f" ({len(members)} miners)" if len(members) > 1 else ""
            print(f"  {ids}{count}")
            print(f"    {key}")
            raws = raw_by_group[key]
            if raws != [key]:
                shown = raws[0] if len(raws) == 1 else f"{raws[0]} (and {len(raws) - 1} more)"
                print(f"    reported as: {shown}")
    return 0


#: Signatures worth translating. The reason text comes from the OS or from our
#: own classifier, and each of these means something specific about the miner
#: that is not obvious from the message itself.
def _diagnose(reason: str) -> str | None:
    r = reason.lower()
    if "invalid json" in r or "unparseable" in r:
        return (
            "the miner accepted the connection then sent nothing - usually 'api-allow' "
            "on the miner does not include this host. Add this PC's IP (or 0/0) to "
            "api-allow in the miner's API settings."
        )
    # Windows says "actively refused" (WinError 10061); asyncio on Linux says
    # "Connect call failed". Same condition, different words.
    if "refused" in r or "10061" in r or "connect call failed" in r:
        return (
            "nothing is listening on that port - the API is switched off "
            "('api-listen'), or the miner is not running its mining process."
        )
    if "timeout" in r or "timed out" in r or "10060" in r:
        return (
            "no reply at all - wrong IP, the miner is powered down, or a firewall "
            "is dropping the connection. Try pinging it."
        )
    if "no route" in r or "unreachable" in r or "10065" in r:
        return "the network cannot reach that address - check the IP and the subnet."
    if "reset" in r or "10054" in r:
        return "the miner closed the connection mid-reply - it may be rebooting."
    return None


_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _fmt_days(days: frozenset) -> str:
    """Render a day set as a compact range list: mon-fri, or sat,sun."""
    if not days:
        return "never"
    if len(days) == 7:
        return "every day"
    out, run = [], []
    for d in sorted(days):
        if run and d == run[-1] + 1:
            run.append(d)
        else:
            if run:
                out.append(run)
            run = [d]
    out.append(run)
    parts = []
    for r in out:
        parts.append(_DAY_NAMES[r[0]] if len(r) == 1 else f"{_DAY_NAMES[r[0]]}-{_DAY_NAMES[r[-1]]}")
    return ",".join(parts)


def _fmt_schedule(schedule) -> list[str]:
    """One human-readable line per window. These are the RUNNING hours."""
    if schedule is None:
        return ["no schedule - never inside working hours"]
    if not schedule.windows:
        return ["no windows - always outside working hours"]
    lines = []
    for w in schedule.windows:
        ranges = ", ".join(f"{_fmt_hhmm(r.start)}-{_fmt_hhmm(r.end)}" for r in w.ranges) or "none"
        lines.append(f"{_fmt_days(w.days)} {ranges}")
    return lines


def _schedule_key(schedule) -> tuple:
    """Identity for grouping miners that share the same effective schedule."""
    if schedule is None:
        return ("none",)
    return (
        str(schedule.timezone),
        tuple((tuple(sorted(w.days)), tuple((r.start, r.end) for r in w.ranges))
              for w in schedule.windows),
    )


def _hour_map(miner) -> list[str]:
    """A 7x24 grid of running (#) versus asleep (.) hours, in local time."""
    from datetime import timedelta

    if miner.schedule is None:
        return []
    tz = miner.schedule.timezone
    # Any Monday works; the schedule is weekly. 2024-01-01 was a Monday.
    monday = datetime(2024, 1, 1, 0, 0, tzinfo=tz)
    rows = []
    for d in range(7):
        day = monday + timedelta(days=d)
        marks = "".join(
            "#" if is_working_time(miner, day + timedelta(hours=h)) else "." for h in range(24)
        )
        rows.append(f"  {_DAY_NAMES[d]}  {marks}")
    rows.append("       000000000011111111112222")
    rows.append("       012345678901234567890123")
    return rows


def _fmt_watchdog(miner) -> str:
    """One-line restart policy for the `config` table.

    `config` reads the file, not the running task, so it deliberately does not
    claim whether restarts are live - that depends on the flags `run` was
    started with, and is stated in the legend instead.
    """
    cfg = miner.watchdog
    if cfg is None:
        return "default"
    if not cfg.enabled:
        return "off (never restart)"
    if cfg.fail_after_seconds == 0:
        return f"immediate, max {cfg.max_restarts}"
    minutes = cfg.fail_after_seconds / 60
    delay = f"{minutes:.0f}m" if minutes == int(minutes) else f"{minutes:.1f}m"
    # Name the mechanism whenever it is not the default. A miner set to reboot
    # its control board is doing something materially heavier than restarting
    # bmminer, and the config table is where an operator would look for that.
    how = "" if cfg.recover_with is RecoverWith.CGMINER else f", {cfg.recover_with.value}"
    return f"after {delay}, max {cfg.max_restarts}{how}"


def cmd_config(args, config, conn) -> int:
    """Print what the config actually resolved to, after group inheritance.

    Inheritance makes mistakes invisible in the file itself: a miner that is
    missing a group, or a sleep block that never got enabled, looks fine in
    YAML and only shows up as an odd column in `status`. This prints the
    settings each miner really ended up with.
    """
    poll_interval, db_path, default_tz, miners = config
    print(f"config       : {args.config}")
    print(f"database     : {db_path}")
    print(f"poll interval: {poll_interval}s")
    print(f"time zone    : {default_tz} (unless a schedule overrides it)")
    print()

    header = (
        f"{'MINER':<14} {'GROUP':<12} {'ADDRESS':<22} {'SLEEP':<22} "
        f"{'RESTART':<20} RUNNING HOURS"
    )
    print(header)
    print("-" * len(header))

    disabled, unscheduled, no_restart = [], [], []
    for m in miners.values():
        if m.sleep.enabled:
            mode = "LIVE" if not m.sleep.dry_run else "dry-run"
            sleep_col = f"{m.sleep.backend.value} {mode}"
        else:
            sleep_col = "off (monitor only)"
            disabled.append(m.id)
        if m.schedule is None:
            unscheduled.append(m.id)

        restart_col = _fmt_watchdog(m)
        if m.watchdog is not None and not m.watchdog.enabled:
            no_restart.append(m.id)

        lines = _fmt_schedule(m.schedule)
        addr = f"{m.host}:{m.port}"
        print(f"{m.id:<14} {(m.group or '-'):<12} {addr:<22} {sleep_col:<22} "
              f"{restart_col:<20} {lines[0]}")
        for extra in lines[1:]:
            print(f"{'':<14} {'':<12} {'':<22} {'':<22} {'':<20} {extra}")

    print()
    print("RUNNING HOURS are when the miner should be mining.")
    print("Sleep is everything outside them.")
    print("RESTART is how long a miner must keep failing, inside its own hours,")
    print("before the watchdog restarts it. Whether restarts are actually sent")
    print("depends on how `run` was started: --live sends them, otherwise they")
    print("are rehearsed as would_restart.")

    # Surface the two mistakes that are invisible in the YAML itself.
    if disabled:
        print()
        print(f"NOTE: software sleep is off for {len(disabled)} of {len(miners)} miner(s):")
        print(f"      {', '.join(disabled)}")
        print("      These are polled and watchdogged, but never slept or woken.")
        print("      Add 'sleep: {enabled: true}' globally, on their group, or per miner.")
    if no_restart:
        print()
        print(f"NOTE: restarts are disabled for: {', '.join(no_restart)}")
        print("      These are polled and recorded, but never restarted even when")
        print("      they fail inside their working hours.")
    if unscheduled:
        print()
        print(f"NOTE: no schedule for: {', '.join(unscheduled)}")
        print("      A miner with no schedule is never inside working hours, so")
        print("      automatic sleep never applies to it.")

    for note in lint_miners(miners):
        print()
        print(f"PROBLEM: {note}")

    if args.hours:
        seen = {}
        for m in miners.values():
            seen.setdefault(_schedule_key(m.schedule), []).append(m)
        for members in seen.values():
            rows = _hour_map(members[0])
            if not rows:
                continue
            print()
            tz = members[0].schedule.timezone
            print(f"{', '.join(x.id for x in members)}  ({tz})")
            print("  # = mining, . = asleep")
            for row in rows:
                print(row)
    return 0


def cmd_check(args, config, conn) -> int:
    """Prove the sleep backend works, without changing anything on a miner.

    A dry run never contacts the backend — it records the intent and returns —
    so a fleet can rehearse cleanly for weeks and still fail the first time it
    goes live, on a web-UI password nobody ever tested. This is the missing
    half of the rehearsal: the schedule is proven by `run`, the plumbing here.
    """
    _, _, _, miners = config
    targets = [m for m in miners.values() if m.sleep.enabled]
    skipped = [m.id for m in miners.values() if not m.sleep.enabled]

    if not targets:
        print("No miner has software sleep enabled, so there is no backend to check.")
        if skipped:
            print(f"  monitor only: {', '.join(skipped)}")
        return 0

    async def _main() -> int:
        drivers = {m.id: get_backend(m.sleep) for m in targets}
        results = await asyncio.gather(
            *(drivers[m.id].probe(m) for m in targets), return_exceptions=True
        )
        w = max(len(m.id) for m in targets) + 1
        failures = 0
        for miner, result in zip(targets, results):
            if isinstance(result, BaseException):
                ok, detail = False, f"{type(result).__name__}: {result}"
            else:
                ok, detail = result
            mark = "OK  " if ok else "FAIL"
            print(f"{mark} {miner.id:<{w}} {miner.sleep.backend.value:<14} {detail}")
            failures += 0 if ok else 1

        print()
        if failures:
            print(f"{failures} of {len(targets)} backend(s) unreachable. Sleep would fail "
                  f"for these at the next window boundary.")
        else:
            print(f"All {len(targets)} backend(s) reachable. Nothing was changed on any miner.")
        if skipped:
            print(f"Not checked (sleep disabled): {', '.join(skipped)}")
        return 1 if failures else 0

    return asyncio.run(_main())


def cmd_diagnose(args, config, conn) -> int:
    """Find the request shape a firmware actually honours.

    Trying several shapes is safe precisely because every write is verified:
    an attempt that does not take is proven to have changed nothing, and the
    run stops immediately if any field other than the power mode moves.
    """
    _, _, _, miners = config
    miner = _select(miners, args.miner)[0]
    if not miner.sleep.enabled:
        print(f"{miner.id}: software sleep is disabled, so there is nothing to diagnose.")
        return 2

    backend = get_backend(miner.sleep)
    if miner.sleep.backend is SleepBackend.BITMAIN_HTTP:
        print(f"Testing write shapes against {miner.id} ({miner.host}). This changes the")
        print("power mode briefly and puts it back. Each attempt is verified.")
    else:
        print(f"Working out whether {miner.id} ({miner.host}) can be slept over the")
        print("cgminer API. Read-only where the firmware allows it. Anything sent is undone.")
    print()

    results = asyncio.run(backend.diagnose_write(miner))
    for label, ok, detail in results:
        print(f"{'OK  ' if ok else 'FAIL'} {label:<32} {detail}")

    # Only an actual attempt counts as a win. The cgminer run also prints
    # read-only `check` rows, and a firmware that merely *reports* a command
    # while refusing to run it must not read as success.
    if miner.sleep.backend is SleepBackend.BITMAIN_HTTP:
        attempts = [(label, ok) for label, ok, _ in results]
    else:
        attempts = [(label, ok) for label, ok, _ in results if label.startswith("try ")]
    winner = next((label for label, ok in attempts if ok), None)

    print()
    if miner.sleep.backend is SleepBackend.BITMAIN_HTTP:
        if winner is None:
            print("No request shape worked. This firmware does not accept a power-mode")
            print("change over the CGI at all.")
            print()
            print("Next: point this miner at the cgminer backend and diagnose again --")
            print("    sleep: {backend: cgminer}")
            return 1
        fmt = "form" if "form" in winner else "json"
        print(f"Use this shape. In {args.config}, under the miner's sleep block:")
        print(f"    post_format: {fmt}")
        return 0

    if winner is None:
        blob = " ".join(detail for _, _, detail in results).lower()
        denied = "lacks privileged access" in blob or "access=n" in blob
        missing = "does not implement" in blob or "exists=n" in blob

        if denied:
            # The command is there and the miner is refusing us. That is a
            # setting on the miner, not a limitation, and it is fixable.
            print("The sleep command EXISTS on this firmware - the miner is refusing")
            print("this host permission to call it.")
            print()
            print("Fix it on the miner, not here: open its web UI, find the API")
            print("settings, and give this PC write access. api-allow needs a W entry")
            print("for your address, e.g.  W:10.0.0.0/24  (or W:0/0 to allow any).")
            print("Then run diagnose again.")
            return 1

        if missing:
            print("This firmware implements no sleep command over the cgminer API,")
            print("and rejected every HTTP write shape earlier. Stock Bitmain builds")
            print("frequently have no software sleep at all.")
        else:
            print("No sleep command was accepted over the cgminer API.")
        print()
        print("Realistic options now:")
        print("  - Aftermarket firmware (Vnish, Braiins OS+) adds a real sleep, at")
        print("    the cost of reflashing and losing the Bitmain warranty.")
        print("  - A switched PDU or smart relay cuts power on the same schedule.")
        print("    MinerWatch keeps working as the monitor and watchdog either way.")
        print("  - Leave these miners running and schedule only the ones that can")
        print("    sleep. `config` shows which is which.")
        return 1

    print(f"{winner} was accepted by the firmware. Keep backend: cgminer for this")
    print("miner, then confirm with a live sleep and watch the hashrate in `status`")
    print("fall to 0 - an accepted command is still not proof the miner stopped.")
    return 0


#: Actions the poller writes on every single poll. Hidden by --decisions,
#: because at a 15-second interval they bury the handful of rows that record an
#: actual decision.
ROUTINE_ACTIONS = ("none", "alert", "expected_off")

#: What each action means, in one line. The events table is the only record of
#: why MinerWatch did or did not touch a miner, and an operator should not have
#: to read the source to interpret it.
ACTION_MEANING = {
    "none": "mining normally",
    "alert": "not mining, inside its window",
    "expected_off": "not mining, outside its window",
    "sleep": "SENT a sleep",
    "wake": "SENT a wake",
    "would_sleep": "rehearsed a sleep - nothing sent",
    "would_wake": "rehearsed a wake - nothing sent",
    "awake": "hashing again although we believed it slept",
    "sleep_failed": "the sleep or wake did not take effect",
    "sleep_needs_attention": "LATCHED: sleeps keep failing, manual clear needed",
    "sleep_attention_cleared": "sleep latch released by an operator",
    "waiting_to_restart": "failing; restart clock running",
    "would_restart": "rehearsed a restart - nothing sent",
    "restart": "SENT a restart",
    "restart_failed": "the restart could not be delivered",
    "skipped_cooldown": "restart withheld: too soon after the last one",
    "skipped_outside_hours": "restart withheld: outside its window",
    "skipped_needs_attention": "restart withheld: latched for a human",
    "skipped_watchdog_disabled": "restart withheld: disabled for this miner",
    "needs_attention": "LATCHED: restart limit reached, manual clear needed",
    "would_need_attention": "rehearsal hit the restart limit - nothing sent, no latch set",
    "reboot": "control board rebooted via the web UI",
    "reboot_failed": "the reboot could not be delivered",
    "skipped_would_need_attention": "restart withheld: rehearsal already hit the limit",
    "attention_cleared": "watchdog latch released by an operator",
}


def _collapse(rows):
    """Fold consecutive identical (state, action) rows into one line.

    A miner failing for two hours at a 15-second poll writes ~480 identical
    rows. Printing every one buries the handful of lines that say what actually
    happened - the first version of this command made an operator scroll past
    four thousand copies of `skipped_needs_attention` to discover that the
    whole fleet was latched.
    """
    out = []
    for ts, state, action, reason, ghs in rows:
        if out and out[-1][3] == state and out[-1][4] == action:
            first, _, count, st, act, rsn, rate = out[-1]
            # Keep the first reason: for a run of identical decisions it is the
            # one that explains why the run started.
            out[-1] = (first, ts, count + 1, st, act, rsn, rate if rate is not None else ghs)
        else:
            out.append((ts, ts, 1, state, action, reason, ghs))
    return out


def cmd_history(args, config, conn) -> int:
    """Replay the events table for a miner.

    Every decision either controller makes is already recorded; without a way to
    read it back, an operator watching a silent log cannot tell "nothing went
    wrong" from "nothing was attempted".
    """
    _, _, _, miners = config
    targets = _select(miners, args.miner)
    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()

    for miner in targets:
        sql = "SELECT ts, state, action, reason, ghs FROM events WHERE miner = ? AND ts >= ?"
        params: list = [miner.id, since]
        if args.decisions:
            placeholders = ",".join("?" for _ in ROUTINE_ACTIONS)
            sql += f" AND (action IS NULL OR action NOT IN ({placeholders}))"
            params.extend(ROUTINE_ACTIONS)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(args.limit, 1))
        rows = conn.execute(sql, params).fetchall()
        rows.reverse()

        print()
        print(f"{miner.id}  -  last {args.hours:g}h"
              f"{' (decisions only)' if args.decisions else ''}")
        if not rows:
            # "Never polled" and "polled all day, never acted on" look identical
            # here and are completely different problems, so check before
            # blaming the poller.
            polled = conn.execute(
                "SELECT COUNT(*) FROM events WHERE miner = ? AND ts >= ?",
                (miner.id, since),
            ).fetchone()[0]
            if polled:
                print(f"  polled {polled} times, but no decisions at all: neither the sleep")
                print("  controller nor the watchdog ever acted on this miner. Drop --decisions")
                print("  to see the raw readings.")
            else:
                print("  nothing recorded. If the whole fleet is empty the poller is not running;")
                print("  if only this miner is, check that it is in the config the task loaded.")
            continue

        width = max(len(r[2] or "-") for r in rows)
        width = max(width, len("ACTION"))
        print(f"  {'WHEN':<20} {'STATE':<12} {'HASHRATE':>10} {'ACTION':<{width}} WHY")
        print("  " + "-" * (20 + 12 + 10 + width + 30))
        for first, last, count, state, action, reason, ghs in _collapse(rows):
            when = first.replace("T", " ")[:19]
            if count > 1:
                when = f"{when} +{count - 1}"
            rate = "-" if ghs is None else (f"{ghs / 1000:,.1f} TH" if ghs >= 1000 else f"{ghs:,.0f} GH")
            act = action or "-"
            why = reason or ACTION_MEANING.get(act, "")
            if count > 1:
                why = f"{why}  [x{count}, through {last.replace('T', ' ')[11:19]}]"
            print(f"  {when:<20} {state:<12} {rate:>10} {act:<{width}} {why}")

        # Name the actions present, so an operator does not have to infer what
        # an absent one means. "No sleep row" is the answer to a different
        # question than "a sleep row that failed".
        seen = {r[2] for r in rows if r[2] and r[2] not in ROUTINE_ACTIONS}
        if seen:
            print()
            print("  decisions in this window:")
            for act in sorted(seen):
                print(f"    {act:<26} {ACTION_MEANING.get(act, '')}")
        elif args.decisions:
            print("  no decisions at all: neither controller acted on this miner.")
    return 0


def cmd_clear_attention(args, config, conn) -> int:
    _, _, _, miners = config
    targets = _select(miners, args.miner)
    controller = SleepController(conn, miners)
    watchdog = Watchdog(conn, miners=miners)
    for miner in targets:
        watchdog.clear_attention(miner.id)
        controller.clear_attention(miner.id)
        print(f"{miner.id}: attention latches cleared")
    return 0


#: Every subcommand name, used to tell a bare config path from a subcommand.
_COMMANDS = ("run", "sleep", "wake", "status", "config", "check", "diagnose", "clear-attention")


def _normalise_argv(argv: list[str] | None) -> list[str]:
    """Support the historic ``python -m minerwatch miners.yaml`` form.

    Before subcommands existed the entry point took a single positional config
    path. Windows Task Scheduler entries out in the field still look like that,
    so a leading non-flag argument that is not a subcommand is rewritten into
    ``-c <path>``.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and not argv[0].startswith("-") and argv[0] not in _COMMANDS:
        return ["-c", argv[0], *argv[1:]]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalise_argv(argv))
    _setup_logging(args.verbose, getattr(args, "log_file", None))

    # No subcommand: behave like the historic `python -m minerwatch <config>`
    # invocation so existing scheduled tasks keep working.
    if args.command is None:
        args.command = "run"
        for attr, default in (
            ("live", False), ("live_watchdog", False), ("live_sleep", False),
            ("dry_run", False), ("once", False),
        ):
            setattr(args, attr, getattr(args, attr, default))

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"Config file not found: {args.config}", file=sys.stderr)
        return 2
    except TimezoneDataMissing as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    conn = init_db(config[1])
    try:
        if args.command == "run":
            return cmd_run(args, config, conn)
        if args.command == "sleep":
            return cmd_power(args, config, conn, to_sleep=True)
        if args.command == "wake":
            return cmd_power(args, config, conn, to_sleep=False)
        if args.command == "status":
            return cmd_status(args, config, conn)
        if args.command == "config":
            return cmd_config(args, config, conn)
        if args.command == "check":
            return cmd_check(args, config, conn)
        if args.command == "diagnose":
            return cmd_diagnose(args, config, conn)
        if args.command == "clear-attention":
            return cmd_clear_attention(args, config, conn)
        if args.command == "history":
            return cmd_history(args, config, conn)
        parser.print_help()
        return 2
    finally:
        conn.close()
