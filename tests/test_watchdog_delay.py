"""The confirmation delay before the first restart.

Restarting a miner is not free: on stock firmware it costs several minutes of
hashing, and a miner that missed one poll is usually not broken at all. These
tests pin the three properties that make the delay trustworthy — it waits, it
survives a service restart, and scheduled downtime does not count towards it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from minerwatch.models import Miner, Schedule, State, WatchdogConfig, Window, Range
from minerwatch.store import init_db
from minerwatch.watchdog import MAX_CLOCK_ROWS, Watchdog

# asyncio_mode = "auto" in pyproject.toml handles the async tests; an explicit
# module-level mark would also be applied to the synchronous ones below.

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def t(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def make_miner(miner_id="m1", watchdog: WatchdogConfig | None = None) -> Miner:
    window = Window(days=frozenset(range(7)), ranges=[Range(start=0, end=1440)])
    return Miner(
        id=miner_id,
        host="127.0.0.1",
        port=9999,
        schedule=Schedule(timezone=timezone.utc, windows=[window]),
        watchdog=watchdog,
    )


def mine(conn, miner_id: str, start: int, end: int, step: int = 15) -> None:
    """Record poller observations of the miner hashing, from start to end."""
    moment = start
    while moment <= end:
        conn.execute(
            "INSERT INTO events (ts, miner, state, action, ghs) VALUES (?, ?, 'mining', 'none', 95000)",
            (t(moment).isoformat(), miner_id),
        )
        moment += step
    conn.commit()


def actions(conn, miner_id="m1") -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT action FROM events WHERE miner = ? ORDER BY ts, id", (miner_id,)
        )
    ]


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


class TestConfirmationDelay:
    async def test_a_single_failing_poll_does_not_restart(self, conn):
        """The case that motivated the feature: one dropped packet."""
        wd = Watchdog(conn, dry_run=True, fail_after=1800)
        miner = make_miner()

        await wd.consider(miner, State.UNREACHABLE, True, t(0), "timeout")

        assert actions(conn) == ["waiting_to_restart"]

    async def test_sustained_recovery_resets_the_clock(self, conn):
        """A miner that genuinely comes back is not carrying a grudge."""
        wd = Watchdog(conn, dry_run=True, fail_after=1800, recovery_seconds=300)
        miner = make_miner()

        # Fails for 25 minutes...
        for i in range(0, 1500, 300):
            await wd.consider(miner, State.STOPPED, True, t(i), "")
        # ...then mines steadily for ten minutes.
        mine(conn, miner.id, 1500, 2100, step=15)
        # ...and fails again.
        await wd.consider(miner, State.STOPPED, True, t(2160), "")

        assert wd.failing_for(miner.id, t(2160)) == 0.0
        assert actions(conn)[-1] == "waiting_to_restart"

    async def test_one_good_poll_is_not_a_recovery(self, conn):
        """The flapping-miner hole: 1% duty cycle used to be immortal.

        Resetting on any single mining reading meant a miner with a dying
        hashboard — hashing for one poll every few minutes — cleared its clock
        forever and was never restarted, which is worse than the immediate
        restarts this delay replaced.
        """
        wd = Watchdog(conn, dry_run=True, fail_after=1800, recovery_seconds=300)
        miner = make_miner()

        # 40 minutes of failing, interrupted by one good poll every 5 minutes.
        moment = 0
        while moment < 2400:
            for _ in range(19):
                await wd.consider(miner, State.STOPPED, True, t(moment), "")
                moment += 15
            mine(conn, miner.id, moment, moment)   # a single lucky reading
            moment += 15

        assert "would_restart" in actions(conn)

    async def test_a_flap_shorter_than_recovery_does_not_reset(self, conn):
        wd = Watchdog(conn, dry_run=True, fail_after=1800, recovery_seconds=300)
        miner = make_miner()

        await wd.consider(miner, State.STOPPED, True, t(0), "")
        mine(conn, miner.id, 60, 180, step=15)      # mines for 2 min only
        await wd.consider(miner, State.STOPPED, True, t(240), "")

        # The clock still runs from the original failure, not from 240.
        assert wd.failing_for(miner.id, t(240)) == pytest.approx(240, abs=1)

    async def test_restart_fires_once_the_delay_expires(self, conn):
        wd = Watchdog(conn, dry_run=True, fail_after=1800)
        miner = make_miner()

        await wd.consider(miner, State.STOPPED, True, t(0), "")
        await wd.consider(miner, State.STOPPED, True, t(900), "")
        assert actions(conn)[-1] == "waiting_to_restart"

        await wd.consider(miner, State.STOPPED, True, t(1800), "")
        assert actions(conn)[-1] == "would_restart"

    async def test_zero_disables_the_delay(self, conn):
        wd = Watchdog(conn, dry_run=True, fail_after=0)
        miner = make_miner()

        await wd.consider(miner, State.STOPPED, True, t(0), "")

        assert actions(conn) == ["would_restart"]


class TestDurability:
    async def test_the_clock_survives_a_service_restart(self, conn):
        """A crash-looping service must not reset its own watchdog.

        If the elapsed time were held in memory, a host that reboots every 20
        minutes would give every miner a fresh 30-minute clock and never
        restart anything — the watchdog would be silently disabled by the very
        instability it exists to handle.
        """
        first = Watchdog(conn, dry_run=True, fail_after=1800)
        miner = make_miner()
        for i in range(0, 1500, 300):
            await first.consider(miner, State.STOPPED, True, t(i), "")

        # Process dies; a new Watchdog is constructed against the same database.
        second = Watchdog(conn, dry_run=True, fail_after=1800)
        assert second.failing_for(miner.id, t(1500)) == pytest.approx(1500, abs=1)

        await second.consider(miner, State.STOPPED, True, t(1800), "")
        assert actions(conn)[-1] == "would_restart"


class TestScheduledDowntimeIsNotFailure:
    async def test_time_outside_the_window_does_not_count(self, conn):
        """The bug this guards against would restart the fleet every morning.

        A miner slept overnight records `skipped_outside_hours` for hours. If
        those counted, it would arrive at its window already past the delay and
        be restarted on the very first poll of the day — every single day.
        """
        wd = Watchdog(conn, dry_run=True, fail_after=1800)
        miner = make_miner()

        # Eight hours of being legitimately off.
        for i in range(0, 8 * 3600, 1800):
            await wd.consider(miner, State.STOPPED, False, t(i), "")
        assert set(actions(conn)) == {"skipped_outside_hours"}

        # The window opens and it is still not hashing. The clock starts now.
        await wd.consider(miner, State.STOPPED, True, t(8 * 3600), "")
        assert actions(conn)[-1] == "waiting_to_restart"
        assert wd.failing_for(miner.id, t(8 * 3600)) == 0.0

        # And it still has to wait the full delay.
        await wd.consider(miner, State.STOPPED, True, t(8 * 3600 + 1799), "")
        assert actions(conn)[-1] == "waiting_to_restart"
        await wd.consider(miner, State.STOPPED, True, t(8 * 3600 + 1800), "")
        assert actions(conn)[-1] == "would_restart"


class TestPerMinerPolicy:
    async def test_a_miners_own_config_wins_over_the_constructor(self, conn):
        wd = Watchdog(conn, dry_run=True, fail_after=1800)
        strict = make_miner("strict", WatchdogConfig(fail_after_seconds=60))

        await wd.consider(strict, State.STOPPED, True, t(0), "")
        await wd.consider(strict, State.STOPPED, True, t(60), "")

        assert actions(conn, "strict")[-1] == "would_restart"

    async def test_disabled_never_restarts(self, conn):
        """`enabled: false` is for miners that must never be touched."""
        wd = Watchdog(conn, dry_run=False, fail_after=0)
        miner = make_miner("hands-off", WatchdogConfig(enabled=False))

        for i in range(0, 3600, 600):
            await wd.consider(miner, State.STOPPED, True, t(i), "")

        assert set(actions(conn, "hands-off")) == {"skipped_watchdog_disabled"}

    async def test_a_disabled_miner_is_still_recorded(self, conn):
        """Monitoring continues; only actuation stops."""
        wd = Watchdog(conn, dry_run=False)
        miner = make_miner("hands-off", WatchdogConfig(enabled=False))

        await wd.consider(miner, State.UNREACHABLE, True, t(0), "timeout")

        row = conn.execute(
            "SELECT state, reason FROM events WHERE miner = 'hands-off'"
        ).fetchone()
        assert row[0] == "unreachable"
        assert "disabled" in row[1]


class TestClockSkew:
    async def test_a_backwards_clock_does_not_postpone_a_restart(self, conn):
        """An NTP step must not read as negative elapsed time.

        Without the clamp, `now - started` going negative would report the
        miner as failing for -3600s and hold off the restart for an extra hour.
        """
        wd = Watchdog(conn, dry_run=True, fail_after=1800)
        miner = make_miner()

        await wd.consider(miner, State.STOPPED, True, t(3600), "")
        assert wd.failing_for(miner.id, t(0)) == 0.0


class TestQueryCost:
    """The clock is read every poll of every failing miner, so its cost must
    not grow with the length of the outage — the watchdog would get slowest
    exactly when the fleet is in the worst shape."""

    def test_a_long_outage_reads_a_bounded_number_of_rows(self, conn):
        """A week of polls must not drag the whole history back each time."""
        rows = [
            (f"2026-08-{d:02d}T{h:02d}:{m:02d}:00+00:00", "m1", "unreachable", "waiting_to_restart")
            for d in range(1, 8)
            for h in range(24)
            for m in range(0, 60, 2)
        ]
        conn.executemany(
            "INSERT INTO events (ts, miner, state, action) VALUES (?,?,?,?)", rows
        )
        conn.commit()

        wd = Watchdog(conn, dry_run=True, fail_after=1800, recovery_seconds=300)
        now = datetime(2026, 8, 8, tzinfo=timezone.utc)

        rules = wd._clock_rules("m1")
        assert rules.max_rows <= MAX_CLOCK_ROWS
        assert rules.max_rows < len(rows) / 5, "reading most of the history"
        # Still unambiguously past the delay, which is the answer that matters.
        assert wd.failing_for("m1", now) > 1800


class TestSecondReviewFindings:
    """Defects found reviewing the fixes to the first review's findings."""

    async def test_a_polling_gap_is_not_evidence_of_mining(self, conn):
        """The recovery rule measured a span, not continuity.

        One lucky reading, a five-minute service outage, one more lucky
        reading — and the gap counted as five minutes of continuous mining, so
        the clock cleared. On this Windows deployment a five-minute gap is a
        service restart or a config reload, not an exotic event.
        """
        wd = Watchdog(conn, dry_run=True, fail_after=1800, recovery_seconds=300,
                      poll_interval=15)
        miner = make_miner()

        for i in range(0, 1500, 300):
            await wd.consider(miner, State.STOPPED, True, t(i), "")
        assert wd.failing_for(miner.id, t(1500)) == pytest.approx(1500, abs=1)

        mine(conn, miner.id, 1500, 1500)          # one lucky poll
        # ...five minutes of nothing at all...
        mine(conn, miner.id, 1800, 1800)          # one more lucky poll

        assert wd.failing_for(miner.id, t(1815)) > 1500

    async def test_an_alert_breaks_a_recovery_run(self, conn):
        """`alert` is the poller saying the miner is *not* hashing.

        It cannot start the clock (it is written outside the window too), but
        letting it fall through meant mining rows either side of a long run of
        alerts were treated as one continuous run.
        """
        wd = Watchdog(conn, dry_run=True, fail_after=1800, recovery_seconds=300,
                      poll_interval=15)
        miner = make_miner()

        await wd.consider(miner, State.STOPPED, True, t(0), "")
        mine(conn, miner.id, 60, 60)
        for i in range(75, 600, 15):
            conn.execute(
                "INSERT INTO events (ts, miner, state, action) VALUES (?, ?, 'unreachable', 'alert')",
                (t(i).isoformat(), miner.id),
            )
        conn.commit()
        mine(conn, miner.id, 600, 600)

        assert wd.failing_for(miner.id, t(615)) == pytest.approx(615, abs=5)

    async def test_a_slow_poll_interval_still_restarts_a_dead_miner(self, conn):
        """A time-bounded lookback silently disabled restarts entirely.

        With `poll_interval_seconds: 7200`, consecutive failure rows sit further
        apart than any fixed horizon, so every poll read as the *first* failure
        and a miner dead for days was never restarted — the exact invariant the
        clock exists to protect. Bounding by rows instead of time fixes it.
        """
        wd = Watchdog(conn, dry_run=True, fail_after=1800, recovery_seconds=300,
                      poll_interval=7200)
        miner = make_miner()

        moment = 0
        for _ in range(18):                      # three days at two-hour polls
            await wd.consider(miner, State.UNREACHABLE, True, t(moment), "timeout")
            moment += 7200

        assert "would_restart" in actions(conn)

    async def test_an_outage_longer_than_the_old_horizon_keeps_the_clock(self, conn):
        """MinerWatch itself being down must not hand the fleet a fresh clock."""
        wd = Watchdog(conn, dry_run=True, fail_after=1800, recovery_seconds=300)
        miner = make_miner()

        await wd.consider(miner, State.STOPPED, True, t(0), "")
        # MinerWatch is down for four hours; nothing is recorded.
        await wd.consider(miner, State.STOPPED, True, t(4 * 3600), "")

        assert wd.failing_for(miner.id, t(4 * 3600)) == pytest.approx(4 * 3600, abs=5)
        assert actions(conn)[-1] == "would_restart"

    async def test_a_row_stamped_in_the_future_cannot_wipe_the_clock(self, conn):
        """A forward clock step that NTP later corrects left classified rows
        ahead of `now`; a future `skipped_outside_hours` cleared the clock."""
        wd = Watchdog(conn, dry_run=True, fail_after=1800)
        miner = make_miner()

        await wd.consider(miner, State.STOPPED, True, t(0), "")
        conn.execute(
            "INSERT INTO events (ts, miner, state, action) VALUES (?, ?, 'stopped', 'skipped_outside_hours')",
            (t(86400).isoformat(), miner.id),
        )
        conn.commit()

        assert wd.failing_for(miner.id, t(600)) == pytest.approx(600, abs=1)


class TestLatchReachability:
    """A restart policy that can never latch restarts forever.

    An unreachable miner cannot be fixed by a restart - the attempts fail, and
    the latch is the only thing that eventually stops MinerWatch trying and
    tells a human to walk over there. A config where the rate window is too
    narrow to hold `max_restarts` attempts silently removes that stop.
    """

    def _cfg(self, tmp_path, **watchdog):
        import yaml
        body = {
            "poll_interval_seconds": 15,
            "timezone": "UTC",
            "schedule": {"days": ["mon"], "hours": ["00:00-24:00"]},
            "watchdog": watchdog,
            "miners": [{"id": "a", "host": "10.0.0.1"}],
        }
        path = tmp_path / "m.yaml"
        path.write_text(yaml.safe_dump(body), encoding="utf-8")
        return str(path)

    def test_a_policy_that_can_never_latch_is_rejected(self, tmp_path):
        from minerwatch.config import ConfigError, load_config

        # 3 attempts 1800s apart span 3600s, which does not fit in 3600s.
        path = self._cfg(tmp_path, cooldown_seconds=1800, max_restarts=3,
                         rate_window_seconds=3600)
        with pytest.raises(ConfigError) as exc:
            load_config(path)
        assert "never latch" in str(exc.value)
        assert "3600" in str(exc.value)

    def test_the_real_fleet_policy_is_accepted(self, tmp_path):
        """900s x 3 attempts spans 1800s, comfortably inside an hour."""
        from minerwatch.config import load_config

        path = self._cfg(tmp_path, cooldown_seconds=900, max_restarts=3,
                         rate_window_seconds=3600)
        _, _, _, miners = load_config(path)
        assert miners["a"].watchdog.cooldown_seconds == 900

    def test_a_single_attempt_policy_is_accepted(self, tmp_path):
        """max_restarts: 1 latches on the first attempt; no span is needed."""
        from minerwatch.config import load_config

        path = self._cfg(tmp_path, cooldown_seconds=7200, max_restarts=1,
                         rate_window_seconds=60)
        _, _, _, miners = load_config(path)
        assert miners["a"].watchdog.max_restarts == 1
