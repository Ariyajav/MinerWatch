import asyncio
import logging
from collections import deque
from datetime import datetime

from minerwatch import api
from minerwatch.models import Event, Miner, State, WatchdogConfig
from minerwatch.store import (
    ClockRules,
    clear_needs_attention,
    failure_clock_start,
    is_needs_attention,
    record_event,
)

logger = logging.getLogger(__name__)

#: Actions that record a failure the watchdog observed *inside* a miner's own
#: working hours. These start and sustain the failure clock.
IN_WINDOW_FAILURE_ACTIONS: tuple[str, ...] = (
    "waiting_to_restart",
    "skipped_needs_attention",
    "skipped_cooldown",
    "would_restart",
    "restart",
    "restart_failed",
    "needs_attention",
)

#: Actions that mean "not a failure we are counting", and therefore clear the
#: clock. ``skipped_outside_hours`` and the poller's ``expected_off`` are the
#: miner being legitimately off; ``skipped_watchdog_disabled`` is the watchdog
#: declining to look at all.
#:
#: The out-of-window entries matter more than they look. Without them the clock
#: measured plain wall-clock time from the first failure, so a single dropped
#: packet at 17:59 kept ticking through a twelve-hour overnight shutdown and the
#: whole fleet — which shares a window — was restarted mid-spin-up the next
#: morning.
CLOCK_RESET_ACTIONS: tuple[str, ...] = (
    "skipped_outside_hours",
    "skipped_watchdog_disabled",
    "expected_off",
)

#: Poller actions that prove the miner is NOT hashing without themselves being
#: a counted failure. `alert` is written for a stopped or unreachable miner in
#: any window, so it cannot start the clock - but it does mean a recovery run
#: is over.
NOT_MINING_ACTIONS: tuple[str, ...] = ("alert",)

#: Poller actions whose ``state='mining'`` rows are real evidence of hashing.
#: The controllers write bookkeeping rows carrying whatever state they assumed
#: at the time — a manual ``sleep`` preview records ``state='mining'`` even for
#: an unreachable miner — so only the poller's own observations count.
MINING_EVIDENCE_ACTIONS: tuple[str, ...] = ("none",)

#: How long a miner must mine continuously before it counts as recovered.
#: One good poll is not enough: a miner hashing 1% of the time would reset the
#: clock every few minutes and never be restarted at all.
DEFAULT_RECOVERY_SECONDS = 300

#: Missed polls tolerated inside a recovery run before it starts over. Silence
#: is not evidence of hashing: without this, one lucky reading, a five-minute
#: service outage, and one more lucky reading counted as five minutes of
#: continuous mining and cleared the clock.
GAP_TOLERANCE_POLLS = 3

#: Floor and ceiling on how many rows of history the clock walk reads.
MIN_CLOCK_ROWS = 300
MAX_CLOCK_ROWS = 5000

#: Rows written per poll of a failing miner: one by the poller, one by the
#: watchdog, with headroom.
ROWS_PER_POLL = 3


class Watchdog:
    """Monitors failing miners and actuates restarts with cooldown, rate-limiting, and needs_attention latching.

    Safe by default: dry_run=True prevents any actual TCP restarts.

    Constructor arguments are the fallback policy. When a :class:`Miner` carries
    its own :class:`WatchdogConfig` (which the configuration file always
    supplies) that wins, so different groups can have different restart
    policies while a Watchdog built by hand keeps the arguments it was given.
    """

    def __init__(
        self,
        conn,
        dry_run: bool = True,
        cooldown: int = 600,
        rate_window: int = 3600,
        max_restarts: int = 3,
        fail_after: int = 1800,
        recovery_seconds: int = DEFAULT_RECOVERY_SECONDS,
        poll_interval: int = 15,
        miners: "dict[str, Miner] | None" = None,
    ):
        self.conn = conn
        self.dry_run = dry_run
        self.cooldown = cooldown
        self.rate_window = rate_window
        self.max_restarts = max_restarts
        self.recovery_seconds = recovery_seconds
        #: The fleet's poll interval. The clock needs it to tell a gap in the
        #: record from continuous hashing, and to size its own lookback.
        self.poll_interval = max(int(poll_interval), 1)
        #: The fleet, when the caller has it. Needed so that startup hydration
        #: and `clear-attention` — neither of which is handed a Miner — apply
        #: that miner's own retry budget rather than this object's default.
        #: Trimming a 2-attempt miner's deque against a default of 3 removed
        #: nothing, so clearing its latch was a silent no-op and restarts stayed
        #: off for good.
        self._miners: dict[str, Miner] = dict(miners or {})
        #: Seconds of continuous in-window failure before the first restart.
        #: A miner that misses one poll is not a broken miner; a miner that has
        #: been down for half an hour is.
        self.fail_after = fail_after
        self._attempts: dict[str, deque[datetime]] = {}
        self._needs_attention: set[str] = set()
        self._hydrate_needs_attention()
        self._hydrate_attempts()

    # ------------------------------------------------------------------
    # Startup hydration
    # ------------------------------------------------------------------

    def _hydrate_needs_attention(self) -> None:
        """Populate _needs_attention from events table so latch survives restarts."""
        rows = self.conn.execute(
            "SELECT DISTINCT miner FROM events WHERE action IN ('needs_attention', 'attention_cleared')"
        ).fetchall()
        for (miner_id,) in rows:
            if is_needs_attention(self.conn, miner_id):
                self._needs_attention.add(miner_id)

    def _hydrate_attempts(self) -> None:
        """Rebuild attempt deques from recent events so rate-limit/cooldown survive restarts."""
        rows = self.conn.execute(
            """SELECT miner, ts, action
               FROM events
               WHERE action IN ('restart', 'restart_failed')
               ORDER BY miner, ts, id"""
        ).fetchall()
        for miner_id, ts_str, action in rows:
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if miner_id not in self._attempts:
                self._attempts[miner_id] = deque()
            self._attempts[miner_id].append(ts)

        # Reapply the effect of any manual clear. Without this, replaying every
        # attempt in the rate window silently undoes `clear_attention`: the
        # operator releases the latch, the service restarts (a reboot, a config
        # edit, Task Scheduler), the full deque comes back, and the miner
        # re-latches on its very next attempt instead of getting its retry
        # budget back.
        for miner_id in list(self._attempts):
            cleared = self.conn.execute(
                "SELECT ts FROM events WHERE miner = ? AND action = 'attention_cleared' "
                "ORDER BY ts DESC, id DESC LIMIT 1",
                (miner_id,),
            ).fetchone()
            if cleared is None or miner_id in self._needs_attention:
                continue
            self._trim_after_clear(
                self._attempts[miner_id], self._resolve_by_id(miner_id).max_restarts
            )

    # ------------------------------------------------------------------
    # Deque helpers
    # ------------------------------------------------------------------

    def _get_dq(self, miner_id: str) -> deque:
        if miner_id not in self._attempts:
            self._attempts[miner_id] = deque()
        return self._attempts[miner_id]

    def _evict_expired(self, dq: deque, now: datetime, rate_window: int | None = None) -> None:
        window = self.rate_window if rate_window is None else rate_window
        while dq and (now - dq[0]).total_seconds() > window:
            dq.popleft()

    # ------------------------------------------------------------------
    # Main decision funnel
    # ------------------------------------------------------------------

    def _defaults(self) -> WatchdogConfig:
        return WatchdogConfig(
            enabled=True,
            fail_after_seconds=self.fail_after,
            cooldown_seconds=self.cooldown,
            rate_window_seconds=self.rate_window,
            max_restarts=self.max_restarts,
        )

    def _resolve(self, miner: Miner) -> WatchdogConfig:
        """Effective policy for *miner*: its own config, else this Watchdog's."""
        if miner.watchdog is not None:
            return miner.watchdog
        return self._defaults()

    def _resolve_by_id(self, miner_id: str) -> WatchdogConfig:
        """Effective policy where only the id is known (hydration, clear)."""
        miner = self._miners.get(miner_id)
        if miner is not None and miner.watchdog is not None:
            return miner.watchdog
        return self._defaults()

    def _clock_rules(self, miner_id: str) -> ClockRules:
        cfg = self._resolve_by_id(miner_id)
        # Bounded by ROWS, not by elapsed time. A time horizon looks equivalent
        # and is not: with a long poll interval a miner dead for days produces
        # rows spaced further apart than the horizon, so every poll reads as the
        # first failure and it is never restarted at all.
        polls = (cfg.fail_after_seconds + self.recovery_seconds) // self.poll_interval + 1
        rows = min(MAX_CLOCK_ROWS, max(MIN_CLOCK_ROWS, polls * ROWS_PER_POLL + 150))
        return ClockRules(
            failure_actions=frozenset(IN_WINDOW_FAILURE_ACTIONS),
            reset_actions=frozenset(CLOCK_RESET_ACTIONS),
            mining_actions=frozenset(MINING_EVIDENCE_ACTIONS),
            not_mining_actions=frozenset(NOT_MINING_ACTIONS),
            recovery_seconds=self.recovery_seconds,
            max_gap_seconds=self.poll_interval * GAP_TOLERANCE_POLLS,
            max_rows=rows,
        )

    def failing_for(self, miner_id: str, now: datetime) -> float:
        """Seconds *miner_id* has been continuously failing inside its window.

        ``0.0`` when this is the first such poll. Read back out of the events
        table so the elapsed time survives a service restart.
        """
        since = failure_clock_start(self.conn, miner_id, now, self._clock_rules(miner_id))
        if since is None:
            return 0.0
        try:
            started = datetime.fromisoformat(since)
        except ValueError:  # pragma: no cover - malformed row
            return 0.0
        elapsed = (now - started).total_seconds()
        # A clock that went backwards (NTP step, a DST-naive row) must not read
        # as "failing for -3600s" and thereby postpone a restart for an hour.
        return max(0.0, elapsed)

    async def consider(self, miner: Miner, state: State, working: bool, now: datetime, reason: str) -> None:
        """Called at the end of each poll to decide whether to restart the miner.

        Decision order (first match wins):
          1. MINING              → no-op
          2. restarts disabled   → skipped_watchdog_disabled
          3. not working         → skipped_outside_hours
          4. needs_attention     → skipped_needs_attention
          5. failing < fail_after→ waiting_to_restart
          6. within cooldown     → skipped_cooldown
          7. ≥max_restarts       → needs_attention (latch set)
          8. dry_run             → would_restart  (deque advanced)
          9. else                → send_restart → restart / restart_failed
        """
        cfg = self._resolve(miner)

        # 1. MINING → no-op
        if state == State.MINING:
            return

        # 2. Restarts switched off for this miner entirely
        if not cfg.enabled:
            self._log(miner, state, "skipped_watchdog_disabled", "restarts are disabled for this miner", now)
            return

        # 3. Outside working hours → skip.
        #    This is checked *before* the latch on purpose. With the latch
        #    first, a latched miner recorded `skipped_needs_attention` all
        #    night instead of `skipped_outside_hours`, and since that action
        #    counts as an in-window failure the overnight hours leaked back
        #    into the clock — so clearing the latch in the morning restarted a
        #    miner that was merely still booting.
        if not working:
            self._log(miner, state, "skipped_outside_hours", "outside working hours", now)
            return

        # 4. Latched → skip
        if miner.id in self._needs_attention:
            self._log(miner, state, "skipped_needs_attention", "miner is latched as needs_attention", now)
            return

        # 5. Confirmation delay. A miner that missed one poll is not a broken
        #    miner - a timeout, a busy web UI, or a pool reconnect all read as
        #    a failure for a poll or two. Restarting on the first one turns a
        #    transient into an outage, and on stock firmware a restart costs
        #    several minutes of hashing.
        #
        #    Logged every poll rather than silently, so an operator reading the
        #    events table can see the clock running instead of wondering why
        #    nothing is happening to an obviously dead miner.
        if cfg.fail_after_seconds > 0:
            elapsed = self.failing_for(miner.id, now)
            if elapsed < cfg.fail_after_seconds:
                remaining = int(cfg.fail_after_seconds - elapsed)
                self._log(
                    miner, state, "waiting_to_restart",
                    f"failing for {int(elapsed)}s; restart in {remaining}s if it does not recover",
                    now,
                )
                return

        dq = self._get_dq(miner.id)

        # 6. Cooldown check
        if dq:
            last_ts = dq[-1]
            if (now - last_ts).total_seconds() < cfg.cooldown_seconds:
                self._log(miner, state, "skipped_cooldown", f"cooldown active; last attempt at {last_ts.isoformat()}", now)
                return

        # 7. Rate-limit — evict expired entries, then check max
        self._evict_expired(dq, now, cfg.rate_window_seconds)
        if len(dq) >= cfg.max_restarts:
            self._needs_attention.add(miner.id)
            self._log(
                miner, state, "needs_attention",
                f"{cfg.max_restarts} restart attempts within {cfg.rate_window_seconds}s; manual clear required",
                now,
            )
            return

        now_ts = now

        # 8. Dry-run
        if self.dry_run:
            dq.append(now_ts)
            self._log(miner, state, "would_restart", f"dry-run: would send restart to {miner.host}:{miner.port}", now)
            return

        # 9. Actuate
        ok, detail = await self.send_restart(miner)
        dq.append(now_ts)
        action = "restart" if ok else "restart_failed"
        self._log(miner, state, action, detail, now)
        logger.warning(
            "Watchdog %s %s after %ds of failure: %s",
            "restarted" if ok else "FAILED to restart",
            miner.id,
            int(self.failing_for(miner.id, now)),
            detail,
        )

    # ------------------------------------------------------------------
    # TCP restart actuator
    # ------------------------------------------------------------------

    async def send_restart(self, miner: Miner) -> tuple[bool, str]:
        """Send ``{"command":"restart"}`` to *miner* over TCP.

        Returns ``(True, "restart acknowledged")`` on success or ``(False, error_msg)`` on
        any exception or error response.  Never raises.
        """
        try:
            # api.request reads until the NUL terminator rather than taking a
            # single read() as a whole message; a restart acknowledgement split
            # across two TCP segments used to be misread as unparseable.
            data = await api.request(
                miner.host, miner.port, "restart", connect_timeout=5, read_timeout=5
            )
            try:
                resp = api.parse_response(data)
            except api.ApiError as exc:
                return (False, str(exc))

            status_list = resp.get("STATUS", [])
            if isinstance(status_list, list) and len(status_list) > 0:
                entry = status_list[0]
                if isinstance(entry, dict) and entry.get("STATUS") == "S":
                    return (True, "restart acknowledged")
                if isinstance(entry, dict) and entry.get("STATUS") == "E":
                    return (False, entry.get("Msg", "unknown error"))
            return (False, f"unexpected response structure: {resp}")
        except Exception as e:
            return (False, str(e) or type(e).__name__)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def clear_attention(self, miner_id: str) -> None:
        """Clear the needs_attention latch and trim the deque so the rate limit no longer blocks.

        The deque is trimmed to at most ``max_restarts - 1`` entries, keeping the most
        recent ones.  The cooldown timer is preserved because the last (most recent)
        entry is retained.
        """
        self._needs_attention.discard(miner_id)
        clear_needs_attention(self.conn, miner_id)
        if miner_id in self._attempts:
            self._trim_after_clear(self._attempts[miner_id], self._resolve_by_id(miner_id).max_restarts)

    def _trim_after_clear(self, dq: deque, max_restarts: int | None = None) -> None:
        """Trim *dq* to at most ``max_restarts - 1`` entries, keeping the newest.

        Shared by :meth:`clear_attention` and startup hydration so a cleared
        latch looks the same whether or not the process has restarted since.

        *max_restarts* is the miner's own budget, not this object's default.
        The funnel latches on the per-miner value, so trimming against a
        different one either removed nothing — leaving a miner configured for
        two attempts to re-latch on its very next poll, permanently, with the
        operator's clear silently doing nothing — or removed too much and
        handed back more attempts than were configured.
        """
        limit = self.max_restarts if max_restarts is None else max_restarts
        while len(dq) >= limit and len(dq) > 1:
            dq.popleft()

    #: Actions the watchdog writes on every poll of a failing miner. At a
    #: 15-second interval these would bury everything else at INFO, so they go
    #: to DEBUG - visible under `-v`, which is exactly the trace an operator
    #: wants when asking "why has nothing been restarted?".
    ROUTINE_ACTIONS = frozenset({
        "waiting_to_restart",
        "skipped_cooldown",
        "skipped_outside_hours",
        "skipped_needs_attention",
        "skipped_watchdog_disabled",
    })

    def _log(self, miner: Miner, state: State, action: str, reason: str, now: datetime) -> None:
        event = Event(ts=now.isoformat(), miner=miner.id, state=state.value, action=action, reason=reason)
        record_event(self.conn, event)
        # The sleeper has always logged its decisions; the watchdog logged
        # nothing at all, so a fleet where restarts were being withheld looked
        # identical to one where nothing was wrong.
        if action in self.ROUTINE_ACTIONS:
            logger.debug("%s: %s (%s)", miner.id, action, reason)
        elif action == "needs_attention":
            logger.warning("%s: %s (%s)", miner.id, action, reason)
        else:
            logger.info("%s: %s (%s)", miner.id, action, reason)
