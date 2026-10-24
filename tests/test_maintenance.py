"""Power maintenance windows: nothing is sent, latched or emailed while one is active."""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from minerwatch import maintenance as maint
from minerwatch.alerts import AlertMonitor, AlertSettings, evaluate, save_settings
from minerwatch.cli import main
from minerwatch.config import ConfigError, load_config
from minerwatch.models import Miner, Range, Schedule, Window, WatchdogConfig
from minerwatch.poller import Poller
from minerwatch.store import init_db
from minerwatch.web import FleetState, make_server, open_readonly, snapshot

NOW = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)  # a Monday

ALWAYS = Schedule(
    timezone=timezone.utc,
    windows=[Window(days=frozenset(range(7)), ranges=[Range(start=0, end=1440)])],
)

CONFIG = """
default_timezone: UTC
db_path: events.db
groups:
  farm-a: {schedule: {windows: [{days: [mon,tue,wed,thu,fri,sat,sun], ranges: ["00:00-24:00"]}]}}
  farm-b: {schedule: {windows: [{days: [mon,tue,wed,thu,fri,sat,sun], ranges: ["00:00-24:00"]}]}}
miners:
  - {id: a1, host: 10.0.0.1, group: farm-a}
  - {id: a2, host: 10.0.0.2, group: farm-a}
  - {id: b1, host: 10.0.0.3, group: farm-b}
"""


def write_config(tmp_path, extra=""):
    path = tmp_path / "miners.yaml"
    path.write_text(CONFIG + extra, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# miners.yaml
# ---------------------------------------------------------------------------

class TestConfig:
    def test_a_one_off_window_covers_only_its_group(self, tmp_path):
        path = write_config(tmp_path, """
maintenance:
  - name: PSU swap
    groups: [farm-b]
    start: 2026-10-26 09:00
    end: "2026-10-26 13:00"
""")
        miners = load_config(path)[3]
        assert miners["a1"].maintenance == () and miners["a2"].maintenance == ()
        (w,) = miners["b1"].maintenance
        assert w.name == "PSU swap" and w.active(NOW)
        assert not w.active(NOW + timedelta(hours=4))
        assert not w.active(NOW - timedelta(hours=1))

    def test_no_target_means_the_whole_fleet(self, tmp_path):
        path = write_config(tmp_path, """
maintenance:
  - days: [mon]
    ranges: ["09:00-10:00"]
""")
        miners = load_config(path)[3]
        assert all(len(m.maintenance) == 1 for m in miners.values())
        w = miners["a1"].maintenance[0]
        assert w.fleet_wide and w.active(NOW)
        assert not w.active(NOW + timedelta(days=1))

    def test_times_without_an_offset_are_in_the_window_timezone(self, tmp_path):
        path = write_config(tmp_path, """
maintenance:
  - start: 2026-10-26 12:00
    end: 2026-10-26 14:00
    timezone: Asia/Tehran
""")
        w = load_config(path)[3]["a1"].maintenance[0]
        # 12:00 in Tehran (UTC+3:30) is 08:30 UTC.
        assert w.start == datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)
        assert w.active(NOW)

    @pytest.mark.parametrize("entry, message", [
        ("{groups: [farm-c], days: [mon], ranges: ['01:00-02:00']}", "unknown group"),
        ("{miners: [zz], days: [mon], ranges: ['01:00-02:00']}", "unknown miner"),
        ("{days: [mon], ranges: ['01:00-02:00'], start: 2026-10-26 01:00, end: 2026-10-26 02:00}", "not both"),
        ("{name: x}", "not neither"),
        ("{start: 2026-10-26 01:00}", "both start and end"),
        ("{start: 2026-10-26 03:00, end: 2026-10-26 02:00}", "end must be after start"),
        ("{start: tomorrow, end: 2026-10-26 02:00}", "must look like"),
        ("{days: [mon], ranges: ['01:00-02:00'], group: farm-a}", "unknown setting"),
    ])
    def test_mistakes_are_refused(self, tmp_path, entry, message):
        path = write_config(tmp_path, f"maintenance:\n  - {entry}\n")
        with pytest.raises(ConfigError, match=message):
            load_config(path)


# ---------------------------------------------------------------------------
# maintenance.json
# ---------------------------------------------------------------------------

MINERS = {
    "a1": Miner(id="a1", host="10.0.0.1", port=4028, group="farm-a", schedule=ALWAYS),
    "b1": Miner(id="b1", host="10.0.0.3", port=4028, group="farm-b", schedule=ALWAYS),
}


class TestManual:
    def test_start_and_stop_round_trip(self, tmp_path):
        path = str(tmp_path / "maintenance.json")
        w = maint.start_manual(path, miners=MINERS, groups=["farm-a"],
                               duration=timedelta(hours=2), name="swap", now=NOW)
        (loaded,) = maint.load_manual(path)
        assert loaded.id == w.id and loaded.covers(MINERS["a1"]) and not loaded.covers(MINERS["b1"])
        assert loaded.active(NOW + timedelta(minutes=119))
        assert not loaded.active(NOW + timedelta(minutes=120))

        stopped = maint.stop_manual(path, w.id, now=NOW + timedelta(minutes=5))
        assert [s.id for s in stopped] == [w.id]
        assert not maint.load_manual(path)[0].active(NOW + timedelta(minutes=6))
        with pytest.raises(ValueError, match="already ended"):
            maint.stop_manual(path, w.id, now=NOW + timedelta(minutes=7))

    def test_until_stopped_and_stop_all(self, tmp_path):
        path = str(tmp_path / "maintenance.json")
        maint.start_manual(path, miners=MINERS, now=NOW)
        maint.start_manual(path, miners=MINERS, miner_ids=["b1"], now=NOW)
        assert all(w.active(NOW + timedelta(days=30)) for w in maint.load_manual(path))
        assert len(maint.stop_manual(path, now=NOW + timedelta(hours=1))) == 2

    def test_bad_requests_are_refused(self, tmp_path):
        path = str(tmp_path / "maintenance.json")
        with pytest.raises(ValueError, match="unknown group"):
            maint.start_manual(path, miners=MINERS, groups=["farm-z"], now=NOW)
        with pytest.raises(ValueError, match="unknown miner"):
            maint.start_manual(path, miners=MINERS, miner_ids=["zz"], now=NOW)
        with pytest.raises(ValueError, match="past"):
            maint.start_manual(path, miners=MINERS, until=NOW - timedelta(minutes=1), now=NOW)
        with pytest.raises(ValueError, match="no hand-started"):
            maint.stop_manual(path, "abc", now=NOW)

    def test_old_ended_windows_are_pruned(self, tmp_path):
        path = str(tmp_path / "maintenance.json")
        w = maint.start_manual(path, miners=MINERS, duration=timedelta(hours=1), now=NOW)
        maint.start_manual(path, miners=MINERS, duration=timedelta(hours=1),
                           now=NOW + timedelta(days=8))
        assert w.id not in [x.id for x in maint.load_manual(path)]

    def test_a_damaged_file_keeps_the_last_good_windows(self, tmp_path):
        path = str(tmp_path / "maintenance.json")
        maint.start_manual(path, miners=MINERS, now=NOW)
        cache = maint.ManualWindows(path)
        assert len(cache.get()) == 1
        (tmp_path / "maintenance.json").write_text("{not json", encoding="utf-8")
        assert len(cache.get()) == 1 and cache.error

    @pytest.mark.parametrize("text, minutes", [
        ("90", 90), ("90m", 90), ("2h", 120), ("1h30m", 90), ("1d", 1440), ("45min", 45),
    ])
    def test_durations(self, text, minutes):
        assert maint.parse_duration(text) == timedelta(minutes=minutes)

    @pytest.mark.parametrize("text", ["", "0", "soon", "2x"])
    def test_bad_durations(self, text):
        with pytest.raises(ValueError):
            maint.parse_duration(text)


# ---------------------------------------------------------------------------
# The supervisor
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def dead_miner():
    # Nothing listens on port 1: an unreachable miner inside its window, which
    # is what every miner looks like with the power off.
    return Miner(id="m1", host="127.0.0.1", port=1, group="farm-a", schedule=ALWAYS,
                 watchdog=WatchdogConfig(fail_after_seconds=0))


def rows(conn):
    return conn.execute("SELECT action, reason FROM events ORDER BY id").fetchall()


class TestPoller:
    async def test_nothing_reaches_either_controller_during_maintenance(self, conn, tmp_path):
        path = str(tmp_path / "maintenance.json")
        miner = dead_miner()
        poller = Poller((15, ":memory:", "UTC", {"m1": miner}), conn, threading.Event(),
                        dry_run=False, maintenance_path=path)
        poller.watchdog.consider = AsyncMock()
        poller.sleeper.consider = AsyncMock(return_value=False)
        maint.start_manual(path, miners={"m1": miner}, groups=["farm-a"],
                           duration=timedelta(hours=1), name="PSU swap")

        await poller._poll_one(miner)

        poller.watchdog.consider.assert_not_awaited()
        poller.sleeper.consider.assert_not_awaited()
        ((action, reason),) = rows(conn)
        assert action == "maintenance" and reason.startswith("maintenance 'PSU swap'")

    async def test_stopping_reaches_the_running_supervisor_without_a_restart(self, conn, tmp_path):
        path = str(tmp_path / "maintenance.json")
        miner = dead_miner()
        poller = Poller((15, ":memory:", "UTC", {"m1": miner}), conn, threading.Event(),
                        dry_run=True, maintenance_path=path)
        maint.start_manual(path, miners={"m1": miner})
        await poller._poll_one(miner)
        maint.stop_manual(path)
        await poller._poll_one(miner)
        assert [a for a, _ in rows(conn)] == ["maintenance", "alert", "would_restart"]

    async def test_a_scheduled_window_from_the_config_works_without_a_file(self, conn):
        window = maint.MaintenanceWindow(name="weekly", timezone=None,
                                         windows=ALWAYS.windows and tuple(ALWAYS.windows))
        miner = dead_miner()
        miner.maintenance = (window,)
        poller = Poller((15, ":memory:", "UTC", {"m1": miner}), conn, threading.Event())
        await poller._poll_one(miner)
        assert [a for a, _ in rows(conn)] == ["maintenance"]

    async def test_the_restart_clock_starts_again_after_maintenance(self, conn, tmp_path):
        """A miner still booting when power returns gets the full delay."""
        path = str(tmp_path / "maintenance.json")
        miner = dead_miner()
        miner.watchdog = WatchdogConfig(fail_after_seconds=1800)
        poller = Poller((15, ":memory:", "UTC", {"m1": miner}), conn, threading.Event(),
                        dry_run=True, maintenance_path=path)
        poller.watchdog.consider = AsyncMock(wraps=poller.watchdog.consider)

        # Failing for an hour before the window opened, so the clock was well
        # past fail_after - then a two-hour window.
        start = datetime.now(timezone.utc) - timedelta(hours=3)
        for i in range(0, 3600, 300):
            conn.execute("INSERT INTO events (ts, miner, state, action) VALUES (?, 'm1', "
                         "'unreachable', 'waiting_to_restart')",
                         ((start + timedelta(seconds=i)).isoformat(),))
        for i in range(3600, 3 * 3600 - 60, 300):
            conn.execute("INSERT INTO events (ts, miner, state, action) VALUES (?, 'm1', "
                         "'unreachable', 'maintenance')",
                         ((start + timedelta(seconds=i)).isoformat(),))
        conn.commit()

        await poller._poll_one(miner)
        assert rows(conn)[-1][0] == "waiting_to_restart"
        assert "restart in 1800s" in rows(conn)[-1][1]


# ---------------------------------------------------------------------------
# Alerts and the dashboard
# ---------------------------------------------------------------------------

def add(conn, mid, minutes_ago, state, action, ghs=None, now=NOW):
    conn.execute(
        "INSERT INTO events (ts, miner, state, action, reason, ghs) VALUES (?, ?, ?, ?, ?, ?)",
        ((now - timedelta(minutes=minutes_ago)).isoformat(), mid, state, action, None, ghs),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "events.db")
    c = init_db(path)
    yield path, c
    c.close()


class Outbox:
    def __init__(self):
        self.sent = []

    def __call__(self, settings, subject, body):
        self.sent.append((subject, body))


class TestAlerts:
    def test_no_alert_for_a_miner_in_maintenance(self, db, tmp_path):
        path, conn = db
        mpath = str(tmp_path / "maintenance.json")
        add(conn, "a1", 90, "stopped", "needs_attention")
        for i in range(60, -1, -1):
            add(conn, "a1", i, "unreachable", "alert")
            add(conn, "b1", i, "unreachable", "alert")
        maint.start_manual(mpath, miners=MINERS, groups=["farm-a"], now=NOW - timedelta(hours=1))
        fleet = FleetState(MINERS, mpath)
        snap = snapshot(conn, MINERS, 15, now=NOW, fleet=fleet)
        keys = set(evaluate(snap, AlertSettings(), conn, NOW))
        assert keys == {"down:b1"}
        assert snap["miners"][0]["maintenance"]["name"] == "manual maintenance"
        assert not snap["fleet_maintenance"]

    def test_a_silent_supervisor_is_expected_during_fleet_maintenance(self, db, tmp_path):
        path, conn = db
        mpath = str(tmp_path / "maintenance.json")
        add(conn, "a1", 120, "mining", "none", ghs=1)
        maint.start_manual(mpath, miners=MINERS, now=NOW - timedelta(hours=1))
        snap = snapshot(conn, MINERS, 15, now=NOW, fleet=FleetState(MINERS, mpath))
        assert snap["stale"] and snap["fleet_maintenance"]
        assert evaluate(snap, AlertSettings(), conn, NOW) == {}

    def test_an_alert_sent_before_maintenance_is_not_resolved_by_it(self, db, tmp_path):
        path, conn = db
        mpath = str(tmp_path / "maintenance.json")
        settings_path = str(tmp_path / "alerts.json")
        save_settings(settings_path, AlertSettings(
            enabled=True, smtp_host="smtp.example.com", from_addr="mw@example.com",
            to_addrs=["me@example.com"]))
        outbox = Outbox()
        fleet = FleetState(MINERS, mpath)
        import functools
        mon = AlertMonitor(settings_path, path, MINERS, 15,
                           functools.partial(snapshot, fleet=fleet), open_readonly,
                           sender=outbox)
        add(conn, "a1", 30, "stopped", "needs_attention")
        add(conn, "a1", 0, "stopped", "alert")
        add(conn, "b1", 0, "mining", "none", ghs=1)
        mon.check_once(NOW)
        assert len(outbox.sent) == 1 and "a1 is latched" in outbox.sent[0][0]

        maint.start_manual(mpath, miners=MINERS, miner_ids=["a1"], now=NOW)
        add(conn, "a1", -1, "unreachable", "maintenance")
        add(conn, "b1", -1, "mining", "none", ghs=1)
        mon.check_once(NOW + timedelta(minutes=1))
        assert len(outbox.sent) == 1  # no "Resolved" while it is not being looked at


@pytest.fixture
def server(db, tmp_path):
    path, conn = db
    add(conn, "a1", 0, "mining", "none", ghs=1, now=datetime.now(timezone.utc))
    mpath = str(tmp_path / "maintenance.json")
    srv = make_server("127.0.0.1", 0, path, MINERS, 15, fleet=FleetState(MINERS, mpath))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", mpath
    srv.shutdown()
    srv.server_close()


def post(url, body, header=True):
    headers = {"Content-Type": "application/json"}
    if header:
        headers["X-MinerWatch"] = "1"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


class TestDashboard:
    def test_start_and_end_from_the_page(self, server):
        base, mpath = server
        started = post(base + "/api/maintenance/start", {"groups": ["farm-b"], "minutes": 60,
                                                         "name": "PSU"})
        assert started["window"]["scope"] == "group farm-b"
        with urllib.request.urlopen(base + "/api/status", timeout=5) as r:
            status = json.loads(r.read())
        by_id = {m["id"]: m for m in status["miners"]}
        assert by_id["b1"]["maintenance"]["name"] == "PSU" and by_id["a1"]["maintenance"] is None
        assert [w["name"] for w in status["maintenance"] if w["active"]] == ["PSU"]

        post(base + "/api/maintenance/stop", {"id": started["window"]["id"]})
        assert not maint.load_manual(mpath)[0].active(datetime.now(timezone.utc))

    def test_the_page_cannot_be_driven_cross_site(self, server):
        base, mpath = server
        with pytest.raises(urllib.error.HTTPError) as err:
            post(base + "/api/maintenance/start", {"minutes": 60}, header=False)
        assert err.value.code == 403
        assert maint.load_manual(mpath) == []

    def test_bad_requests_say_why(self, server):
        base, _ = server
        with pytest.raises(urllib.error.HTTPError) as err:
            post(base + "/api/maintenance/start", {"groups": ["nope"]})
        assert err.value.code == 400 and b"unknown group" in err.value.read()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli:
    def test_start_list_stop(self, tmp_path, capsys):
        cfg = write_config(tmp_path)
        assert main(["-c", cfg, "maintenance", "start", "--group", "farm-a", "--for", "2h",
                     "--name", "PSU swap"]) == 0
        out = capsys.readouterr().out
        assert "covers 2 miner(s): a1, a2" in out and "no restart needed" in out
        (w,) = maint.load_manual(str(tmp_path / "maintenance.json"))

        assert main(["-c", cfg, "maintenance"]) == 0
        assert "ACTIVE NOW" in capsys.readouterr().out

        assert main(["-c", cfg, "status"]) == 0
        out = capsys.readouterr().out
        assert "MAINTENANCE" in out and "maint" in out

        assert main(["-c", cfg, "maintenance", "stop", w.id]) == 0
        assert "Maintenance ended" in capsys.readouterr().out

    def test_a_length_is_required(self, tmp_path):
        cfg = write_config(tmp_path)
        with pytest.raises(SystemExit):
            main(["-c", cfg, "maintenance", "start"])

    def test_unknown_group_is_refused(self, tmp_path, capsys):
        cfg = write_config(tmp_path)
        assert main(["-c", cfg, "maintenance", "start", "--group", "x", "--for", "1h"]) == 2
        assert "unknown group" in capsys.readouterr().err

    def test_until_is_read_in_the_default_timezone(self, tmp_path):
        cfg = write_config(tmp_path)
        assert main(["-c", cfg, "maintenance", "start", "--until", "2099-01-01 10:00"]) == 0
        (w,) = maint.load_manual(str(tmp_path / "maintenance.json"))
        assert w.end == datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)
