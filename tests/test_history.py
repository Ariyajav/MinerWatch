"""`history` is the answer to "did MinerWatch do anything, and why not?".

Every decision either controller makes is already recorded. Without a reader,
an operator watching a silent log cannot tell "nothing went wrong" from
"nothing was attempted" - which is exactly the ambiguity that made a fleet look
broken when it was only rehearsing.
"""

from datetime import datetime, timedelta, timezone

import pytest

from minerwatch.cli import cmd_history
from minerwatch.models import Miner, Range, Schedule, Window
from minerwatch.store import init_db


class Args:
    def __init__(self, miner="m1", hours=24.0, decisions=False, limit=5000):
        self.miner = miner
        self.hours = hours
        self.decisions = decisions
        self.limit = limit


@pytest.fixture
def fixture():
    conn = init_db(":memory:")
    window = Window(days=frozenset(range(7)), ranges=[Range(start=0, end=1440)])
    miner = Miner(id="m1", host="10.0.0.1", port=4028,
                  schedule=Schedule(timezone=timezone.utc, windows=[window]))
    config = (15, ":memory:", "UTC", {"m1": miner})
    yield config, conn
    conn.close()


def add(conn, minutes_ago, state, action, reason=None, ghs=None):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    conn.execute(
        "INSERT INTO events (ts, miner, state, action, reason, ghs) VALUES (?, 'm1', ?, ?, ?, ?)",
        (ts, state, action, reason, ghs),
    )
    conn.commit()


def test_it_shows_readings_and_decisions_together(fixture, capsys):
    config, conn = fixture
    add(conn, 30, "mining", "none", ghs=144000)
    add(conn, 20, "stopped", "alert", ghs=0)
    add(conn, 10, "stopped", "waiting_to_restart", "failing for 600s; restart in 1200s")

    assert cmd_history(Args(), config, conn) == 0
    out = capsys.readouterr().out

    assert "144.0 TH" in out
    assert "waiting_to_restart" in out
    assert "failing for 600s" in out


def test_decisions_hides_the_routine_per_poll_rows(fixture, capsys):
    config, conn = fixture
    for i in range(40, 10, -1):
        add(conn, i, "mining", "none", ghs=144000)
    add(conn, 5, "stopped", "sleep", "sleep: '0' -> 1 via miner-mode, verified")

    cmd_history(Args(decisions=True), config, conn)
    out = capsys.readouterr().out

    assert "sleep" in out
    assert "144.0 TH" not in out, "routine polls should be hidden"


def test_it_says_plainly_when_nothing_was_attempted(fixture, capsys):
    """The reported symptom: 'restarts are not working'.

    A miner polled all day with no decision row means neither controller ever
    acted on it, which is a different problem from one that acted and failed.
    """
    config, conn = fixture
    for i in range(60, 0, -1):
        add(conn, i, "mining", "none", ghs=144000)

    cmd_history(Args(decisions=True), config, conn)
    out = capsys.readouterr().out

    assert "no decisions at all" in out
    assert "polled 60 times" in out
    assert "poller is not running" not in out, "it WAS polled"


def test_an_empty_database_explains_itself(fixture, capsys):
    config, conn = fixture

    cmd_history(Args(), config, conn)
    out = capsys.readouterr().out

    assert "nothing recorded" in out
    assert "poller is not running" in out


def test_the_hours_window_is_respected(fixture, capsys):
    config, conn = fixture
    add(conn, 60 * 48, "stopped", "restart", "old news")
    add(conn, 30, "stopped", "waiting_to_restart", "recent")

    cmd_history(Args(hours=24), config, conn)
    out = capsys.readouterr().out

    assert "recent" in out
    assert "old news" not in out


def test_every_action_the_code_writes_has_a_plain_english_meaning():
    """A legend that silently omits an action is worse than none.

    The whole point is that an operator should not have to read the source to
    interpret the table.
    """
    from minerwatch.cli import ACTION_MEANING
    from minerwatch.sleeper import (
        CLEARED_ACTION, DRY_RUN_ACTIONS, SLEEP_ACTIONS, WAKE_ACTIONS,
    )
    from minerwatch.watchdog import CLOCK_RESET_ACTIONS, IN_WINDOW_FAILURE_ACTIONS

    known = set(ACTION_MEANING)
    for action in (
        *SLEEP_ACTIONS, *WAKE_ACTIONS, *DRY_RUN_ACTIONS, CLEARED_ACTION,
        *IN_WINDOW_FAILURE_ACTIONS, *CLOCK_RESET_ACTIONS,
        "none", "alert", "expected_off", "attention_cleared",
        "sleep_failed", "sleep_needs_attention",
    ):
        assert action in known, f"{action!r} has no entry in ACTION_MEANING"


def test_long_runs_of_identical_decisions_are_collapsed(fixture, capsys):
    """A latched miner writes one row every poll; printing them all buries the
    finding. The first version of this command made an operator scroll past
    four thousand copies of the same line to learn the fleet was latched."""
    config, conn = fixture
    for i in range(300, 0, -1):
        add(conn, i, "stopped", "skipped_needs_attention", "miner is latched as needs_attention")
    add(conn, 0, "stopped", "needs_attention", "3 restart attempts within 3600s")

    cmd_history(Args(decisions=True), config, conn)
    out = capsys.readouterr().out

    assert out.count("skipped_needs_attention") <= 3, "run was not collapsed"
    assert "[x300" in out
    assert "needs_attention" in out


def test_a_changing_state_breaks_a_run(fixture, capsys):
    config, conn = fixture
    add(conn, 30, "unreachable", "skipped_needs_attention", "latched")
    add(conn, 20, "unreachable", "skipped_needs_attention", "latched")
    add(conn, 10, "stopped", "skipped_needs_attention", "latched")

    cmd_history(Args(decisions=True), config, conn)
    out = capsys.readouterr().out

    assert "unreachable" in out and "stopped" in out
    assert "[x2" in out


class TestStatusConsistency:
    """Every column of a `status` line must describe the same observation."""

    def test_an_unreachable_miner_shows_no_hashrate(self, fixture, capsys):
        """The reported symptom: `unreachable` next to `74.5 TH/s`.

        The state came from the newest row of any kind - including the
        watchdog's own `skipped_needs_attention`, which carries the state it
        assumed - while the hashrate came from the newest poll. An operator
        reading that line cannot tell which half is current.
        """
        from minerwatch.cli import cmd_status

        config, conn = fixture
        add(conn, 30, "mining", "none", ghs=74500)
        add(conn, 10, "unreachable", "alert", "timeout")
        add(conn, 5, "unreachable", "skipped_needs_attention", "latched")

        class A:
            config = "miners.yaml"
        cmd_status(A(), config, conn)
        out = capsys.readouterr().out

        assert "unreachable" in out
        assert "74.5 TH" not in out, "stale hashrate shown beside a live failure"

    def test_a_mining_miner_still_shows_its_hashrate(self, fixture, capsys):
        from minerwatch.cli import cmd_status

        config, conn = fixture
        add(conn, 30, "unreachable", "alert", "timeout")
        add(conn, 5, "mining", "none", ghs=144200)

        class A:
            config = "miners.yaml"
        cmd_status(A(), config, conn)
        out = capsys.readouterr().out

        assert "mining" in out
        assert "144.2 TH" in out
