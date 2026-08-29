"""Tests for the schedule-driven sleep controller.

The controller's job is to make two things true: a miner outside its window is
stopped in software, and a miner MinerWatch stopped is never mistaken for a
failure by the watchdog. Most tests here are about that second property,
because it is the one that would otherwise cause a restart storm at every
window boundary.
"""

from datetime import datetime, timedelta, timezone

import pytest

from minerwatch.models import (
    Command,
    Miner,
    Range,
    Schedule,
    SleepBackend,
    SleepConfig,
    State,
    Window,
)
from minerwatch.sleeper import SleepController
from minerwatch.store import init_db, last_action_in


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


ALWAYS = Schedule(
    timezone=timezone.utc,
    windows=[Window(days=frozenset(range(7)), ranges=[Range(start=0, end=1440)])],
)


def miner(mid="m1", enabled=True, schedule=ALWAYS, **sleep_kwargs):
    defaults = dict(enabled=enabled, backend=SleepBackend.CGMINER, dry_run=True, cooldown_seconds=300)
    return Miner(
        id=mid,
        host="127.0.0.1",
        port=4028,
        schedule=schedule,
        sleep=SleepConfig(**{**defaults, **sleep_kwargs}),
    )


def t(seconds: float) -> datetime:
    return datetime.fromtimestamp(1_700_000_000 + seconds, tz=timezone.utc)


def actions(conn, miner_id):
    rows = conn.execute(
        "SELECT action FROM events WHERE miner = ? ORDER BY id", (miner_id,)
    ).fetchall()
    return [r[0] for r in rows]


class RecordingDriver:
    """Stand-in driver that records calls and can be made to fail."""

    name = "recording"

    def __init__(self, ok=True, detail="done"):
        self.ok = ok
        self.detail = detail
        self.calls = []

    async def sleep(self, m):
        self.calls.append(("sleep", m.id))
        return self.ok, self.detail

    async def wake(self, m):
        self.calls.append(("wake", m.id))
        return self.ok, self.detail


def with_driver(controller, driver):
    """Force every backend lookup to return *driver*."""
    controller._driver_for = lambda m: driver
    return driver


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

class TestGating:
    async def test_disabled_miner_is_never_owned(self, conn):
        c = SleepController(conn)
        m = miner(enabled=False)
        assert await c.consider(m, State.MINING, working=False, now=t(0)) is False
        assert actions(conn, m.id) == []

    async def test_miner_without_a_schedule_is_left_to_the_watchdog(self, conn):
        c = SleepController(conn)
        m = miner(schedule=None)
        assert await c.consider(m, State.STOPPED, working=False, now=t(0)) is False

    async def test_unreachable_miner_is_left_to_the_watchdog(self, conn):
        c = SleepController(conn)
        m = miner()
        assert await c.consider(m, State.UNREACHABLE, working=True, now=t(0)) is False

    async def test_mining_inside_the_window_is_not_owned(self, conn):
        c = SleepController(conn)
        m = miner()
        assert await c.consider(m, State.MINING, working=True, now=t(0)) is False

    async def test_stopped_inside_window_without_a_latch_is_a_real_failure(self, conn):
        """A miner we did not sleep must still reach the watchdog."""
        c = SleepController(conn)
        m = miner()
        assert await c.consider(m, State.STOPPED, working=True, now=t(0)) is False


# ---------------------------------------------------------------------------
# Sleeping at the end of a window
# ---------------------------------------------------------------------------

class TestSleep:
    async def test_dry_run_records_intent_without_calling_the_driver(self, conn):
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver())
        m = miner(dry_run=True)

        assert await c.consider(m, State.MINING, working=False, now=t(0)) is True
        assert driver.calls == []
        assert actions(conn, m.id) == ["would_sleep"]
        assert c.is_asleep(m.id)

    async def test_live_run_calls_the_driver(self, conn):
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver())
        m = miner(dry_run=False)

        assert await c.consider(m, State.MINING, working=False, now=t(0)) is True
        assert driver.calls == [("sleep", "m1")]
        assert actions(conn, m.id) == ["sleep"]
        assert c.is_asleep(m.id)

    async def test_override_forces_dry_run_regardless_of_config(self, conn):
        c = SleepController(conn, dry_run=True)
        driver = with_driver(c, RecordingDriver())
        m = miner(dry_run=False)  # config says live
        await c.consider(m, State.MINING, working=False, now=t(0))
        assert driver.calls == []

    async def test_override_forces_live_regardless_of_config(self, conn):
        c = SleepController(conn, dry_run=False)
        driver = with_driver(c, RecordingDriver())
        m = miner(dry_run=True)  # config says rehearse
        await c.consider(m, State.MINING, working=False, now=t(0))
        assert driver.calls == [("sleep", "m1")]

    async def test_already_stopped_outside_window_takes_no_action(self, conn):
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver())
        m = miner()
        # Not ours: no latch, so ownership is declined and nothing is sent.
        assert await c.consider(m, State.STOPPED, working=False, now=t(0)) is False
        assert driver.calls == []

    async def test_cooldown_blocks_a_second_attempt(self, conn):
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver(ok=False, detail="nope"))
        m = miner(dry_run=False, cooldown_seconds=300)

        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.MINING, working=False, now=t(60))
        assert driver.calls == [("sleep", "m1")]
        assert actions(conn, m.id) == ["sleep_failed", "skipped_sleep_cooldown"]

    async def test_cooldown_expires(self, conn):
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver(ok=False, detail="nope"))
        m = miner(dry_run=False, cooldown_seconds=300)

        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.MINING, working=False, now=t(301))
        assert driver.calls == [("sleep", "m1"), ("sleep", "m1")]


# ---------------------------------------------------------------------------
# Waking at the start of a window
# ---------------------------------------------------------------------------

class TestWake:
    async def test_window_opening_wakes_a_slept_miner(self, conn):
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver())
        m = miner(dry_run=False, cooldown_seconds=0)

        await c.consider(m, State.MINING, working=False, now=t(0))       # sleep
        assert await c.consider(m, State.STOPPED, working=True, now=t(10)) is True
        assert driver.calls == [("sleep", "m1"), ("wake", "m1")]
        assert not c.is_asleep(m.id)

    async def test_spin_up_grace_shields_the_watchdog(self, conn):
        """After a wake the miner is still at zero hashrate for a minute or two."""
        c = SleepController(conn)
        with_driver(c, RecordingDriver())
        m = miner(dry_run=False, cooldown_seconds=0, grace_seconds=180)

        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.STOPPED, working=True, now=t(10))      # wake
        # Still stopped 60s later: owned, so the watchdog does not restart it.
        assert await c.consider(m, State.STOPPED, working=True, now=t(70)) is True

    async def test_grace_expiry_hands_a_still_dead_miner_to_the_watchdog(self, conn):
        c = SleepController(conn)
        with_driver(c, RecordingDriver())
        m = miner(dry_run=False, cooldown_seconds=0, grace_seconds=180)

        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.STOPPED, working=True, now=t(10))      # wake
        # The wake did not take: after the grace window this is a real fault.
        assert await c.consider(m, State.STOPPED, working=True, now=t(500)) is False

    async def test_successful_wake_clears_grace_once_hashing(self, conn):
        c = SleepController(conn)
        with_driver(c, RecordingDriver())
        m = miner(dry_run=False, cooldown_seconds=0)

        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.STOPPED, working=True, now=t(10))
        assert await c.consider(m, State.MINING, working=True, now=t(60)) is False


# ---------------------------------------------------------------------------
# Latch behaviour
# ---------------------------------------------------------------------------

class TestLatch:
    async def test_externally_woken_miner_clears_the_latch(self, conn):
        """Somebody pressed the button on the miner; drop our stale belief."""
        c = SleepController(conn)
        with_driver(c, RecordingDriver())
        m = miner(dry_run=False, grace_seconds=180)

        await c.consider(m, State.MINING, working=False, now=t(0))
        assert c.is_asleep(m.id)
        # Window has since reopened and the miner is hashing well past the
        # settle window: our belief is stale and must be dropped.
        assert await c.consider(m, State.MINING, working=True, now=t(300)) is False
        assert not c.is_asleep(m.id)
        assert "awake" in actions(conn, m.id)

    async def test_latch_survives_a_restart(self, conn):
        """The asleep latch is rebuilt from the event log, not held in RAM."""
        c1 = SleepController(conn)
        with_driver(c1, RecordingDriver())
        m = miner(dry_run=False)
        await c1.consider(m, State.MINING, working=False, now=t(0))

        c2 = SleepController(conn, {m.id: m})
        assert c2.is_asleep(m.id)
        # And it is therefore still owned rather than looking like a failure.
        assert await c2.consider(m, State.STOPPED, working=False, now=t(60)) is True

    async def test_wake_recorded_before_restart_clears_the_latch(self, conn):
        c1 = SleepController(conn)
        with_driver(c1, RecordingDriver())
        m = miner(dry_run=False, cooldown_seconds=0)
        await c1.consider(m, State.MINING, working=False, now=t(0))
        await c1.consider(m, State.STOPPED, working=True, now=t(10))

        c2 = SleepController(conn, {m.id: m})
        assert not c2.is_asleep(m.id)

    async def test_dry_run_latch_does_not_persist(self, conn):
        """A rehearsal must not leave behind a belief a live run acts on.

        The miner was never touched, so a fresh controller that trusted
        ``would_sleep`` would stand the watchdog down for a device that is
        genuinely dead.
        """
        c1 = SleepController(conn)
        with_driver(c1, RecordingDriver())
        m = miner(dry_run=True)
        await c1.consider(m, State.MINING, working=False, now=t(0))
        assert c1.is_asleep(m.id)                      # rehearsed in-process
        assert not SleepController(conn, {m.id: m}).is_asleep(m.id)   # not durable

    async def test_dry_run_does_not_suppress_the_watchdog_after_a_restart(self, conn):
        c1 = SleepController(conn, dry_run=True)
        with_driver(c1, RecordingDriver())
        m = miner(dry_run=True)
        await c1.consider(m, State.MINING, working=False, now=t(0))

        c2 = SleepController(conn, {m.id: m})
        # Genuinely stopped inside its window: must reach the watchdog.
        assert await c2.consider(m, State.STOPPED, working=True, now=t(60)) is False


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestFailures:
    async def test_repeated_failures_latch_for_attention(self, conn):
        c = SleepController(conn)
        with_driver(c, RecordingDriver(ok=False, detail="refused"))
        m = miner(dry_run=False, cooldown_seconds=0, max_failures=3)

        for i in range(3):
            await c.consider(m, State.MINING, working=False, now=t(i * 10))
        assert c.needs_attention(m.id)
        assert actions(conn, m.id).count("sleep_failed") == 3
        assert "sleep_needs_attention" in actions(conn, m.id)

    async def test_latched_miner_stops_being_retried(self, conn):
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver(ok=False))
        m = miner(dry_run=False, cooldown_seconds=0, max_failures=2)

        for i in range(4):
            await c.consider(m, State.MINING, working=False, now=t(i * 10))
        assert len(driver.calls) == 2  # stopped trying after the latch

    async def test_a_success_resets_the_failure_count(self, conn):
        """Two failures, a success, then two more failures must not latch.

        Without a reset the third failure overall would trip max_failures=3 and
        take the miner out of automatic control for no good reason.
        """
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver(ok=False))
        m = miner(dry_run=False, cooldown_seconds=0, max_failures=3)

        await c.consider(m, State.MINING, working=False, now=t(0))   # fail 1
        await c.consider(m, State.MINING, working=False, now=t(10))  # fail 2
        driver.ok = True
        await c.consider(m, State.MINING, working=False, now=t(20))  # success
        assert c.is_asleep(m.id)

        driver.ok = False
        await c.consider(m, State.STOPPED, working=True, now=t(30))  # fail 1 again
        await c.consider(m, State.STOPPED, working=True, now=t(40))  # fail 2 again
        assert not c.needs_attention(m.id)
        await c.consider(m, State.STOPPED, working=True, now=t(50))  # fail 3
        assert c.needs_attention(m.id)

    async def test_clear_attention_releases_the_latch(self, conn):
        c = SleepController(conn)
        with_driver(c, RecordingDriver(ok=False))
        m = miner(dry_run=False, cooldown_seconds=0, max_failures=1)

        await c.consider(m, State.MINING, working=False, now=t(0))
        assert c.needs_attention(m.id)
        c.clear_attention(m.id)
        assert not c.needs_attention(m.id)
        assert actions(conn, m.id)[-1] == "sleep_attention_cleared"

    async def test_attention_latch_survives_a_restart(self, conn):
        c1 = SleepController(conn)
        with_driver(c1, RecordingDriver(ok=False))
        m = miner(dry_run=False, cooldown_seconds=0, max_failures=1)
        await c1.consider(m, State.MINING, working=False, now=t(0))

        assert SleepController(conn, {m.id: m}).needs_attention(m.id)

    async def test_cleared_attention_survives_a_restart(self, conn):
        c1 = SleepController(conn)
        with_driver(c1, RecordingDriver(ok=False))
        m = miner(dry_run=False, cooldown_seconds=0, max_failures=1)
        await c1.consider(m, State.MINING, working=False, now=t(0))
        c1.clear_attention(m.id)

        assert not SleepController(conn, {m.id: m}).needs_attention(m.id)

    async def test_driver_exception_is_contained(self, conn):
        class Exploding:
            name = "exploding"

            async def sleep(self, m):
                raise RuntimeError("boom")

            async def wake(self, m):
                raise RuntimeError("boom")

        c = SleepController(conn)
        with_driver(c, Exploding())
        m = miner(dry_run=False)
        assert await c.consider(m, State.MINING, working=False, now=t(0)) is True
        assert actions(conn, m.id) == ["sleep_failed"]

    async def test_latched_miner_still_shields_a_sleeping_one(self, conn):
        """A miner latched after a *wake* failure is still believed asleep."""
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver())
        m = miner(dry_run=False, cooldown_seconds=0, max_failures=1)
        await c.consider(m, State.MINING, working=False, now=t(0))   # slept OK
        driver.ok = False
        await c.consider(m, State.STOPPED, working=True, now=t(10))  # wake fails
        assert c.needs_attention(m.id)
        assert await c.consider(m, State.STOPPED, working=True, now=t(20)) is True


# ---------------------------------------------------------------------------
# Manual control
# ---------------------------------------------------------------------------

class TestManualControl:
    async def test_sleep_now_ignores_schedule_and_cooldown(self, conn):
        c = SleepController(conn, dry_run=False)
        driver = with_driver(c, RecordingDriver())
        m = miner(dry_run=True, cooldown_seconds=99999, schedule=None)

        ok, _ = await c.sleep_now(m, now=t(0))
        assert ok and driver.calls == [("sleep", "m1")]
        ok, _ = await c.sleep_now(m, now=t(1))
        assert ok and len(driver.calls) == 2

    async def test_wake_now_clears_the_latch(self, conn):
        c = SleepController(conn, dry_run=False)
        with_driver(c, RecordingDriver())
        m = miner()
        await c.sleep_now(m, now=t(0))
        assert c.is_asleep(m.id)
        await c.wake_now(m, now=t(1))
        assert not c.is_asleep(m.id)

    async def test_manual_failure_does_not_latch_attention(self, conn):
        """An operator watching the CLI does not need a latch as well."""
        c = SleepController(conn, dry_run=False)
        with_driver(c, RecordingDriver(ok=False))
        m = miner(max_failures=1)
        ok, _ = await c.sleep_now(m, now=t(0))
        assert not ok
        assert not c.needs_attention(m.id)


# ---------------------------------------------------------------------------
# Regressions found in review
# ---------------------------------------------------------------------------

class TestSettleWindow:
    """Real hardware keeps hashing for a while after accepting a sleep."""

    async def test_still_hashing_right_after_sleep_keeps_the_latch(self, conn):
        c = SleepController(conn)
        with_driver(c, RecordingDriver())
        m = miner(dry_run=False, grace_seconds=180)

        await c.consider(m, State.MINING, working=False, now=t(0))
        # Next poll, 15s later: hashrate has not decayed yet.
        assert await c.consider(m, State.MINING, working=False, now=t(15)) is True
        assert c.is_asleep(m.id)
        assert "awake" not in actions(conn, m.id)

    async def test_window_reopens_after_a_slow_stop_and_the_miner_is_woken(self, conn):
        """The end-to-end failure the settle window prevents.

        Without it the latch was dropped on the poll right after the sleep, so
        when the window reopened nothing woke the miner - and for a persistent
        backend like bitmain_http a watchdog restart does not wake it either.
        """
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver())
        m = miner(dry_run=False, grace_seconds=180, cooldown_seconds=0)

        await c.consider(m, State.MINING, working=False, now=t(0))    # sleep sent
        await c.consider(m, State.MINING, working=False, now=t(15))   # still spinning down
        await c.consider(m, State.STOPPED, working=False, now=t(30))  # now actually stopped
        assert c.is_asleep(m.id)

        assert await c.consider(m, State.STOPPED, working=True, now=t(3600)) is True
        assert driver.calls == [("sleep", "m1"), ("wake", "m1")]

    async def test_dry_run_does_not_flip_flop(self, conn):
        """A rehearsal must not churn the event log once per poll.

        Nothing is sent, so the miner keeps hashing forever; the controller has
        to keep believing its own pretend latch instead of recording
        would_sleep / awake / would_sleep / awake indefinitely.
        """
        c = SleepController(conn, dry_run=True)
        with_driver(c, RecordingDriver())
        m = miner(dry_run=True, grace_seconds=0, cooldown_seconds=0)

        for i in range(40):
            assert await c.consider(m, State.MINING, working=False, now=t(i * 15)) is True

        recorded = actions(conn, m.id)
        assert recorded == ["would_sleep"], recorded


class TestAttentionOrdering:
    async def test_latched_miner_still_sheds_a_stale_asleep_latch(self, conn):
        """Otherwise both controllers stand down forever.

        A miner latched after failed wakes keeps its asleep latch; if the
        attention check ran first, nothing could ever clear it, so the sleeper
        would refuse to retry and the watchdog would be told the state is owned.
        """
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver())
        m = miner(dry_run=False, cooldown_seconds=0, grace_seconds=0, max_failures=1)

        await c.consider(m, State.MINING, working=False, now=t(0))    # slept OK
        driver.ok = False
        await c.consider(m, State.STOPPED, working=True, now=t(10))   # wake fails -> latched
        assert c.needs_attention(m.id) and c.is_asleep(m.id)

        # An operator wakes it by hand and it hashes normally.
        assert await c.consider(m, State.MINING, working=True, now=t(20)) is False
        assert not c.is_asleep(m.id)

        # A later genuine failure must now reach the watchdog.
        assert await c.consider(m, State.STOPPED, working=True, now=t(30)) is False


class TestHydrationCompleteness:
    async def test_spin_up_grace_survives_a_restart(self, conn):
        """Otherwise a restart mid-spin-up hands a healthy miner to the watchdog."""
        c1 = SleepController(conn)
        with_driver(c1, RecordingDriver())
        m = miner(dry_run=False, cooldown_seconds=0, grace_seconds=180)
        await c1.consider(m, State.MINING, working=False, now=t(0))
        await c1.consider(m, State.STOPPED, working=True, now=t(10))   # wake

        c2 = SleepController(conn, {m.id: m})
        # 10s after the wake, still at zero hashrate: nobody should restart it.
        assert await c2.consider(m, State.STOPPED, working=True, now=t(20)) is True

    async def test_cooldown_survives_a_restart(self, conn):
        """Otherwise a crash-looping service actuates far faster than configured."""
        c1 = SleepController(conn)
        with_driver(c1, RecordingDriver(ok=False))
        m = miner(dry_run=False, cooldown_seconds=600, grace_seconds=0)
        await c1.consider(m, State.MINING, working=False, now=t(0))

        c2 = SleepController(conn, {m.id: m})
        driver = with_driver(c2, RecordingDriver(ok=False))
        await c2.consider(m, State.MINING, working=False, now=t(60))
        assert driver.calls == []
        assert "skipped_sleep_cooldown" in actions(conn, m.id)

    async def test_failure_count_survives_a_restart(self, conn):
        """Otherwise max_failures is never reached across restarts."""
        c1 = SleepController(conn)
        with_driver(c1, RecordingDriver(ok=False))
        m = miner(dry_run=False, cooldown_seconds=0, grace_seconds=0, max_failures=3)
        await c1.consider(m, State.MINING, working=False, now=t(0))
        await c1.consider(m, State.MINING, working=False, now=t(10))

        c2 = SleepController(conn, {m.id: m})
        with_driver(c2, RecordingDriver(ok=False))
        await c2.consider(m, State.MINING, working=False, now=t(20))
        assert c2.needs_attention(m.id)

    async def test_a_success_resets_the_hydrated_failure_count(self, conn):
        c1 = SleepController(conn)
        driver = with_driver(c1, RecordingDriver(ok=False))
        m = miner(dry_run=False, cooldown_seconds=0, grace_seconds=0, max_failures=2)
        await c1.consider(m, State.MINING, working=False, now=t(0))
        driver.ok = True
        await c1.consider(m, State.MINING, working=False, now=t(10))

        assert SleepController(conn, {m.id: m})._failures.get(m.id, 0) == 0

    async def test_unparseable_timestamp_does_not_drop_the_latch(self, conn):
        """A bad row must not make MinerWatch forget it slept a miner."""
        m = miner()
        conn.execute(
            "INSERT INTO events (ts, miner, state, action, reason) VALUES (?, ?, ?, ?, ?)",
            ("not-a-timestamp", m.id, "mining", "sleep", "legacy row"),
        )
        conn.commit()
        assert SleepController(conn, {m.id: m}).is_asleep(m.id)

    async def test_same_timestamp_events_resolve_to_the_later_insert(self, conn):
        """Windows clock granularity is ~15.6ms, so ties are common there."""
        m = miner()
        stamp = t(0).isoformat()
        for action in ("sleep", "wake"):
            conn.execute(
                "INSERT INTO events (ts, miner, state, action, reason) VALUES (?, ?, ?, ?, ?)",
                (stamp, m.id, "mining", action, None),
            )
        conn.commit()
        # "wake" was inserted last, so the miner must not be considered asleep.
        assert not SleepController(conn, {m.id: m}).is_asleep(m.id)


class TestCooldownLogging:
    async def test_one_event_per_cooldown_not_one_per_poll(self, conn):
        c = SleepController(conn)
        with_driver(c, RecordingDriver(ok=False))
        m = miner(dry_run=False, cooldown_seconds=300, grace_seconds=0)

        await c.consider(m, State.MINING, working=False, now=t(0))
        for i in range(1, 20):
            await c.consider(m, State.MINING, working=False, now=t(i * 15))

        recorded = actions(conn, m.id)
        assert recorded.count("skipped_sleep_cooldown") == 1, recorded

    async def test_a_new_cooldown_is_logged_again(self, conn):
        c = SleepController(conn)
        with_driver(c, RecordingDriver(ok=False))
        m = miner(dry_run=False, cooldown_seconds=300, grace_seconds=0)

        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.MINING, working=False, now=t(15))    # logged
        await c.consider(m, State.MINING, working=False, now=t(400))   # retry
        await c.consider(m, State.MINING, working=False, now=t(415))   # logged again
        assert actions(conn, m.id).count("skipped_sleep_cooldown") == 2


# ---------------------------------------------------------------------------
# Second review round
# ---------------------------------------------------------------------------

class TestClearAttentionDurability:
    async def test_clear_restores_the_full_retry_budget_across_a_restart(self, conn):
        """Otherwise clear-attention is undone by the next service restart.

        Replaying the failure history would re-latch the miner after a single
        attempt, so the transient the operator was clearing never gets the
        retries it needs to resolve.
        """
        m = miner(dry_run=False, cooldown_seconds=0, grace_seconds=0, max_failures=3)
        c1 = SleepController(conn)
        with_driver(c1, RecordingDriver(ok=False))
        for i in range(3):
            await c1.consider(m, State.MINING, working=False, now=t(i * 10))
        assert c1.needs_attention(m.id)
        c1.clear_attention(m.id)

        c2 = SleepController(conn, {m.id: m})
        driver = with_driver(c2, RecordingDriver(ok=False))
        assert not c2.needs_attention(m.id)

        await c2.consider(m, State.MINING, working=False, now=t(100))
        assert not c2.needs_attention(m.id), "re-latched after a single attempt"
        await c2.consider(m, State.MINING, working=False, now=t(110))
        assert not c2.needs_attention(m.id)
        await c2.consider(m, State.MINING, working=False, now=t(120))
        assert c2.needs_attention(m.id)
        assert len(driver.calls) == 3


class TestRehearsedWake:
    async def test_a_reopening_window_rehearses_the_wake(self, conn):
        """Dry run must exercise both halves of the cycle, not just the sleep.

        The live wake path needs state == STOPPED, which can never happen in a
        rehearsal because nothing was ever stopped - so without an explicit
        rehearsed wake an operator validating a schedule would never see
        whether their window wakes the fleet.
        """
        c = SleepController(conn, dry_run=True)
        driver = with_driver(c, RecordingDriver())
        m = miner(dry_run=True, cooldown_seconds=0, grace_seconds=0)

        await c.consider(m, State.MINING, working=False, now=t(0))     # window closed
        assert c.is_asleep(m.id)

        assert await c.consider(m, State.MINING, working=True, now=t(3600)) is True
        assert not c.is_asleep(m.id)
        assert actions(conn, m.id) == ["would_sleep", "would_wake"]
        assert driver.calls == []                                       # still nothing sent

    async def test_a_rehearsed_miner_is_released_to_the_watchdog(self, conn):
        """The pretend latch must not be immortal."""
        c = SleepController(conn, dry_run=True)
        with_driver(c, RecordingDriver())
        m = miner(dry_run=True, cooldown_seconds=0, grace_seconds=0)

        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.MINING, working=True, now=t(3600))   # rehearsed wake
        # Hashing normally inside its window: nothing to own.
        assert await c.consider(m, State.MINING, working=True, now=t(3700)) is False
        # And a genuine failure now reaches the watchdog promptly.
        assert await c.consider(m, State.STOPPED, working=True, now=t(3800)) is False


class TestIneffectiveSleep:
    async def test_a_sleep_that_is_acked_but_never_takes_effect_is_surfaced(self, conn):
        """Firmware that ACKs an unsupported sleep must not loop silently.

        The backends judge success from the command's own acknowledgement, so
        every cycle "succeeds" and the ordinary failure counter keeps resetting.
        """
        c = SleepController(conn)
        driver = with_driver(c, RecordingDriver(ok=True))
        m = miner(dry_run=False, cooldown_seconds=0, grace_seconds=60, max_failures=3)

        # Each round: sleep is accepted, the miner keeps hashing regardless.
        for round_no in range(3):
            base = round_no * 1000
            await c.consider(m, State.MINING, working=False, now=t(base))
            await c.consider(m, State.MINING, working=False, now=t(base + 200))

        assert c.needs_attention(m.id)
        recorded = actions(conn, m.id)
        assert recorded.count("awake") == 3
        assert "sleep_needs_attention" in recorded
        assert len(driver.calls) == 3, "must stop hammering once latched"

    async def test_a_sleep_that_works_resets_the_counter(self, conn):
        """Observing the miner actually stopped is proof the sleep took."""
        c = SleepController(conn)
        with_driver(c, RecordingDriver(ok=True))
        m = miner(dry_run=False, cooldown_seconds=0, grace_seconds=60, max_failures=3)

        # Two ineffective rounds: still hashing well after each sleep.
        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.MINING, working=False, now=t(200))
        await c.consider(m, State.MINING, working=False, now=t(400))
        assert c._ineffective[m.id] == 2

        # Then a poll that finds it genuinely stopped.
        await c.consider(m, State.STOPPED, working=False, now=t(500))
        assert m.id not in c._ineffective

        # Two more ineffective rounds must not trip a max_failures of 3, which
        # they would if the counter had carried the earlier two forward.
        await c.consider(m, State.MINING, working=False, now=t(600))
        await c.consider(m, State.MINING, working=False, now=t(800))
        assert c._ineffective[m.id] == 2
        assert not c.needs_attention(m.id)

    async def test_the_reason_explains_what_the_operator_should_check(self, conn):
        c = SleepController(conn)
        with_driver(c, RecordingDriver(ok=True))
        m = miner(dry_run=False, cooldown_seconds=0, grace_seconds=60, max_failures=1)

        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.MINING, working=False, now=t(200))

        reason = conn.execute(
            "SELECT reason FROM events WHERE miner = ? AND action = 'sleep_needs_attention'",
            (m.id,),
        ).fetchone()[0]
        assert "never took effect" in reason and "backend" in reason


class TestCooldownHydrationPrecision:
    async def test_an_awake_observation_does_not_push_the_cooldown_forward(self, conn):
        """`awake` is an observation, not an attempt; counting it delays retries."""
        m = miner(dry_run=False, cooldown_seconds=300, grace_seconds=0, max_failures=9)
        c1 = SleepController(conn)
        with_driver(c1, RecordingDriver(ok=True))
        await c1.consider(m, State.MINING, working=False, now=t(0))     # sleep @ t0
        await c1.consider(m, State.MINING, working=False, now=t(280))   # awake @ t280

        c2 = SleepController(conn, {m.id: m})
        # The real attempt was at t0, so the cooldown expires at t300.
        assert c2._last_attempt[m.id] == t(0)
