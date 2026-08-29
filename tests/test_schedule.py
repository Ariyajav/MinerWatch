from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from minerwatch.config import load_config
from minerwatch.schedule import is_working_time

from .fixtures import MINERS_YAML


def _load():
    _, _, _, miners = load_config(MINERS_YAML)
    return miners


def make_dt(year, month, day, hour, minute, tz=None):
    dt = datetime(year, month, day, hour, minute, tzinfo=tz or timezone.utc)
    return dt


class TestMinerOverrideBeatsGroup:
    def test_miner01_works_tue_10am(self):
        miners = _load()
        dt = make_dt(2025, 1, 7, 10, 0)  # Tue
        assert is_working_time(miners["miner-01"], dt) is True

    def test_miner02_does_not_work_tue_10am(self):
        miners = _load()
        dt = make_dt(2025, 1, 7, 10, 0)  # Tue
        assert is_working_time(miners["miner-02"], dt) is False

    def test_miner02_works_tue_22_30(self):
        miners = _load()
        dt = make_dt(2025, 1, 7, 22, 30)  # Tue
        assert is_working_time(miners["miner-02"], dt) is True


class TestMidnightCrossing:
    """Schedule: 22:00-02:00 on Mon (0)"""

    def _miner_with_midnight_range(self):
        _, _, _, miners = load_config(MINERS_YAML)
        return miners["miner-01"]

    def test_mon_23_00_true(self):
        miners = _load()
        dt = make_dt(2025, 1, 6, 23, 0)  # Mon
        # Build a miner with 22:00-02:00 on Mon
        from minerwatch.models import Miner, Schedule, Window, Range
        from zoneinfo import ZoneInfo
        m = Miner(
            id="test",
            host="127.0.0.1",
            port=4101,
            schedule=Schedule(
                timezone=ZoneInfo("UTC"),
                windows=[Window(days=frozenset({0}), ranges=[Range(start=22*60, end=2*60)])],
            ),
        )
        assert is_working_time(m, dt) is True

    def test_tue_01_00_true(self):
        from minerwatch.models import Miner, Schedule, Window, Range
        from zoneinfo import ZoneInfo
        dt = make_dt(2025, 1, 7, 1, 0)  # Tue -> (Mon-1)%7 = 0 in days
        m = Miner(
            id="test",
            host="127.0.0.1",
            port=4101,
            schedule=Schedule(
                timezone=ZoneInfo("UTC"),
                windows=[Window(days=frozenset({0}), ranges=[Range(start=22*60, end=2*60)])],
            ),
        )
        assert is_working_time(m, dt) is True

    def test_tue_02_00_false(self):
        from minerwatch.models import Miner, Schedule, Window, Range
        from zoneinfo import ZoneInfo
        dt = make_dt(2025, 1, 7, 2, 0)  # Tue, half-open
        m = Miner(
            id="test",
            host="127.0.0.1",
            port=4101,
            schedule=Schedule(
                timezone=ZoneInfo("UTC"),
                windows=[Window(days=frozenset({0}), ranges=[Range(start=22*60, end=2*60)])],
            ),
        )
        assert is_working_time(m, dt) is False

    def test_mon_21_00_false(self):
        from minerwatch.models import Miner, Schedule, Window, Range
        from zoneinfo import ZoneInfo
        dt = make_dt(2025, 1, 6, 21, 0)  # Mon, before start
        m = Miner(
            id="test",
            host="127.0.0.1",
            port=4101,
            schedule=Schedule(
                timezone=ZoneInfo("UTC"),
                windows=[Window(days=frozenset({0}), ranges=[Range(start=22*60, end=2*60)])],
            ),
        )
        assert is_working_time(m, dt) is False


class TestNoSchedule:
    def test_miner03_always_false(self):
        miners = _load()
        dt = make_dt(2025, 1, 7, 10, 0)
        assert is_working_time(miners["miner-03"], dt) is False


class TestWeekendDay:
    def test_saturday_false(self):
        miners = _load()
        dt = make_dt(2025, 1, 4, 10, 0)  # Sat
        assert is_working_time(miners["miner-01"], dt) is False


class TestHalfOpenBoundary:
    def test_09_00_true(self):
        miners = _load()
        dt = make_dt(2025, 1, 6, 9, 0)  # Mon
        assert is_working_time(miners["miner-01"], dt) is True

    def test_17_00_false(self):
        miners = _load()
        dt = make_dt(2025, 1, 6, 17, 0)  # Mon
        assert is_working_time(miners["miner-01"], dt) is False


class TestTimezoneConversion:
    def test_utc_to_est_morning(self):
        from minerwatch.models import Miner, Schedule, Window, Range
        est = ZoneInfo("America/New_York")
        m = Miner(
            id="test",
            host="127.0.0.1",
            port=4101,
            schedule=Schedule(
                timezone=est,
                windows=[Window(days=frozenset({0, 1, 2, 3, 4}), ranges=[Range(start=9*60, end=17*60)])],
            ),
        )
        # UTC 14:00 on Mon -> EST 09:00 (winter) -> working
        dt = datetime(2025, 1, 6, 14, 0, tzinfo=timezone.utc)  # Mon 14:00 UTC = Mon 09:00 EST
        assert is_working_time(m, dt) is True

    def test_utc_midnight_est_previous_day(self):
        from minerwatch.models import Miner, Schedule, Window, Range
        est = ZoneInfo("America/New_York")
        m = Miner(
            id="test",
            host="127.0.0.1",
            port=4101,
            schedule=Schedule(
                timezone=est,
                windows=[Window(days=frozenset({0, 1, 2, 3, 4}), ranges=[Range(start=9*60, end=17*60)])],
            ),
        )
        # UTC 04:00 on Sat -> EST 23:00 on Fri -> should be False (Fri 23:00 > 17:00)
        dt = datetime(2025, 1, 11, 4, 0, tzinfo=timezone.utc)  # Sat UTC -> Fri 23:00 EST
        assert is_working_time(m, dt) is False
