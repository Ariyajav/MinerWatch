"""clear-attention reaches a running supervisor without a restart.

The CLI runs in its own process and can only write the clear to the events
table. Before ``pick_up_clears`` the supervisor kept its in-memory latch until
the scheduled task was stopped and started, so an operator who ran
``clear-attention`` and nothing else saw the miner stay latched. These tests
use two connections to one database file to stand in for the two processes.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from minerwatch.cli import cmd_clear_attention
from minerwatch.models import Miner, Range, Schedule, SleepBackend, SleepConfig, State, Window
from minerwatch.sleeper import SleepController
from minerwatch.store import init_db
from minerwatch.watchdog import Watchdog

ALWAYS = Schedule(
    timezone=timezone.utc,
    windows=[Window(days=frozenset(range(7)), ranges=[Range(start=0, end=1440)])],
)


def t(seconds: float) -> datetime:
    return datetime.fromtimestamp(1_700_000_000 + seconds, tz=timezone.utc)


def later() -> str:
    """A timestamp after the CLI's clear, which is stamped with the real clock."""
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def make_miner(mid="m1", **sleep_kwargs):
    sleep = dict(enabled=True, backend=SleepBackend.CGMINER, dry_run=False,
                 cooldown_seconds=0, grace_seconds=0, max_failures=2)
    sleep.update(sleep_kwargs)
    return Miner(id=mid, host="127.0.0.1", port=4028, schedule=ALWAYS,
                 sleep=SleepConfig(**sleep))


@pytest.fixture
def db(tmp_path):
    """Two connections to one file: the supervisor's and the CLI's."""
    path = str(tmp_path / "minerwatch.db")
    supervisor, cli = init_db(path), init_db(path)
    yield supervisor, cli
    supervisor.close()
    cli.close()


def live_watchdog(conn, miners, max_restarts=2):
    wd = Watchdog(conn, fail_after=0, dry_run=False, cooldown=0,
                  max_restarts=max_restarts, miners=miners)
    wd.send_restart = AsyncMock(return_value=(True, "restart sent"))
    return wd


async def latch_watchdog(wd, miner, attempts):
    for i in range(attempts + 1):
        await wd.consider(miner, State.STOPPED, True, t(i * 10), "")
    assert miner.id in wd._needs_attention


class FailingDriver:
    name = "failing"

    def __init__(self):
        self.calls = 0

    async def sleep(self, m):
        self.calls += 1
        return False, "HTTP 500"

    async def wake(self, m):
        self.calls += 1
        return False, "HTTP 500"


async def latch_sleeper(controller, miner):
    controller._driver_for = lambda m: FailingDriver()
    for i in range(miner.sleep.max_failures):
        await controller.consider(miner, State.MINING, working=False, now=t(i * 10))
    assert controller.needs_attention(miner.id)


class TestWatchdogPicksUpClears:
    async def test_cli_clear_releases_the_running_watchdog(self, db):
        supervisor, cli = db
        m = make_miner()
        wd = live_watchdog(supervisor, {m.id: m})
        await latch_watchdog(wd, m, 2)

        Watchdog(cli, miners={m.id: m}).clear_attention(m.id)
        assert m.id in wd._needs_attention, "nothing is applied before the pick-up"

        assert wd.pick_up_clears() == [m.id]
        assert m.id not in wd._needs_attention
        # And it really restarts again rather than logging skipped_needs_attention.
        wd.send_restart.reset_mock()
        await wd.consider(m, State.STOPPED, True, t(1000), "")
        wd.send_restart.assert_awaited_once()

    async def test_each_clear_is_applied_once(self, db):
        supervisor, cli = db
        m = make_miner()
        wd = live_watchdog(supervisor, {m.id: m})
        await latch_watchdog(wd, m, 2)
        Watchdog(cli, miners={m.id: m}).clear_attention(m.id)
        assert wd.pick_up_clears() == [m.id]
        assert wd.pick_up_clears() == []

    async def test_clears_from_before_startup_are_not_replayed(self, db):
        """Hydration already applied them; replaying one would undo a later latch."""
        supervisor, cli = db
        m = make_miner()
        first = live_watchdog(supervisor, {m.id: m})
        await latch_watchdog(first, m, 2)
        Watchdog(cli, miners={m.id: m}).clear_attention(m.id)

        restarted = live_watchdog(supervisor, {m.id: m})
        assert restarted.pick_up_clears() == []

    async def test_a_newer_latch_beats_an_older_clear(self, db):
        supervisor, cli = db
        m = make_miner()
        wd = live_watchdog(supervisor, {m.id: m})
        await latch_watchdog(wd, m, 2)
        Watchdog(cli, miners={m.id: m}).clear_attention(m.id)
        # The latch is written again before the supervisor gets to the clear.
        supervisor.execute(
            "INSERT INTO events (ts, miner, state, action, reason) VALUES (?, ?, ?, ?, ?)",
            (later(), m.id, "stopped", "needs_attention", "again"),
        )
        supervisor.commit()
        assert wd.pick_up_clears() == []
        assert m.id in wd._needs_attention

    async def test_rehearsed_latch_is_released_too(self, db):
        supervisor, cli = db
        m = make_miner()
        wd = Watchdog(supervisor, fail_after=0, dry_run=True, cooldown=0,
                      max_restarts=2, miners={m.id: m})
        for i in range(3):
            await wd.consider(m, State.STOPPED, True, t(i * 10), "")
        assert m.id in wd._rehearsed_attention
        Watchdog(cli, miners={m.id: m}).clear_attention(m.id)
        wd.pick_up_clears()
        assert m.id not in wd._rehearsed_attention


class TestSleeperPicksUpClears:
    async def test_cli_clear_releases_the_running_sleeper(self, db):
        supervisor, cli = db
        m = make_miner()
        controller = SleepController(supervisor, {m.id: m})
        await latch_sleeper(controller, m)

        SleepController(cli, {m.id: m}).clear_attention(m.id)
        assert controller.needs_attention(m.id)
        assert controller.pick_up_clears() == [m.id]
        assert not controller.needs_attention(m.id)
        assert controller.pick_up_clears() == []

    async def test_a_newer_sleep_latch_beats_an_older_clear(self, db):
        supervisor, cli = db
        m = make_miner()
        controller = SleepController(supervisor, {m.id: m})
        await latch_sleeper(controller, m)
        SleepController(cli, {m.id: m}).clear_attention(m.id)
        supervisor.execute(
            "INSERT INTO events (ts, miner, state, action, reason) VALUES (?, ?, ?, ?, ?)",
            (later(), m.id, "mining", "sleep_needs_attention", "again"),
        )
        supervisor.commit()
        assert controller.pick_up_clears() == []
        assert controller.needs_attention(m.id)


class TestThroughTheCli:
    async def test_clear_attention_command_reaches_the_running_poller(self, db, tmp_path):
        """The path the operator takes: run the command, nothing else."""
        from threading import Event as ThreadEvent

        from minerwatch.poller import Poller

        supervisor, cli = db
        m = make_miner()
        miners = {m.id: m}
        poller = Poller((15, None, timezone.utc, miners), supervisor, ThreadEvent(),
                        dry_run=False, maintenance_path=str(tmp_path / "maintenance.json"))
        poller.watchdog.send_restart = AsyncMock(return_value=(True, "restart sent"))
        poller.watchdog.fail_after = 0
        poller.watchdog.max_restarts = 2
        poller.watchdog.cooldown = 0
        await latch_watchdog(poller.watchdog, m, 2)
        await latch_sleeper(poller.sleeper, m)

        rc = cmd_clear_attention(SimpleNamespace(miner=m.id),
                                 (15, None, timezone.utc, miners), cli)
        assert rc == 0

        poller._pick_up_clears()
        assert m.id not in poller.watchdog._needs_attention
        assert not poller.sleeper.needs_attention(m.id)
