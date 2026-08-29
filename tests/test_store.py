import tempfile

from minerwatch.models import Event
from minerwatch.store import init_db, record_event, last_state


def test_init_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = init_db(path)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'")
    assert cursor.fetchone() is not None
    conn.close()


def test_record_event_round_trip():
    conn = init_db(":memory:")
    event = Event(ts="2025-01-07T10:00:00", miner="miner-01", state="mining", action="none")
    record_event(conn, event)

    cursor = conn.execute("SELECT ts, miner, state, action, reason FROM events")
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "2025-01-07T10:00:00"
    assert row[1] == "miner-01"
    assert row[2] == "mining"
    assert row[3] == "none"
    assert row[4] is None


def test_last_state():
    conn = init_db(":memory:")
    e1 = Event(ts="2025-01-07T10:00:00", miner="miner-01", state="mining", action="none")
    e2 = Event(ts="2025-01-07T10:05:00", miner="miner-01", state="stopped", action="alert")
    record_event(conn, e1)
    record_event(conn, e2)

    last = last_state(conn, "miner-01")
    assert last is not None
    assert last.ts == "2025-01-07T10:05:00"
    assert last.state == "stopped"
    assert last.action == "alert"


def test_unknown_miner_returns_none():
    conn = init_db(":memory:")
    assert last_state(conn, "nonexistent") is None


class TestHashrateColumn:
    """Hashrate is what tells a real sleep from a low-power mode.

    Both stop full-rate mining; only one goes to zero. Recording the number
    turns "the sleep command was accepted" into "the miner actually stopped".
    """

    def test_round_trip(self):
        conn = init_db(":memory:")
        try:
            record_event(conn, Event(ts="2026-01-01T00:00:00+00:00", miner="m",
                                     state="mining", action="none", ghs=95170.14))
            assert last_state(conn, "m").ghs == 95170.14
        finally:
            conn.close()

    def test_zero_is_distinct_from_absent(self):
        conn = init_db(":memory:")
        try:
            record_event(conn, Event(ts="2026-01-01T00:00:00+00:00", miner="slept",
                                     state="stopped", action="expected_off", ghs=0.0))
            record_event(conn, Event(ts="2026-01-01T00:00:00+00:00", miner="gone",
                                     state="unreachable", action="alert", ghs=None))
            assert last_state(conn, "slept").ghs == 0.0
            assert last_state(conn, "gone").ghs is None
        finally:
            conn.close()

    def test_an_existing_database_is_migrated_not_replaced(self, tmp_path):
        """A deployment already collecting history must keep it."""
        import sqlite3

        path = str(tmp_path / "old.db")
        old = sqlite3.connect(path)
        old.execute(
            """CREATE TABLE events (
                id INTEGER PRIMARY KEY, ts TEXT NOT NULL, miner TEXT NOT NULL,
                state TEXT NOT NULL, action TEXT, reason TEXT)"""
        )
        old.execute(
            "INSERT INTO events (ts, miner, state, action, reason) VALUES (?,?,?,?,?)",
            ("2026-01-01T00:00:00+00:00", "legacy", "mining", "none", None),
        )
        old.commit()
        old.close()

        conn = init_db(path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            assert "ghs" in columns
            legacy = last_state(conn, "legacy")
            assert legacy is not None, "existing history must survive the migration"
            assert legacy.state == "mining"
            assert legacy.ghs is None, "rows written before the column read back as unknown"

            record_event(conn, Event(ts="2026-01-02T00:00:00+00:00", miner="legacy",
                                     state="mining", action="none", ghs=95170.0))
            assert last_state(conn, "legacy").ghs == 95170.0
        finally:
            conn.close()

    def test_migration_is_idempotent(self, tmp_path):
        path = str(tmp_path / "twice.db")
        for _ in range(3):
            conn = init_db(path)
            conn.close()
        conn = init_db(path)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(events)")]
            assert columns.count("ghs") == 1
        finally:
            conn.close()
