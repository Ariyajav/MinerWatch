import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from minerwatch.models import Event, Miner, State, Schedule, Window, Range
from minerwatch.store import init_db, is_needs_attention, record_event
from minerwatch.watchdog import Watchdog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def always_working_miner(id="test", host="127.0.0.1", port=9999):
    return Miner(
        id=id, host=host, port=port,
        schedule=Schedule(
            timezone=timezone.utc,
            windows=[Window(days=frozenset(range(7)), ranges=[Range(start=0, end=1440)])],
        ),
    )


def never_working_miner(id="test-off"):
    return Miner(id=id, host="127.0.0.1", port=9999, schedule=None)


def t(ts: float) -> datetime:
    """Create a UTC datetime from a Unix timestamp."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


async def ok_restart(miner):
    return (True, "restart sent")


async def fail_restart(miner):
    return (False, "connection refused")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWatchdog:
    """4.1 — STOPPED inside hours (dry_run=False) → actuator called, action='restart'."""
    @pytest.mark.asyncio
    async def test_restart_stopped_inside_hours(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=1, rate_window=10, max_restarts=3)
        wd.send_restart = AsyncMock(side_effect=ok_restart)
        miner = always_working_miner()
        await wd.consider(miner, State.STOPPED, True, t(1000), "test")
        wd.send_restart.assert_awaited_once_with(miner)
        row = conn.execute("SELECT action FROM events WHERE miner=?", (miner.id,)).fetchone()
        assert row[0] == "restart"

    """4.2 — STOPPED outside hours → NOT called, action='skipped_outside_hours'."""
    @pytest.mark.asyncio
    async def test_skip_outside_hours(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=1, rate_window=10, max_restarts=3)
        mock = AsyncMock()
        wd.send_restart = mock
        miner = never_working_miner()
        await wd.consider(miner, State.STOPPED, False, t(1000), "test")
        mock.assert_not_called()
        row = conn.execute("SELECT action FROM events WHERE miner=?", (miner.id,)).fetchone()
        assert row[0] == "skipped_outside_hours"

    """4.3 — UNREACHABLE inside hours → restart attempted."""
    @pytest.mark.asyncio
    async def test_restart_unreachable_inside_hours(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=1, rate_window=10, max_restarts=3)
        wd.send_restart = AsyncMock(side_effect=ok_restart)
        miner = always_working_miner()
        await wd.consider(miner, State.UNREACHABLE, True, t(1000), "no route")
        wd.send_restart.assert_awaited_once_with(miner)
        row = conn.execute("SELECT action FROM events WHERE miner=?", (miner.id,)).fetchone()
        assert row[0] == "restart"

    """4.4 — Restart at t0; t0+5m → skipped_cooldown; t0+11m → called again."""
    @pytest.mark.asyncio
    async def test_cooldown(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=600, rate_window=3600, max_restarts=3)
        wd.send_restart = AsyncMock(side_effect=ok_restart)
        miner = always_working_miner()
        base = 1000.0

        # First restart at t0
        await wd.consider(miner, State.STOPPED, True, t(base), "first")
        assert wd.send_restart.await_count == 1

        # t0+5m → within 600s cooldown
        await wd.consider(miner, State.STOPPED, True, t(base + 300), "cooldown")
        assert wd.send_restart.await_count == 1  # not called again
        row = conn.execute(
            "SELECT action FROM events WHERE miner=? ORDER BY ts DESC LIMIT 1",
            (miner.id,),
        ).fetchone()
        assert row[0] == "skipped_cooldown"

        # t0+11m → past 600s cooldown
        await wd.consider(miner, State.STOPPED, True, t(base + 660), "retry")
        assert wd.send_restart.await_count == 2

    """4.5 — 3 restarts (t, t+11, t+22), 4th → needs_attention, subsequent → skipped."""
    @pytest.mark.asyncio
    async def test_rate_limit_needs_attention(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=600, rate_window=3600, max_restarts=3)
        wd.send_restart = AsyncMock(side_effect=ok_restart)
        miner = always_working_miner()
        base = 2000.0

        # 3 successful restarts at 11-min intervals
        for i in range(3):
            await wd.consider(miner, State.STOPPED, True, t(base + i * 660), f"attempt {i}")
        assert wd.send_restart.await_count == 3

        # 4th → needs_attention, no actuation
        await wd.consider(miner, State.STOPPED, True, t(base + 3 * 660), "4th")
        assert wd.send_restart.await_count == 3  # not called
        assert miner.id in wd._needs_attention
        row = conn.execute(
            "SELECT action FROM events WHERE miner=? ORDER BY ts DESC LIMIT 1",
            (miner.id,),
        ).fetchone()
        assert row[0] == "needs_attention"

        # Subsequent → skipped_needs_attention
        await wd.consider(miner, State.STOPPED, True, t(base + 4 * 660), "5th")
        row = conn.execute(
            "SELECT action FROM events WHERE miner=? ORDER BY ts DESC LIMIT 1",
            (miner.id,),
        ).fetchone()
        assert row[0] == "skipped_needs_attention"

    """4.6 — After clear → next failing poll blocked by cooldown (deque preserved, not reset)."""
    @pytest.mark.asyncio
    async def test_clear_attention(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=600, rate_window=3600, max_restarts=3)
        wd.send_restart = AsyncMock(side_effect=ok_restart)
        miner = always_working_miner()
        base = 3000.0

        # 3 restarts at 11-min intervals → deque has 3 entries
        # 4th call (at base+1980) trips needs_attention (does NOT append to deque)
        for i in range(3):
            await wd.consider(miner, State.STOPPED, True, t(base + i * 660), f"attempt {i}")
        await wd.consider(miner, State.STOPPED, True, t(base + 3 * 660), "trip")
        assert miner.id in wd._needs_attention
        assert wd.send_restart.await_count == 3

        # Clear latch — deque is trimmed to max_restarts - 1, not reset
        wd.clear_attention(miner.id)
        assert miner.id not in wd._needs_attention
        assert len(wd._get_dq(miner.id)) == 2  # trimmed to max_restarts - 1

        # Call at base+1320+300 = base+1620: only 300s after last attempt (1320),
        # well within 600s cooldown → blocked
        await wd.consider(miner, State.STOPPED, True, t(base + 1620), "again")
        assert wd.send_restart.await_count == 3  # blocked by cooldown
        assert conn.execute(
            "SELECT action FROM events WHERE miner=? AND action='skipped_cooldown'",
            (miner.id,),
        ).fetchone() is not None

    """4.6b — Clear attention trims deque → restart allowed after cooldown."""
    @pytest.mark.asyncio
    async def test_clear_attention_allows_restart_after_cooldown(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=600, rate_window=3600, max_restarts=3)
        wd.send_restart = AsyncMock(side_effect=ok_restart)
        miner = always_working_miner()
        base = 3000.0

        # 3 restarts at 11-min intervals → deque has 3 entries
        for i in range(3):
            await wd.consider(miner, State.STOPPED, True, t(base + i * 660), f"attempt {i}")
        assert wd.send_restart.await_count == 3
        dq = wd._get_dq(miner.id)
        assert len(dq) == 3

        # 4th call trips needs_attention
        await wd.consider(miner, State.STOPPED, True, t(base + 3 * 660), "trip")
        assert miner.id in wd._needs_attention

        # Clear attention → deque trimmed to max_restarts - 1 = 2 entries
        wd.clear_attention(miner.id)
        assert miner.id not in wd._needs_attention
        assert len(dq) == 2, f"expected 2 but got {len(dq)}"

        # Advance time past cooldown from last restart (11 min → 660s > 600s)
        # but NOT past the rate_window (last entry is at base+1320, now = base+2000 → 680s later)
        await wd.consider(miner, State.STOPPED, True, t(base + 2000), "restart after clear")
        assert wd.send_restart.await_count == 4  # restart IS allowed
        assert miner.id not in wd._needs_attention  # no needs_attention triggered

    """4.6c — Edge case: max_restarts=1 → clear_attention retains the sole entry (cooldown preserved)."""
    @pytest.mark.asyncio
    async def test_clear_attention_max_restarts_1(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=600, rate_window=3600, max_restarts=1)
        wd.send_restart = AsyncMock(side_effect=ok_restart)
        miner = always_working_miner()
        base = 5000.0

        # One restart call → deque has 1 entry
        await wd.consider(miner, State.STOPPED, True, t(base), "attempt 1")
        assert wd.send_restart.await_count == 1
        assert len(wd._get_dq(miner.id)) == 1

        # Advance past cooldown, then call again → trips needs_attention (1 ≥ max_restarts=1)
        await wd.consider(miner, State.STOPPED, True, t(base + 700), "trip")
        assert miner.id in wd._needs_attention

        # Clear attention — deque should still have 1 entry (cooldown preserved)
        wd.clear_attention(miner.id)
        assert miner.id not in wd._needs_attention
        assert len(wd._get_dq(miner.id)) == 1, "deque emptied — cooldown NOT preserved"

        # Restart blocked by cooldown (only 700s past last attempt, cooldown=600 → still within?
        # Actually 700 > 600, so cooldown is satisfied. But needs_attention was cleared.
        # After clear, deque has 1 entry, len(dq) >= 1 → triggers needs_attention again.
        # So third call should re-latch, not restart.
        await wd.consider(miner, State.STOPPED, True, t(base + 700), "blocked")
        assert wd.send_restart.await_count == 1  # not restarted (re-latched)
        assert miner.id in wd._needs_attention

        # Clear again, then advance past rate_window so the one entry evicts
        wd.clear_attention(miner.id)
        assert len(wd._get_dq(miner.id)) == 1

        # Advance 3601s past the first entry → evict, deque empty → restart allowed
        await wd.consider(miner, State.STOPPED, True, t(base + 3601), "allowed")
        assert wd.send_restart.await_count == 2

    """4.7 — 3 restarts, advance past 1h → oldest evicted, new restart allowed."""
    @pytest.mark.asyncio
    async def test_eviction_allows_new_restart(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=600, rate_window=3600, max_restarts=3)
        wd.send_restart = AsyncMock(side_effect=ok_restart)
        miner = always_working_miner()
        base = 4000.0

        # 3 restarts at 11-min intervals → dq has 3 entries
        for i in range(3):
            await wd.consider(miner, State.STOPPED, True, t(base + i * 660), f"attempt {i}")
        assert wd.send_restart.await_count == 3
        assert len(wd._get_dq(miner.id)) == 3

        # Advance past 1h from first entry → first entry evicted
        far = t(base + 3601)
        await wd.consider(miner, State.STOPPED, True, far, "evicted")
        assert wd.send_restart.await_count == 4  # new restart allowed
        assert len(wd._get_dq(miner.id)) == 3  # 2 old + 1 new

    """4.8 — dry_run=True: NOT called, action='would_restart', deque advances."""
    @pytest.mark.asyncio
    async def test_dry_run_default(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=True, cooldown=1, rate_window=10, max_restarts=3)
        mock = AsyncMock()
        wd.send_restart = mock
        miner = always_working_miner()
        await wd.consider(miner, State.STOPPED, True, t(5000), "dry")
        mock.assert_not_called()
        row = conn.execute("SELECT action FROM events WHERE miner=?", (miner.id,)).fetchone()
        assert row[0] == "would_restart"
        # deque should have advanced
        assert len(wd._get_dq(miner.id)) == 1

    """4.9 — MINING → no watchdog event."""
    @pytest.mark.asyncio
    async def test_mining_noop(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=1, rate_window=10, max_restarts=3)
        mock = AsyncMock()
        wd.send_restart = mock
        miner = always_working_miner()
        await wd.consider(miner, State.MINING, True, t(6000), "mining ok")
        mock.assert_not_called()
        count = conn.execute("SELECT COUNT(*) FROM events WHERE miner=?", (miner.id,)).fetchone()[0]
        assert count == 0

    """4.10 — Actuator failure → restart_failed, counts toward cooldown & limit, escalates."""
    @pytest.mark.asyncio
    async def test_actuator_failure_escalates(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=600, rate_window=3600, max_restarts=3)
        wd.send_restart = AsyncMock(side_effect=fail_restart)
        miner = always_working_miner()
        base = 7000.0

        for i in range(3):
            await wd.consider(miner, State.STOPPED, True, t(base + i * 660), f"fail {i}")
        # All three were actuated but failed
        assert wd.send_restart.await_count == 3
        row = conn.execute(
            "SELECT action FROM events WHERE miner=? ORDER BY ts DESC LIMIT 1",
            (miner.id,),
        ).fetchone()
        assert row[0] == "restart_failed"

        # 4th → needs_attention (3 failed attempts hit max_restarts)
        await wd.consider(miner, State.STOPPED, True, t(base + 3 * 660), "trip")
        assert miner.id in wd._needs_attention
        row = conn.execute(
            "SELECT action FROM events WHERE miner=? ORDER BY ts DESC LIMIT 1",
            (miner.id,),
        ).fetchone()
        assert row[0] == "needs_attention"

    """4.11 — 2 failed + 1 success = 3 total → next trip needs_attention (mix counts)."""
    @pytest.mark.asyncio
    async def test_mixed_success_failure_counts(self, conn):
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=600, rate_window=3600, max_restarts=3)
        miner = always_working_miner()
        base = 8000.0

        # 2 failed restarts
        wd.send_restart = AsyncMock(side_effect=fail_restart)
        for i in range(2):
            await wd.consider(miner, State.STOPPED, True, t(base + i * 660), f"fail {i}")
            row = conn.execute(
                "SELECT action FROM events WHERE miner=? ORDER BY ts DESC LIMIT 1",
                (miner.id,),
            ).fetchone()
            assert row[0] == "restart_failed"

        # 1 successful restart
        wd.send_restart = AsyncMock(side_effect=ok_restart)
        await wd.consider(miner, State.STOPPED, True, t(base + 2 * 660), "success")
        row = conn.execute(
            "SELECT action FROM events WHERE miner=? ORDER BY ts DESC LIMIT 1",
            (miner.id,),
        ).fetchone()
        assert row[0] == "restart"

        # 3 total attempts now (2 fail + 1 success) in window
        dq = wd._get_dq(miner.id)
        assert len(dq) == 3

        # 4th attempt → needs_attention (3 ≥ max_restarts)
        wd.send_restart = AsyncMock()
        await wd.consider(miner, State.STOPPED, True, t(base + 3 * 660), "trip")
        assert miner.id in wd._needs_attention
        row = conn.execute(
            "SELECT action FROM events WHERE miner=? ORDER BY ts DESC LIMIT 1",
            (miner.id,),
        ).fetchone()
        assert row[0] == "needs_attention"

    """Hydration — deques rebuilt from events table on startup."""
    @pytest.mark.asyncio
    async def test_hydration_rebuilds_deque(self, conn):
        """Watchdog hydrates attempt deques from restart/restart_failed events at startup."""
        miner = always_working_miner()
        base = 9000.0

        # Pre-populate events as if 2 restarts happened before restart
        for i in range(2):
            ev = Event(
                ts=t(base + i * 660).isoformat(),
                miner=miner.id,
                state="stopped",
                action="restart",
                reason=f"restart {i}",
            )
            record_event(conn, ev)

        # Fresh watchdog rebuilds deque from DB (rate_window covers all test timestamps)
        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=1, rate_window=3600, max_restarts=3)
        wd.send_restart = AsyncMock(side_effect=ok_restart)

        dq = wd._get_dq(miner.id)
        assert len(dq) == 2, "should have 2 pre-populated attempts"

        # 3rd attempt should succeed (2 < 3 max_restarts)
        await wd.consider(miner, State.STOPPED, True, t(base + 2 * 660), "3rd")
        assert wd.send_restart.await_count == 1

        # 4th attempt → needs_attention (3 ≥ max_restarts)
        await wd.consider(miner, State.STOPPED, True, t(base + 3 * 660), "4th")
        assert miner.id in wd._needs_attention

    @pytest.mark.asyncio
    async def test_hydration_skips_expired(self, conn):
        """Old restart events are evicted when consider runs with current time."""
        miner = always_working_miner()
        base = 10000.0

        # Pre-populate: 1 old event (2h ago) and 1 recent event (5min ago)
        old = Event(ts=t(base - 7200).isoformat(), miner=miner.id, state="stopped", action="restart", reason="old")
        recent = Event(ts=t(base - 300).isoformat(), miner=miner.id, state="stopped", action="restart_failed", reason="recent")
        record_event(conn, old)
        record_event(conn, recent)

        wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=1, rate_window=3600, max_restarts=3)
        wd.send_restart = AsyncMock(side_effect=ok_restart)

        # Hydration loads all events; consider triggers eviction of old ones
        dq = wd._get_dq(miner.id)
        assert len(dq) == 2, "hydration loads all restart/restart_failed events"

        # Call consider with now aligned to recent event → old (>3600s) gets evicted,
        # but a new entry (would_restart) is added in its place
        await wd.consider(miner, State.STOPPED, True, t(base), "now")
        assert len(dq) == 2, "old evicted, new entry added for current attempt"
        # dq[0] should be the recent hydrated event's timestamp
        assert dq[0] == t(base - 300), "recent restart_failed timestamp preserved"


class TestClearAttentionDurability:
    """A manual clear must survive a service restart.

    Hydration replays every restart attempt in the rate window, so without
    reapplying the clear the operator's action is silently undone: the deque
    comes back full and the miner re-latches on its very next attempt instead
    of getting its retry budget back. On the Windows host a restart is routine
    (a reboot, a config edit, Task Scheduler), so this is not a rare path.
    """

    async def test_cleared_latch_restores_the_retry_budget_after_a_restart(self, conn):
        miner = always_working_miner()
        w1 = Watchdog(conn, fail_after=0, dry_run=True, cooldown=0, max_restarts=3)
        for i in range(3):
            await w1.consider(miner, State.STOPPED, True, t(i * 10), "")
        # 4th attempt trips the latch.
        await w1.consider(miner, State.STOPPED, True, t(40), "")
        assert w1._needs_attention

        w1.clear_attention(miner.id)
        assert len(w1._attempts[miner.id]) < 3

        w2 = Watchdog(conn, fail_after=0, dry_run=True, cooldown=0, max_restarts=3)
        assert miner.id not in w2._needs_attention
        assert len(w2._attempts.get(miner.id, [])) < 3, "the clear was undone by hydration"

        # And it genuinely gets another attempt rather than re-latching at once.
        await w2.consider(miner, State.STOPPED, True, t(100), "")
        assert miner.id not in w2._needs_attention

    async def test_an_uncleared_latch_still_survives_a_restart(self, conn):
        miner = always_working_miner(id="still-latched")
        w1 = Watchdog(conn, fail_after=0, dry_run=True, cooldown=0, max_restarts=2)
        for i in range(3):
            await w1.consider(miner, State.STOPPED, True, t(i * 10), "")
        assert miner.id in w1._needs_attention

        w2 = Watchdog(conn, fail_after=0, dry_run=True, cooldown=0, max_restarts=2)
        assert miner.id in w2._needs_attention
