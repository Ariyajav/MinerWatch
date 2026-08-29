import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from threading import Event as ThreadEvent

from minerwatch import api
from minerwatch.compat import interruptible_sleep
from minerwatch.models import Event, Miner, State
from minerwatch.schedule import is_working_time
from minerwatch.sleeper import SleepController
from minerwatch.store import last_state, record_event
from minerwatch.watchdog import Watchdog

logger = logging.getLogger(__name__)


#: Hashrate fields seen across Antminer firmwares, and their factor to GH/s.
#: Ordered by preference: an instantaneous 5-second rate beats a lifetime
#: average, which lags for minutes after a miner actually stops.
_HASHRATE_KEYS: tuple[tuple[str, float], ...] = (
    ("GHS 5s", 1.0),
    ("GHS 1m", 1.0),
    ("MHS 5s", 1e-3),
    ("THS 5s", 1e3),
    ("KHS 5s", 1e-6),
    ("ghs_5s", 1.0),
    ("GHS av", 1.0),
    ("MHS av", 1e-3),
    ("THS av", 1e3),
    ("KHS av", 1e-6),
)


def _hashrate_ghs(entry: dict) -> tuple[float, str | None]:
    """Find a hashrate in a SUMMARY entry, normalised to GH/s.

    Returns ``(value, key_used)``; ``key_used`` is ``None`` when the reply
    carries no recognisable hashrate at all, which is a different condition
    from a hashrate of zero and must not be mistaken for a stopped miner.
    """
    for key, factor in _HASHRATE_KEYS:
        if key not in entry:
            continue
        try:
            # Some firmwares quote the number as a string.
            return float(entry[key]) * factor, key
        except (TypeError, ValueError):
            continue
    return 0.0, None


def classify(payload_or_error: Exception | bytes) -> tuple[State, str]:
    """Back-compatible wrapper: state and reason only."""
    state, reason, _ = classify_detail(payload_or_error)
    return state, reason


def classify_detail(payload_or_error: Exception | bytes) -> tuple[State, str, float | None]:
    """Classify a reply and return the hashrate alongside it.

    The hashrate is what separates a real sleep from a low-power mode: both
    stop full-rate mining, but only one goes to zero. Recording it turns "the
    sleep command was accepted" into "the miner actually stopped".
    """
    if isinstance(payload_or_error, Exception):
        # Several relevant exceptions carry an empty message - asyncio.TimeoutError
        # most of all, which is exactly what a miner polled on its *web* port
        # produces. "unreachable" with a blank reason tells an operator nothing,
        # so fall back to the exception's type name.
        return State.UNREACHABLE, str(payload_or_error) or type(payload_or_error).__name__, None

    raw = payload_or_error.rstrip(b"\x00")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return State.UNREACHABLE, "invalid JSON", None

    # A command the miner refused never carries a SUMMARY. Reading the refusal
    # as "no hashrate, therefore stopped" would have the watchdog restarting a
    # perfectly healthy miner that simply would not talk to us, so check the
    # envelope before looking for numbers.
    ok, envelope_msg = api.check_status(data) if "STATUS" in data else (True, "")
    has_summary = isinstance(data.get("SUMMARY"), list) and data.get("SUMMARY")
    if not ok and not has_summary:
        return State.UNREACHABLE, envelope_msg or "miner refused the summary command", None

    summary_list = data.get("SUMMARY")
    if isinstance(summary_list, list) and summary_list:
        entry = summary_list[0] or {}  # handle [null]
    else:
        entry = data
    if not isinstance(entry, dict):
        entry = {}

    ghs_5s, source = _hashrate_ghs(entry)
    status = entry.get("Status", "")
    state_val = entry.get("state", "")

    # Explicit stopped/state overrides hashrate — a miner can report
    # stale hashrate while in a stopping state. "sleeping" is the state a
    # software-slept miner reports; it is a stop, not a fault.
    if state_val in ("stopped", "sleeping") or status in ("Sick", "Dead", "Sleeping"):
        return State.STOPPED, "", ghs_5s if source else None

    # Hashrate alone decides. Stock bmminer does NOT put a "Status" field in
    # SUMMARY — that lives in the STATUS envelope, and per-device "Alive" comes
    # from the `devs` command. Requiring Status == "Alive" here meant every
    # healthy miner on real firmware fell through to "unexpected payload" and
    # was reported unreachable; only the simulator, which sends the field, ever
    # looked healthy.
    if source is None:
        keys = ", ".join(sorted(entry)[:8]) or "none"
        return State.UNREACHABLE, f"no hashrate field in reply (keys: {keys})", None
    if ghs_5s > 0:
        return State.MINING, "", ghs_5s
    return State.STOPPED, "", ghs_5s


class Poller:
    def __init__(
        self,
        config: tuple,
        conn: sqlite3.Connection,
        stop_event: ThreadEvent,
        dry_run: bool = True,
        sleep_dry_run: bool | None = None,
    ):
        """
        Args:
            config: the tuple returned by :func:`minerwatch.config.load_config`.
            conn: open events database.
            stop_event: set by the signal handler to end the loop.
            dry_run: watchdog rehearsal mode (no restarts are sent).
            sleep_dry_run: overrides each miner's configured ``sleep.dry_run``.
                ``None`` (the default) honours the config file.
        """
        self.poll_interval, _, self.default_tz, self.miners = config
        self.conn = conn
        self.stop_event = stop_event
        # Pass the fleet so per-miner restart policy is available where only
        # an id is known - startup hydration and `clear-attention`.
        self.watchdog = Watchdog(
            conn, dry_run=dry_run, miners=self.miners, poll_interval=self.poll_interval
        )
        self.sleeper = SleepController(conn, self.miners, dry_run=sleep_dry_run)
        #: Last state logged per miner, so a transition is announced once
        #: rather than every poll. Seeded from the database so a service
        #: restart does not re-announce the whole fleet.
        self._last_logged: dict[str, str] = {}
        for miner in self.miners.values():
            previous = last_state(conn, miner.id)
            if previous is not None:
                self._last_logged[miner.id] = previous.state

    async def run(self):
        while not self.stop_event.is_set():
            start = time.monotonic()
            tasks = [self._poll_one(m) for m in self.miners.values()]
            # return_exceptions: one miner raising must not cancel the sibling
            # polls or break out of the supervision loop.
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for miner, result in zip(self.miners.values(), results):
                if isinstance(result, BaseException):
                    logger.exception(
                        "Unhandled error polling %s: %s", miner.id, result, exc_info=result
                    )
            elapsed = time.monotonic() - start
            sleep = max(0, self.poll_interval - elapsed)
            if sleep > 0:
                # Wake early on shutdown: a plain asyncio.sleep(poll_interval)
                # leaves the process looking hung for a full interval after
                # Ctrl+C, which on Windows is compounded by the Proactor loop
                # not observing SIGINT until it returns from a wait.
                await interruptible_sleep(sleep, self.stop_event)

    async def _poll_one(self, miner: Miner):
        try:
            raw = await api.request(miner.host, miner.port, "summary")
            state, reason, ghs = classify_detail(raw)
        except Exception as e:
            state, reason, ghs = classify_detail(e)

        now = datetime.now(timezone.utc)
        ts = now.isoformat()
        working = is_working_time(miner, now)

        if state == State.MINING:
            action = "none"
        elif state == State.STOPPED:
            action = "alert" if working else "expected_off"
        else:
            action = "alert"

        event = Event(ts=ts, miner=miner.id, state=state.value, action=action,
                      reason=reason or None, ghs=ghs)
        record_event(self.conn, event)

        # Announce state changes at INFO. Without this the log records only
        # startup lines and power actions, so a miner going unreachable - the
        # thing an operator most wants to know about - writes nothing at all,
        # and a silent log is indistinguishable from a dead service.
        previous = self._last_logged.get(miner.id)
        if previous != state.value:
            self._last_logged[miner.id] = state.value
            rate = "" if ghs is None else f" at {ghs / 1000:,.1f} TH/s"
            detail = f" ({reason})" if reason else ""
            log = logger.warning if state != State.MINING else logger.info
            log(
                "%s: %s -> %s%s%s",
                miner.id, previous or "unknown", state.value, rate, detail,
            )

        # Software power control runs first: a miner MinerWatch deliberately
        # put to sleep reads back as STOPPED, which is exactly what would make
        # the watchdog fire a restart. When the sleeper owns the current state,
        # the watchdog stands down for this cycle.
        handled = await self.sleeper.consider(miner, state, working, now, reason)
        if not handled:
            await self.watchdog.consider(miner, state, working, now, reason)
        logger.debug(
            "Miner %s: state=%s action=%s power_handled=%s", miner.id, state.value, action, handled
        )
