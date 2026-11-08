"""Regression tests for the 17 Sep sleep guard (the 15 Sep 2026 sxp-04 outage).

What went wrong: the sleep POST answered HTTP 500 although the write landed,
so the miner slept while MinerWatch recorded ``sleep_failed`` and believed it
awake. No wake was sent when the window opened, and the watchdog spent three
reboots on a miner that a reboot cannot wake. The guard has three parts, and
each is pinned down here:

* backends: a failed POST is verified before it is believed, with one more
  re-read after a settle;
* sleeper: a failed sleep leaves the miner *uncertain* (also across a
  restart), and an uncertain miner is woken when its window opens;
* watchdog: a stopped miner that its web UI reports asleep is not restarted
  (``skipped_asleep``), and that costs it none of its restart budget.
"""

import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from minerwatch import backends, watchdog as watchdog_mod
from minerwatch.backends import BitmainHttpBackend
from minerwatch.models import (
    Miner, Range, Schedule, SleepBackend, SleepConfig, State, WatchdogConfig, Window,
)
from minerwatch.sleeper import SleepController
from minerwatch.store import init_db
from minerwatch.watchdog import Watchdog
from sim.bitmain_http_sim import AsymmetricConf, BitmainHttpServer

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


def http_miner(port=80, mid="sxp-04", **sleep_kw):
    cfg = dict(enabled=True, backend=SleepBackend.BITMAIN_HTTP, http_port=port,
               username="root", password="root", timeout_seconds=5, dry_run=False,
               cooldown_seconds=0, grace_seconds=0)
    cfg.update(sleep_kw)
    return Miner(id=mid, host="127.0.0.1", port=4028, schedule=ALWAYS,
                 sleep=SleepConfig(**cfg),
                 watchdog=WatchdogConfig(fail_after_seconds=0, cooldown_seconds=0,
                                         max_restarts=3))


def cgminer_miner(mid="plain"):
    return Miner(id=mid, host="127.0.0.1", port=4028, schedule=ALWAYS,
                 sleep=SleepConfig(enabled=False, backend=SleepBackend.CGMINER),
                 watchdog=WatchdogConfig(fail_after_seconds=0, cooldown_seconds=0,
                                         max_restarts=3))


@pytest.fixture
def s19xp():
    """The S19 XP web UI: reads bitmain-work-mode, only honours miner-mode."""
    conf = AsymmetricConf()
    server = BitmainHttpServer(("127.0.0.1", 0), conf=conf)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield conf, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def post_500_after_write(monkeypatch):
    """set_miner_conf.cgi applies the change, then dies before answering."""
    real = BitmainHttpBackend._post_conf

    def post(self, miner, conf, fmt, content_type=None):
        real(self, miner, conf, fmt, content_type)
        return False, "HTTP 500 Internal Server Error"

    monkeypatch.setattr(BitmainHttpBackend, "_post_conf", post)


@pytest.fixture
def no_settle_wait(monkeypatch):
    monkeypatch.setattr(backends.time, "sleep", lambda s: None)


# ---------------------------------------------------------------------------
# backends.py
# ---------------------------------------------------------------------------

class TestVerifyBeforeBelievingAnError:
    async def test_failed_post_is_verified_before_believing_it(
        self, s19xp, post_500_after_write, no_settle_wait
    ):
        conf, port = s19xp
        ok, detail = await BitmainHttpBackend().sleep(http_miner(port))
        assert ok, detail
        assert "verified" in detail and "HTTP 500" in detail
        assert conf.miner_mode == 1

    async def test_a_post_that_failed_and_did_not_land_is_still_a_failure(
        self, s19xp, monkeypatch, no_settle_wait
    ):
        conf, port = s19xp
        monkeypatch.setattr(BitmainHttpBackend, "_post_conf",
                            lambda self, *a, **k: (False, "HTTP 500 Internal Server Error"))
        ok, detail = await BitmainHttpBackend().sleep(http_miner(port))
        assert not ok
        assert "HTTP 500" in detail
        assert conf.miner_mode == 0

    async def test_settle_reverify_catches_a_slow_write(self, s19xp, monkeypatch):
        """The field only changes after the settle: still reported as a sleep."""
        conf, port = s19xp
        pending = []
        real_set = conf.set
        monkeypatch.setattr(conf, "set", lambda doc: pending.append(doc))
        slept = []

        def settle(seconds):
            slept.append(seconds)
            for doc in pending:
                real_set(doc)

        monkeypatch.setattr(backends.time, "sleep", settle)
        ok, detail = await BitmainHttpBackend().sleep(http_miner(port))
        assert ok, detail
        assert "settle" in detail
        assert slept == [backends.DEFAULT_SETTLE_SECONDS]
        assert conf.miner_mode == 1

    async def test_is_asleep_reads_without_writing(self, s19xp):
        conf, port = s19xp
        assert (await BitmainHttpBackend().is_asleep(http_miner(port)))[0] is False
        conf.set({"miner-mode": 1})
        writes = len(conf.writes)
        assert (await BitmainHttpBackend().is_asleep(http_miner(port)))[0] is True
        assert len(conf.writes) == writes

    async def test_is_asleep_is_none_when_the_web_ui_is_dead(self):
        asleep, _ = await BitmainHttpBackend().is_asleep(http_miner(port=1, timeout_seconds=1))
        assert asleep is None


# ---------------------------------------------------------------------------
# sleeper.py
# ---------------------------------------------------------------------------

class FailingDriver:
    name = "failing"

    def __init__(self):
        self.calls = []

    async def sleep(self, m):
        self.calls.append("sleep")
        return False, "sleep: HTTP 500"

    async def wake(self, m):
        self.calls.append("wake")
        return True, "wake: verified"


class TestUncertainSleep:
    async def test_sleep_failed_marks_uncertain(self, conn):
        m = http_miner()
        c = SleepController(conn, {m.id: m})
        c._driver_for = lambda _m: FailingDriver()
        await c.consider(m, State.MINING, working=False, now=t(0))
        assert "sleep_failed" in actions(conn, m.id)
        assert c.is_uncertain(m.id)
        assert not c.is_asleep(m.id)

    async def test_uncertainty_survives_hydration(self, conn):
        m = http_miner()
        c1 = SleepController(conn, {m.id: m})
        c1._driver_for = lambda _m: FailingDriver()
        await c1.consider(m, State.MINING, working=False, now=t(0))
        assert SleepController(conn, {m.id: m}).is_uncertain(m.id)

    async def test_uncertain_miner_is_woken_when_its_window_opens(self, conn):
        m = http_miner()
        c = SleepController(conn, {m.id: m})
        driver = FailingDriver()
        c._driver_for = lambda _m: driver
        await c.consider(m, State.MINING, working=False, now=t(0))
        # Stopped at 0 TH/s, exactly like a successful sleep. Window opens:
        owned = await c.consider(m, State.STOPPED, working=True, now=t(3 * 3600))
        assert driver.calls == ["sleep", "wake"]
        assert owned is True
        assert not c.is_uncertain(m.id)

    async def test_mining_settles_the_doubt(self, conn):
        m = http_miner()
        c = SleepController(conn, {m.id: m})
        c._driver_for = lambda _m: FailingDriver()
        await c.consider(m, State.MINING, working=False, now=t(0))
        await c.consider(m, State.MINING, working=True, now=t(60))
        assert not c.is_uncertain(m.id)


# ---------------------------------------------------------------------------
# watchdog.py
# ---------------------------------------------------------------------------

class Probe:
    """Stands in for BitmainHttpBackend.is_asleep; counts the HTTP reads."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def install(self, monkeypatch):
        async def is_asleep(backend_self, miner):
            return await self(miner)
        monkeypatch.setattr(watchdog_mod.BitmainHttpBackend, "is_asleep", is_asleep)
        return self

    async def __call__(self, miner):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer, f"work mode {self.answer}"


@pytest.fixture
def probe(monkeypatch):
    return Probe(True).install(monkeypatch)


def live_watchdog(conn, miners, **kw):
    wd = Watchdog(conn, dry_run=False, miners=miners, **kw)
    wd.recover = AsyncMock(return_value=(True, "reboot sent"))
    return wd


class TestWatchdogSleepGuard:
    async def test_asleep_miner_is_not_restarted(self, conn, probe):
        m = http_miner()
        wd = live_watchdog(conn, {m.id: m})
        await wd.consider(m, State.STOPPED, True, t(0), "")
        assert actions(conn, m.id)[-1] == "skipped_asleep"
        wd.recover.assert_not_awaited()

    async def test_skipped_asleep_does_not_consume_restart_budget(self, conn, probe):
        m = http_miner()
        wd = live_watchdog(conn, {m.id: m}, asleep_recheck=0)
        for i in range(10):
            await wd.consider(m, State.STOPPED, True, t(i * 15), "")
        assert probe.calls == 10
        assert "needs_attention" not in actions(conn, m.id)
        probe.answer = False
        await wd.consider(m, State.STOPPED, True, t(200), "")
        wd.recover.assert_awaited_once()
        assert actions(conn, m.id)[-1] == "restart"
        assert m.id not in wd._needs_attention

    async def test_awake_miner_still_restarts(self, conn, probe):
        probe.answer = False
        m = http_miner()
        wd = live_watchdog(conn, {m.id: m})
        await wd.consider(m, State.STOPPED, True, t(0), "")
        wd.recover.assert_awaited_once()

    async def test_probe_failure_falls_through(self, conn, probe):
        probe.answer = None
        m = http_miner()
        wd = live_watchdog(conn, {m.id: m})
        await wd.consider(m, State.STOPPED, True, t(0), "")
        wd.recover.assert_awaited_once()

    async def test_probe_exception_falls_through(self, conn, probe):
        probe.answer = TimeoutError("no reply")
        m = http_miner()
        wd = live_watchdog(conn, {m.id: m})
        await wd.consider(m, State.STOPPED, True, t(0), "")
        wd.recover.assert_awaited_once()

    async def test_no_credentials_skips_probe(self, conn, probe):
        m = cgminer_miner()
        wd = live_watchdog(conn, {m.id: m})
        await wd.consider(m, State.STOPPED, True, t(0), "")
        assert probe.calls == 0
        wd.recover.assert_awaited_once()

    async def test_unreachable_skips_probe(self, conn, probe):
        m = http_miner()
        wd = live_watchdog(conn, {m.id: m})
        await wd.consider(m, State.UNREACHABLE, True, t(0), "")
        assert probe.calls == 0
        wd.recover.assert_awaited_once()

    async def test_probe_is_rate_limited(self, conn, probe):
        m = http_miner()
        wd = live_watchdog(conn, {m.id: m}, asleep_recheck=300)
        for i in range(20):
            await wd.consider(m, State.STOPPED, True, t(i * 10), "")
        assert probe.calls == 1
        assert actions(conn, m.id).count("skipped_asleep") == 20

    async def test_dry_run_records_skipped_asleep_not_would_restart(self, conn, probe):
        m = http_miner()
        wd = Watchdog(conn, dry_run=True, miners={m.id: m})
        await wd.consider(m, State.STOPPED, True, t(0), "")
        assert "skipped_asleep" in actions(conn, m.id)
        assert "would_restart" not in actions(conn, m.id)

    async def test_clear_attention_drops_the_cached_reading(self, conn, probe):
        m = http_miner()
        wd = live_watchdog(conn, {m.id: m}, asleep_recheck=300)
        await wd.consider(m, State.STOPPED, True, t(0), "")
        wd.clear_attention(m.id)
        probe.answer = False
        await wd.consider(m, State.STOPPED, True, t(10), "")
        assert probe.calls == 2
        wd.recover.assert_awaited_once()


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

class TestSxp04Regression:
    async def test_sxp04_20260915_regression(self, conn, s19xp, post_500_after_write,
                                             no_settle_wait):
        """15 Sep 2026 replayed against the S19 XP simulator.

        18:00 the sleep POST answers HTTP 500 but lands. The miner stops.
        21:00 the window opens. Before the fix: no wake, three reboots, a
        latch, and 14 hours lost. Now the sleep is recognised, the wake is
        sent, and no reboot is sent at any point.
        """
        conf, port = s19xp
        m = http_miner(port)
        miners = {m.id: m}
        sleeper = SleepController(conn, miners)
        wd = live_watchdog(conn, miners)

        async def poll(state, working, now):
            if not await sleeper.consider(m, state, working, now):
                await wd.consider(m, state, working, now, "")

        await poll(State.MINING, False, t(0))                 # 18:00, window closes
        assert "sleep" in actions(conn, m.id)
        assert "sleep_failed" not in actions(conn, m.id)
        assert conf.miner_mode == 1

        for minute in range(1, 180, 15):                      # asleep all evening
            await poll(State.STOPPED, False, t(minute * 60))

        await poll(State.STOPPED, True, t(3 * 3600))          # 21:00, window opens
        assert actions(conn, m.id).count("wake") == 1
        assert conf.miner_mode == 0
        wd.recover.assert_not_awaited()
        assert "needs_attention" not in actions(conn, m.id)

    async def test_sxp04_even_when_the_verify_also_fails(self, conn, monkeypatch):
        """Belt and braces: the read-back also misses, so the sleep is recorded as
        failed. The miner is uncertain, gets woken at 21:00, and the watchdog's
        own probe stops a reboot."""
        m = http_miner()
        miners = {m.id: m}
        sleeper = SleepController(conn, miners)
        driver = FailingDriver()
        sleeper._driver_for = lambda _m: driver
        wd = live_watchdog(conn, miners)
        p = Probe(True).install(monkeypatch)

        async def poll(state, working, now):
            if not await sleeper.consider(m, state, working, now):
                await wd.consider(m, state, working, now, "")

        await poll(State.MINING, False, t(0))
        assert actions(conn, m.id)[-1] == "sleep_failed"
        await poll(State.STOPPED, False, t(60))
        await poll(State.STOPPED, True, t(3 * 3600))
        assert driver.calls == ["sleep", "wake"]
        wd.recover.assert_not_awaited()
