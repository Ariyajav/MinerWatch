"""A miner MinerWatch slept is woken at window open even if its API port is down.

sxp-04, 3 and 4 Nov 2026: after a verified sleep the miner stopped answering
on its cgminer port for the whole three hours. The sleeper used to hand every
UNREACHABLE miner to the watchdog, so the wake was never sent. On 4 Nov the
watchdog spent three restarts on it and latched it, and it was woken only
about four hours later. The wake goes over the web UI, a different port, so
the sleeper now tries it.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from minerwatch.models import (
    Miner, Range, Schedule, SleepBackend, SleepConfig, State, WatchdogConfig, Window,
)
from minerwatch.sleeper import SleepController
from minerwatch.store import init_db
from minerwatch.watchdog import Watchdog

ALWAYS = Schedule(
    timezone=timezone.utc,
    windows=[Window(days=frozenset(range(7)), ranges=[Range(start=0, end=1440)])],
)


def t(seconds: float) -> datetime:
    return datetime.fromtimestamp(1_700_000_000 + seconds, tz=timezone.utc)


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def actions(conn, miner_id):
    return [r[0] for r in conn.execute(
        "SELECT action FROM events WHERE miner = ? ORDER BY id", (miner_id,)
    ).fetchall()]


def make_miner(backend=SleepBackend.BITMAIN_HTTP, **kw):
    cfg = dict(enabled=True, backend=backend, dry_run=False, cooldown_seconds=300,
               grace_seconds=180, max_failures=3)
    cfg.update(kw)
    return Miner(id="sxp-04", host="127.0.0.1", port=4028, schedule=ALWAYS,
                 sleep=SleepConfig(**cfg),
                 watchdog=WatchdogConfig(fail_after_seconds=0, cooldown_seconds=0,
                                         max_restarts=3))


class Driver:
    name = "recording"

    def __init__(self, sleep_ok=True, wake_ok=True):
        self.sleep_ok, self.wake_ok = sleep_ok, wake_ok
        self.calls = []

    async def sleep(self, m):
        self.calls.append("sleep")
        return self.sleep_ok, "sleep"

    async def wake(self, m):
        self.calls.append("wake")
        return self.wake_ok, "wake"


def controller(conn, m, driver):
    c = SleepController(conn, {m.id: m})
    c._driver_for = lambda _m: driver
    return c


class TestWakeWhileUnreachable:
    async def test_asleep_and_unreachable_at_window_open_is_woken(self, conn):
        m = make_miner()
        d = Driver()
        c = controller(conn, m, d)
        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.UNREACHABLE, working=False, now=t(60))
        assert d.calls == ["sleep"], "no wake outside the window"
        owned = await c.consider(m, State.UNREACHABLE, working=True, now=t(3 * 3600))
        assert owned is True
        assert d.calls == ["sleep", "wake"]
        assert not c.is_asleep(m.id)

    async def test_uncertain_and_unreachable_is_woken_too(self, conn):
        m = make_miner()
        d = Driver(sleep_ok=False)
        c = controller(conn, m, d)
        await c.consider(m, State.MINING, working=False, now=t(0))
        assert c.is_uncertain(m.id)
        await c.consider(m, State.UNREACHABLE, working=True, now=t(3 * 3600))
        assert d.calls == ["sleep", "wake"]

    async def test_unreachable_miner_we_did_not_sleep_is_left_to_the_watchdog(self, conn):
        m = make_miner()
        d = Driver()
        c = controller(conn, m, d)
        assert await c.consider(m, State.UNREACHABLE, working=True, now=t(0)) is False
        assert d.calls == []

    async def test_cgminer_backend_is_unchanged(self, conn):
        """Its wake goes over the same port that is not answering."""
        m = make_miner(backend=SleepBackend.CGMINER)
        d = Driver()
        c = controller(conn, m, d)
        await c.consider(m, State.MINING, working=False, now=t(0))
        assert await c.consider(m, State.UNREACHABLE, working=True, now=t(3 * 3600)) is False
        assert d.calls == ["sleep"]

    async def test_failed_wakes_are_paced_and_latch(self, conn):
        """A miner that is really off: cooldown between wakes, then the usual latch."""
        m = make_miner()
        d = Driver(wake_ok=False)
        c = controller(conn, m, d)
        await c.consider(m, State.MINING, working=False, now=t(0))
        start = 3 * 3600
        for i in range(0, 1200, 15):          # 20 minutes of 15 s polls
            await c.consider(m, State.UNREACHABLE, working=True, now=t(start + i))
        assert d.calls.count("wake") == 3     # at 0, 300 and 600 s; latched after the third
        assert c.needs_attention(m.id)
        assert "sleep_needs_attention" in actions(conn, m.id)

    async def test_after_a_successful_wake_the_watchdog_gets_it_back(self, conn):
        """Still unreachable once the spin-up grace is over: a real fault."""
        m = make_miner()
        d = Driver()
        c = controller(conn, m, d)
        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.UNREACHABLE, working=True, now=t(3 * 3600))
        assert await c.consider(m, State.UNREACHABLE, working=True, now=t(3 * 3600 + 60)) is True
        assert await c.consider(m, State.UNREACHABLE, working=True, now=t(3 * 3600 + 400)) is False


class TestSxp04November4:
    async def test_no_restarts_are_spent_on_a_sleeping_unreachable_miner(self, conn):
        m = make_miner()
        d = Driver()
        c = controller(conn, m, d)
        wd = Watchdog(conn, dry_run=False, miners={m.id: m})
        wd.recover = AsyncMock(return_value=(False, "TimeoutError"))

        async def poll(state, working, now):
            if not await c.consider(m, state, working, now):
                await wd.consider(m, state, working, now, "")

        await poll(State.MINING, False, t(0))                    # 18:00 local, sleep
        for s in range(30, 3 * 3600, 15):                        # unreachable all evening
            await poll(State.UNREACHABLE, False, t(s))
        await poll(State.UNREACHABLE, True, t(3 * 3600))         # 21:00 local, window opens
        assert d.calls == ["sleep", "wake"]
        wd.recover.assert_not_awaited()
        assert "needs_attention" not in actions(conn, m.id)
