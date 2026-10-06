"""The dashboard shows what `status` and `history` show, and can change nothing."""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from minerwatch.cli import build_parser, _normalise_argv
from minerwatch.models import Miner, Range, Schedule, SleepBackend, SleepConfig, Window
from minerwatch.store import init_db
from minerwatch.web import FleetState, history, make_server, open_readonly, snapshot

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def miner(mid, sleep=False):
    window = Window(days=frozenset(range(7)), ranges=[Range(start=0, end=1440)])
    cfg = SleepConfig(enabled=True, backend=SleepBackend.CGMINER) if sleep else SleepConfig()
    return Miner(id=mid, host="10.0.0.1", port=4028, group="hall-a",
                 schedule=Schedule(timezone=timezone.utc, windows=[window]), sleep=cfg)


MINERS = {"m1": miner("m1"), "m2": miner("m2", sleep=True)}


def add(conn, mid, minutes_ago, state, action, reason=None, ghs=None):
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    conn.execute(
        "INSERT INTO events (ts, miner, state, action, reason, ghs) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, mid, state, action, reason, ghs),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "events.db")
    conn = init_db(path)
    yield path, conn
    conn.close()


class TestSnapshot:
    def test_state_and_hashrate_come_from_the_newest_poll(self, db):
        path, conn = db
        add(conn, "m1", 2, "mining", "none", ghs=141000)
        # A controller row after the poll must not overwrite the observation.
        add(conn, "m1", 1, "unknown", "attention_cleared")
        snap = snapshot(conn, MINERS, 15, now=NOW)
        m1 = snap["miners"][0]
        assert m1["state"] == "mining" and m1["ghs"] == 141000
        assert m1["last_decision"]["action"] == "attention_cleared"
        assert m1["window"] == "open"

    def test_a_latched_miner_is_named_with_when_it_latched(self, db):
        path, conn = db
        add(conn, "m1", 60, "stopped", "needs_attention", "restart limit reached")
        add(conn, "m1", 1, "stopped", "alert", "zero hashrate", ghs=0)
        m1 = snapshot(conn, MINERS, 15, now=NOW)["miners"][0]
        assert m1["latches"] == ["watchdog"]
        assert m1["latched_since"].startswith("2026-10-05T11:00")
        assert m1["reason"] == "zero hashrate"

    def test_a_sleep_whose_outcome_is_unknown_shows_as_unsure(self, db):
        path, conn = db
        add(conn, "m2", 5, "mining", "sleep_failed", "HTTP 500")
        m2 = snapshot(conn, MINERS, 15, now=NOW)["miners"][1]
        assert m2["power"] == "unsure"

    def test_no_recent_poll_is_flagged_as_a_stale_supervisor(self, db):
        path, conn = db
        add(conn, "m1", 10, "mining", "none", ghs=1)
        assert snapshot(conn, MINERS, 15, now=NOW)["stale"] is True
        add(conn, "m1", 0, "mining", "none", ghs=1)
        assert snapshot(conn, MINERS, 15, now=NOW)["stale"] is False

    def test_an_empty_database_is_stale_not_an_error(self, db):
        path, conn = db
        snap = snapshot(conn, MINERS, 15, now=NOW)
        assert snap["stale"] is True and snap["newest_poll"] is None
        assert snap["miners"][0]["state"] == "unknown"


class TestIncremental:
    def test_later_rows_are_picked_up_without_a_rescan(self, db):
        path, conn = db
        add(conn, "m1", 30, "mining", "none", ghs=140000)
        add(conn, "m2", 30, "mining", "sleep")
        fleet = FleetState(MINERS)
        first = snapshot(conn, MINERS, 15, now=NOW, fleet=fleet)
        assert first["miners"][1]["power"] == "asleep"

        add(conn, "m1", 20, "stopped", "needs_attention")
        add(conn, "m1", 1, "stopped", "alert", "zero hashrate", ghs=0)
        add(conn, "m2", 1, "mining", "wake")
        later = snapshot(conn, MINERS, 15, now=NOW, fleet=fleet)
        m1, m2 = later["miners"]
        assert m1["state"] == "stopped" and m1["latches"] == ["watchdog"]
        assert m2["power"] == "awake"
        # And it agrees with a fresh full read of the same table.
        fresh = snapshot(conn, MINERS, 15, now=NOW)
        assert fresh["miners"] == later["miners"]

    def test_a_cleared_latch_clears(self, db):
        path, conn = db
        add(conn, "m1", 20, "stopped", "needs_attention")
        fleet = FleetState(MINERS)
        assert snapshot(conn, MINERS, 15, now=NOW, fleet=fleet)["miners"][0]["latches"]
        add(conn, "m1", 10, "unknown", "attention_cleared")
        assert snapshot(conn, MINERS, 15, now=NOW, fleet=fleet)["miners"][0]["latches"] == []


class TestHistory:
    def test_decisions_are_separated_from_readings(self, db):
        path, conn = db
        add(conn, "m1", 30, "mining", "none", ghs=140000)
        add(conn, "m1", 20, "stopped", "alert", ghs=0)
        add(conn, "m1", 10, "stopped", "waiting_to_restart", "failing for 600s")
        h = history(conn, "m1", 24, now=NOW)
        assert [p["ghs"] for p in h["points"]] == [140000, 0]
        assert [d["action"] for d in h["decisions"]] == ["waiting_to_restart"]
        assert h["decisions"][0]["why"] == "failing for 600s"

    def test_downsampling_keeps_a_short_drop_to_zero(self, db):
        path, conn = db
        rows = [(NOW - timedelta(seconds=15 * i)).isoformat() for i in range(2000)]
        conn.executemany(
            "INSERT INTO events (ts, miner, state, action, ghs) VALUES (?, 'm1', ?, ?, ?)",
            [(ts, "stopped" if i == 777 else "mining", "alert" if i == 777 else "none",
              0 if i == 777 else 140000) for i, ts in enumerate(rows)],
        )
        conn.commit()
        h = history(conn, "m1", 24, now=NOW)
        assert len(h["points"]) <= 360
        assert any(p["ghs"] == 0 for p in h["points"])


class TestReadOnly:
    def test_the_connection_refuses_writes(self, db):
        path, _ = db
        ro = open_readonly(path)
        try:
            with pytest.raises(Exception, match="readonly"):
                ro.execute("INSERT INTO events (ts, miner, state) VALUES ('x', 'm1', 'x')")
        finally:
            ro.close()


@pytest.fixture
def server(db):
    path, conn = db
    add(conn, "m1", 0, "mining", "none", ghs=140000)
    srv = make_server("127.0.0.1", 0, path, MINERS, 15)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", conn
    srv.shutdown()
    srv.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


class TestHttp:
    def test_page_status_and_history(self, server):
        base, _ = server
        code, ctype, body = get(base + "/")
        assert code == 200 and ctype.startswith("text/html") and b"MinerWatch" in body
        code, _, body = get(base + "/api/status")
        assert [m["id"] for m in json.loads(body)["miners"]] == ["m1", "m2"]
        code, _, body = get(base + "/api/history?miner=m1&hours=6")
        assert json.loads(body)["miner"] == "m1"

    def test_unknown_miner_and_path_are_404(self, server):
        base, _ = server
        for path in ("/api/history?miner=nope", "/nope"):
            with pytest.raises(urllib.error.HTTPError) as err:
                get(base + path)
            assert err.value.code == 404

    def test_writes_are_refused_and_change_nothing(self, server):
        base, conn = server
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        req = urllib.request.Request(base + "/api/status", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 405
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_cli_accepts_web_with_and_without_a_config_flag():
    args = build_parser().parse_args(_normalise_argv(["web", "--port", "9000"]))
    assert args.command == "web" and args.port == 9000 and args.host == "127.0.0.1"
    args = build_parser().parse_args(_normalise_argv(["-c", "x.yaml", "web"]))
    assert args.config == "x.yaml" and args.port == 8787
    assert args.no_config_editor is False
    assert build_parser().parse_args(["web", "--no-config-editor"]).no_config_editor is True


def test_an_unexpected_error_reaches_the_page_instead_of_a_dropped_connection(server, monkeypatch):
    base, _ = server
    import minerwatch.web as web

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(web, "snapshot", boom)
    with pytest.raises(urllib.error.HTTPError) as err:
        get(base + "/api/status")
    assert err.value.code == 500
    assert "RuntimeError: kaboom" in json.loads(err.value.read())["error"]
