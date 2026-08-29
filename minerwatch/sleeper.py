"""Schedule-driven software power control.

The scheduler previously only *described* when a miner should run — a miner
found stopped outside its window was logged ``expected_off`` and nothing acted
on a miner still hashing outside its window. :class:`SleepController` closes
that loop: it puts miners to sleep when their window ends and wakes them when
it starts, using the drivers in :mod:`minerwatch.backends`.

Two invariants shape the design.

**The watchdog must not fight the scheduler.** A miner that MinerWatch
deliberately put to sleep reads back as ``STOPPED``, which is exactly the
condition that makes the watchdog fire a restart. The controller therefore runs
*before* the watchdog each poll and reports whether it owns the current state;
when it does, the watchdog stands down.

**Intent is durable.** "This miner is asleep because we slept it" cannot live
only in memory, or a MinerWatch restart would see a stopped miner it does not
recognise and start restarting it. The latch is reconstructed from the events
table on startup, the same way the watchdog recovers ``needs_attention``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from minerwatch.backends import SleepBackendDriver, get_backend
from minerwatch.models import Event, Miner, State
from minerwatch.store import last_action_in, miners_with_actions, record_event

logger = logging.getLogger(__name__)

# Actions that mean "MinerWatch believes this miner is asleep".
SLEEP_ACTIONS = ("sleep",)
# Actions that mean "MinerWatch believes this miner should be running".
WAKE_ACTIONS = ("wake", "awake")
POWER_ACTIONS = SLEEP_ACTIONS + WAKE_ACTIONS
# Rehearsal records. Deliberately *not* hydrated: a dry run must never leave
# behind a belief that a later live run reads back as fact and acts on. They
# are written purely so an operator can see what would have happened.
DRY_RUN_ACTIONS = ("would_sleep", "would_wake")
FAILURE_ACTIONS = ("sleep_failed", "wake_failed")
#: Actions that represent MinerWatch actually trying to change a miner's power
#: state. "awake" is an *observation* and is deliberately excluded: counting it
#: as an attempt would push the cooldown forward past the real one.
ACTUATION_ACTIONS = SLEEP_ACTIONS + ("wake",) + FAILURE_ACTIONS
ATTENTION_ACTIONS = ("sleep_needs_attention", "sleep_attention_cleared")
CLEARED_ACTION = "sleep_attention_cleared"


class SleepController:
    """Decide and actuate sleep/wake transitions for scheduled miners."""

    def __init__(self, conn, miners: dict[str, Miner] | None = None, dry_run: bool | None = None):
        """
        The sqlite connection is used from the event loop thread only — the
        poller awaits :meth:`consider` inline rather than offloading it — so no
        additional locking is required.

        Args:
            conn: open events database.
            miners: miners to hydrate latches for. Any miner may still be
                passed to :meth:`consider` later; this only seeds known state.
            dry_run: overrides every miner's configured ``dry_run`` when set.
                The CLI uses it to force a rehearsal (``True``) or an explicit
                live run (``False``) without editing the config file.
        """
        self.conn = conn
        self.dry_run_override = dry_run
        self._drivers: dict[str, SleepBackendDriver] = {}
        #: miner id -> time we last believed it went to sleep
        self._asleep: dict[str, datetime] = {}
        #: miner ids whose asleep latch came from a rehearsal, so nothing was
        #: actually sent and the miner is expected to keep hashing
        self._simulated: set[str] = set()
        #: miner id -> time of the last wake, used for the spin-up grace period
        self._waking: dict[str, datetime] = {}
        #: miner id -> time of the last actuation attempt (cooldown)
        self._last_attempt: dict[str, datetime] = {}
        #: miner id -> the ``_last_attempt`` value a cooldown skip was already
        #: logged for, so one cooldown produces one event and not one per poll
        self._cooldown_logged: dict[str, datetime] = {}
        #: miner id -> consecutive actuation failures
        self._failures: dict[str, int] = {}
        #: miner id -> consecutive sleeps that were acknowledged but did not
        #: actually stop the miner. Tracked separately from ``_failures``
        #: because each of those attempts reports success.
        self._ineffective: dict[str, int] = {}
        #: miners latched off after repeated failures
        self._attention: set[str] = set()
        self._hydrate(miners or {})

    # ------------------------------------------------------------------
    # Startup hydration
    # ------------------------------------------------------------------

    def _hydrate(self, miners: dict[str, Miner]) -> None:
        """Rebuild in-memory state from the durable event log.

        Everything the decision funnel reads is restored, not just the asleep
        latch: losing the wake timestamp would make the first poll after a
        process restart see a miner that is still spinning up as a failure and
        hand it to the watchdog, and losing the cooldown would let a
        crash-looping service actuate far faster than configured.
        """
        tracked = POWER_ACTIONS + FAILURE_ACTIONS + ATTENTION_ACTIONS
        known = set(miners) | set(miners_with_actions(self.conn, tracked))
        for miner_id in known:
            last_power = last_action_in(self.conn, miner_id, POWER_ACTIONS)
            if last_power is not None:
                if last_power.action in SLEEP_ACTIONS:
                    # Fall back to "now" rather than dropping the latch: an
                    # unparseable timestamp must not make MinerWatch forget it
                    # slept a miner, or the watchdog starts restarting a device
                    # that was stopped on purpose.
                    self._asleep[miner_id] = _parse_ts(last_power.ts) or _utcnow(None)
                elif last_power.action == "wake":
                    ts = _parse_ts(last_power.ts)
                    if ts is not None:
                        self._waking[miner_id] = ts

            last_attempt = last_action_in(self.conn, miner_id, ACTUATION_ACTIONS)
            if last_attempt is not None:
                ts = _parse_ts(last_attempt.ts)
                if ts is not None:
                    self._last_attempt[miner_id] = ts

            failures = self._count_recent_failures(miner_id)
            if failures:
                self._failures[miner_id] = failures

            attention = last_action_in(self.conn, miner_id, ATTENTION_ACTIONS)
            if attention is not None and attention.action == "sleep_needs_attention":
                self._attention.add(miner_id)

    def _count_recent_failures(self, miner_id: str) -> int:
        """Consecutive actuation failures since the last success *or manual clear*.

        Mirrors the in-memory counter so ``max_failures`` still means the same
        thing across a process restart. ``sleep_attention_cleared`` stops the
        scan as firmly as a success does: an operator who clears a latch is
        asking for the miner's full retry budget back, and without this the
        clear would be silently undone by the next service restart — the miner
        would re-latch after a single attempt.
        """
        tracked = (*ACTUATION_ACTIONS, CLEARED_ACTION)
        placeholders = ",".join("?" for _ in tracked)
        rows = self.conn.execute(
            f"SELECT action FROM events WHERE miner = ? AND action IN ({placeholders}) "
            f"ORDER BY ts DESC, id DESC LIMIT 100",
            (miner_id, *tracked),
        ).fetchall()
        count = 0
        for (action,) in rows:
            if action in FAILURE_ACTIONS:
                count += 1
            else:
                break
        return count

    # ------------------------------------------------------------------
    # Introspection (used by the CLI's status command and by tests)
    # ------------------------------------------------------------------

    def is_asleep(self, miner_id: str) -> bool:
        return miner_id in self._asleep

    def needs_attention(self, miner_id: str) -> bool:
        return miner_id in self._attention

    def clear_attention(self, miner_id: str) -> None:
        """Release the failure latch so automatic control resumes."""
        self._attention.discard(miner_id)
        self._failures.pop(miner_id, None)
        self._ineffective.pop(miner_id, None)
        self._last_attempt.pop(miner_id, None)
        self._cooldown_logged.pop(miner_id, None)
        self._record(miner_id, State.UNREACHABLE, CLEARED_ACTION, "manual clear")

    # ------------------------------------------------------------------
    # Main decision funnel
    # ------------------------------------------------------------------

    async def consider(
        self, miner: Miner, state: State, working: bool, now: datetime, reason: str | None = None
    ) -> bool:
        """Act on *miner* for this poll.

        Returns ``True`` when the controller owns the miner's current state and
        the watchdog should not act on it this cycle.

        Decision order (first match wins):

        1. control disabled / no schedule    -> not owned
        2. hashing while we believe it slept -> settle, or clear a stale latch
        3. failure latch set                 -> owned only while we think it is asleep
        4. inside spin-up grace after wake   -> owned
        5. unreachable                       -> not owned
        6. outside window and mining         -> sleep
        7. outside window and stopped        -> owned iff we slept it
        8. inside window and stopped by us   -> wake
        9. anything else                     -> not owned

        Step 2 runs before step 3 deliberately. If the failure latch were
        checked first, a miner latched after a failed wake could never shed a
        stale asleep latch, and *both* controllers would stand down forever —
        the sleeper because it is latched, the watchdog because it is told the
        state is owned — leaving a genuinely dead miner unattended.
        """
        cfg = miner.sleep
        # 1. Software power control switched off for this miner.
        if not cfg.enabled:
            return False
        # Automatic control is schedule-driven; a miner with no schedule has no
        # notion of "outside working hours" and is left entirely to the
        # watchdog (it can still be slept manually through the CLI).
        if miner.schedule is None:
            return False

        # 2. Still hashing although we believe we slept it.
        sleep_ts = self._asleep.get(miner.id)
        if state == State.MINING and sleep_ts is not None:
            if miner.id in self._simulated:
                # Rehearsal: nothing was ever sent, so of course it is still
                # hashing. Keep the pretend latch instead of flip-flopping
                # between "would sleep" and "awake" on every single poll...
                if not working:
                    return True
                # ...but when the window reopens, rehearse the wake too. The
                # live path reaches it via step 8, which needs state == STOPPED
                # and so can never fire for a miner that was never stopped.
                # Skipping it would silently leave the half operators most want
                # to validate — "does my schedule actually wake the fleet?" —
                # untested, and would hold the latch forever.
                return await self._transition(miner, state, now, to_sleep=False)
            if (now - sleep_ts).total_seconds() < cfg.grace_seconds:
                # Real hardware does not drop to zero hashrate in the same poll
                # it accepts the sleep command; the rate decays over tens of
                # seconds. Treat this as still winding down.
                return True
            # Grace expired and it is still mining: the sleep did not take, or
            # somebody woke it by hand. Drop the stale belief.
            self._asleep.pop(miner.id, None)
            self._simulated.discard(miner.id)
            # Count it. The backends judge success from the command's own
            # acknowledgement, never from a follow-up reading, so a firmware
            # that cheerfully ACKs a sleep it does not implement would
            # otherwise loop sleep -> settle -> awake -> sleep forever with
            # nothing ever surfacing to an operator. This counter is separate
            # from the actuation-failure one precisely because each cycle
            # *succeeds* and would keep resetting it.
            ineffective = self._ineffective.get(miner.id, 0) + 1
            self._ineffective[miner.id] = ineffective
            self._record(
                miner.id,
                state,
                "awake",
                f"still hashing {int((now - sleep_ts).total_seconds())}s after sleep; "
                f"clearing asleep latch (ineffective sleep {ineffective})",
                now,
            )
            if ineffective >= cfg.max_failures:
                self._attention.add(miner.id)
                self._record(
                    miner.id,
                    state,
                    "sleep_needs_attention",
                    f"{ineffective} sleeps acknowledged but never took effect; "
                    f"the firmware may not support this backend - manual clear required",
                    now,
                )

        # 3. Repeated failures: stop trying, but keep owning a miner we still
        #    believe we slept so the watchdog does not start restarting it.
        if miner.id in self._attention:
            return miner.id in self._asleep

        # 4. Just woken and still spinning up: suppress the watchdog until the
        #    grace period expires, otherwise it would restart a healthy miner
        #    that simply has not reached full hashrate yet.
        wake_ts = self._waking.get(miner.id)
        if wake_ts is not None:
            if state == State.MINING:
                self._waking.pop(miner.id, None)
            elif (now - wake_ts).total_seconds() < cfg.grace_seconds:
                return True
            else:
                self._waking.pop(miner.id, None)

        # 5. We cannot tell what happened; let the watchdog apply its own
        #    policy rather than guessing.
        if state == State.UNREACHABLE:
            return False

        # 6/7. Outside the window.
        if not working:
            if state == State.MINING:
                return await self._transition(miner, state, now, to_sleep=True)
            # Already stopped. Owned only if this is our doing — and if it is,
            # the sleep demonstrably worked, so reset the ineffective counter.
            if miner.id in self._asleep:
                self._ineffective.pop(miner.id, None)
                return True
            return False

        # 8. Inside the window and stopped by us -> wake it back up.
        if state == State.STOPPED and miner.id in self._asleep:
            return await self._transition(miner, state, now, to_sleep=False)

        return False

    # ------------------------------------------------------------------
    # Manual actuation (CLI)
    # ------------------------------------------------------------------

    async def sleep_now(self, miner: Miner, now: datetime | None = None) -> tuple[bool, str]:
        """Sleep *miner* immediately, ignoring schedule and cooldown."""
        return await self._actuate(miner, State.MINING, _utcnow(now), to_sleep=True, forced=True)

    async def wake_now(self, miner: Miner, now: datetime | None = None) -> tuple[bool, str]:
        """Wake *miner* immediately, ignoring schedule and cooldown."""
        return await self._actuate(miner, State.STOPPED, _utcnow(now), to_sleep=False, forced=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _driver_for(self, miner: Miner) -> SleepBackendDriver:
        """Return (and cache) the driver for this miner's backend."""
        key = f"{miner.sleep.backend.value}:{miner.sleep.enabled}"
        driver = self._drivers.get(key)
        if driver is None:
            driver = get_backend(miner.sleep)
            self._drivers[key] = driver
        return driver

    def _is_dry_run(self, miner: Miner) -> bool:
        if self.dry_run_override is not None:
            return self.dry_run_override
        return miner.sleep.dry_run

    async def _transition(self, miner: Miner, state: State, now: datetime, to_sleep: bool) -> bool:
        """Cooldown-guarded wrapper around :meth:`_actuate`.

        Always returns ``True``: whether the attempt succeeded, was skipped for
        cooldown, or failed, this miner's state is the controller's business
        this cycle and the watchdog should keep its hands off.
        """
        label = "sleep" if to_sleep else "wake"
        last = self._last_attempt.get(miner.id)
        if last is not None and (now - last).total_seconds() < miner.sleep.cooldown_seconds:
            # Log the skip once per cooldown, not once per poll: a 5-minute
            # cooldown against a 15-second interval would otherwise write 20
            # identical rows and bury the events that matter.
            if self._cooldown_logged.get(miner.id) != last:
                self._cooldown_logged[miner.id] = last
                self._record(
                    miner.id,
                    state,
                    f"skipped_{label}_cooldown",
                    f"cooldown active; last attempt at {last.isoformat()}",
                    now,
                )
            return True
        await self._actuate(miner, state, now, to_sleep=to_sleep, forced=False)
        return True

    async def _actuate(
        self, miner: Miner, state: State, now: datetime, to_sleep: bool, forced: bool
    ) -> tuple[bool, str]:
        label = "sleep" if to_sleep else "wake"
        driver = self._driver_for(miner)
        self._last_attempt[miner.id] = now

        if self._is_dry_run(miner):
            # Record the intent and move the latch so the rest of the state
            # machine can be rehearsed end to end without touching hardware.
            # The latch is flagged simulated: the miner never actually stopped,
            # and the would_* event is excluded from hydration so a rehearsal
            # can never leave behind a belief a later live run acts on.
            detail = f"dry-run: would {label} {miner.host} via {driver.name}"
            self._apply_latch(miner.id, to_sleep, now, simulated=True)
            self._record(miner.id, state, f"would_{label}", detail, now)
            self._failures.pop(miner.id, None)
            return True, detail

        try:
            ok, detail = await (driver.sleep(miner) if to_sleep else driver.wake(miner))
        except Exception as exc:  # pragma: no cover - drivers already trap
            ok, detail = False, f"{type(exc).__name__}: {exc}"

        if ok:
            self._apply_latch(miner.id, to_sleep, now)
            self._failures.pop(miner.id, None)
            self._record(miner.id, state, label, detail, now)
        else:
            count = self._failures.get(miner.id, 0) + 1
            self._failures[miner.id] = count
            self._record(miner.id, state, f"{label}_failed", f"{detail} (failure {count})", now)
            if not forced and count >= miner.sleep.max_failures:
                self._attention.add(miner.id)
                self._record(
                    miner.id,
                    state,
                    "sleep_needs_attention",
                    f"{count} consecutive {label} failures; manual clear required",
                    now,
                )
        return ok, detail

    def _apply_latch(
        self, miner_id: str, to_sleep: bool, now: datetime, simulated: bool = False
    ) -> None:
        if to_sleep:
            self._asleep[miner_id] = now
            self._waking.pop(miner_id, None)
        else:
            self._asleep.pop(miner_id, None)
            self._waking[miner_id] = now
        if simulated:
            self._simulated.add(miner_id)
        else:
            self._simulated.discard(miner_id)

    def _record(
        self,
        miner_id: str,
        state: State,
        action: str,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        ts = _utcnow(now)
        record_event(
            self.conn,
            Event(ts=ts.isoformat(), miner=miner_id, state=state.value, action=action, reason=reason),
        )
        logger.info("%s: %s (%s)", miner_id, action, reason)


def _utcnow(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
