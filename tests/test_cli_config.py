"""Tests for the `config` subcommand.

Group inheritance makes mistakes invisible in the YAML itself: a miner in the
wrong group, or a `sleep:` block that was never enabled, reads fine in the file
and only shows up as an odd column in `status`. This command exists to make the
resolved settings legible, so the tests are mostly about it telling the truth
about what was resolved.
"""

import tempfile
from datetime import timezone

import pytest
import yaml

from minerwatch.cli import _fmt_days, _fmt_hhmm, _fmt_schedule, _hour_map, main
from minerwatch.models import Miner, Range, Schedule, Window


def write(raw) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.dump(raw, f)
        return f.name


BASE = {
    "poll_interval_seconds": 15,
    "default_timezone": "UTC",
    "db_path": ":memory:",
}


class TestFormatting:
    def test_hhmm(self):
        assert _fmt_hhmm(0) == "00:00"
        assert _fmt_hhmm(9 * 60) == "09:00"
        assert _fmt_hhmm(1440) == "24:00"

    def test_contiguous_days_collapse_to_a_range(self):
        assert _fmt_days(frozenset({0, 1, 2, 3, 4})) == "mon-fri"

    def test_all_seven_days(self):
        assert _fmt_days(frozenset(range(7))) == "every day"

    def test_split_runs_are_listed_separately(self):
        assert _fmt_days(frozenset({0, 1, 5, 6})) == "mon-tue,sat-sun"

    def test_single_days(self):
        assert _fmt_days(frozenset({2})) == "wed"

    def test_empty(self):
        assert _fmt_days(frozenset()) == "never"

    def test_schedule_lines_render_running_hours(self):
        s = Schedule(
            timezone=timezone.utc,
            windows=[Window(days=frozenset({0, 1, 2, 3, 4}),
                            ranges=[Range(start=17 * 60, end=9 * 60)])],
        )
        assert _fmt_schedule(s) == ["mon-fri 17:00-09:00"]

    def test_no_schedule_says_so_plainly(self):
        assert "no schedule" in _fmt_schedule(None)[0]

    def test_empty_windows_say_so_plainly(self):
        s = Schedule(timezone=timezone.utc, windows=[])
        assert "no windows" in _fmt_schedule(s)[0]


class TestHourMap:
    def _miner(self, ranges, days=frozenset(range(7))):
        return Miner(
            id="m", host="h", port=1,
            schedule=Schedule(timezone=timezone.utc,
                              windows=[Window(days=days, ranges=ranges)]),
        )

    def test_a_daytime_window(self):
        rows = _hour_map(self._miner([Range(start=9 * 60, end=17 * 60)]))
        assert rows[0] == "  mon  " + "." * 9 + "#" * 8 + "." * 7

    def test_a_window_crossing_midnight(self):
        rows = _hour_map(self._miner([Range(start=17 * 60, end=9 * 60)]))
        assert rows[0] == "  mon  " + "#" * 9 + "." * 8 + "#" * 7

    def test_no_schedule_produces_no_map(self):
        assert _hour_map(Miner(id="m", host="h", port=1)) == []

    def test_the_axis_labels_line_up_with_24_columns(self):
        rows = _hour_map(self._miner([Range(start=0, end=1440)]))
        assert len(rows) == 9                      # 7 days + 2 axis rows
        assert rows[-1].split()[-1] == "012345678901234567890123"


class TestCommandOutput:
    def test_reports_resolved_inheritance(self, capsys):
        path = write({**BASE,
            "sleep": {"enabled": True, "dry_run": True},
            "groups": {"hall": {
                "schedule": {"windows": [{"days": ["mon"], "ranges": ["09:00-17:00"]}]},
                "sleep": {"backend": "bitmain_http", "password": "s3cret"}}},
            "miners": [{"id": "rack-1", "host": "10.0.0.9", "port": 4028, "group": "hall"}]})
        assert main(["-c", path, "config"]) == 0
        out = capsys.readouterr().out
        assert "rack-1" in out and "hall" in out
        assert "10.0.0.9:4028" in out
        assert "bitmain_http dry-run" in out       # backend from group, mode from global
        assert "mon 09:00-17:00" in out
        assert "s3cret" not in out, "credentials must not be printed"

    def test_flags_miners_with_sleep_disabled(self, capsys):
        """The exact confusion this command was written for."""
        path = write({**BASE,
            "groups": {"g": {"schedule": {"windows": [{"days": ["mon"], "ranges": ["01:00-02:00"]}]}}},
            "miners": [
                {"id": "on-1",  "host": "10.0.0.1", "group": "g", "sleep": {"enabled": True}},
                {"id": "off-1", "host": "10.0.0.2", "group": "g"},
                {"id": "off-2", "host": "10.0.0.3", "group": "g"},
            ]})
        assert main(["-c", path, "config"]) == 0
        out = capsys.readouterr().out
        assert "software sleep is off for 2 of 3 miner(s)" in out
        assert "off-1, off-2" in out
        assert "on-1," not in out.split("software sleep is off")[1]

    def test_says_nothing_when_every_miner_is_enabled(self, capsys):
        path = write({**BASE,
            "sleep": {"enabled": True},
            "groups": {"g": {"schedule": {"windows": [{"days": ["mon"], "ranges": ["01:00-02:00"]}]}}},
            "miners": [{"id": "a", "host": "10.0.0.1", "group": "g"}]})
        main(["-c", path, "config"])
        assert "software sleep is off" not in capsys.readouterr().out

    def test_flags_miners_with_no_schedule(self, capsys):
        path = write({**BASE, "miners": [{"id": "loose", "host": "10.0.0.1"}]})
        main(["-c", path, "config"])
        out = capsys.readouterr().out
        assert "no schedule for: loose" in out

    def test_multiple_windows_each_get_a_line(self, capsys):
        path = write({**BASE,
            "groups": {"g": {"schedule": {"windows": [
                {"days": ["mon", "tue", "wed", "thu", "fri"], "ranges": ["00:00-09:00", "12:00-18:00"]},
                {"days": ["sat", "sun"], "ranges": ["00:00-24:00"]}]}}},
            "miners": [{"id": "a", "host": "10.0.0.1", "group": "g"}]})
        main(["-c", path, "config"])
        out = capsys.readouterr().out
        assert "mon-fri 00:00-09:00, 12:00-18:00" in out
        assert "sat-sun 00:00-24:00" in out

    def test_hours_map_groups_miners_sharing_a_schedule(self, capsys):
        path = write({**BASE,
            "groups": {"g": {"schedule": {"windows": [{"days": ["mon"], "ranges": ["09:00-17:00"]}]}}},
            "miners": [
                {"id": "a", "host": "10.0.0.1", "group": "g"},
                {"id": "b", "host": "10.0.0.2", "group": "g"},
            ]})
        main(["-c", path, "config", "--hours"])
        out = capsys.readouterr().out
        assert "a, b" in out, "miners on the same schedule share one map"
        assert "# = mining, . = asleep" in out
        assert out.count("# = mining") == 1

    def test_a_bad_config_is_reported_not_traced(self, capsys):
        path = write({**BASE, "miners": [{"id": "a", "port": 99999}]})
        assert main(["-c", path, "config"]) == 2
        assert "out of range" in capsys.readouterr().err


class TestLint:
    """Configurations that parse but cannot work.

    The port case is from the field: every miner was given `port: 80`, the web
    UI, so all twelve polled as unreachable with a blank reason and the fault
    looked like a network problem.
    """

    def test_a_web_ui_poll_port_is_flagged(self, capsys):
        path = write({**BASE,
            "miners": [{"id": "s19-01", "host": "10.0.0.5", "port": 80}]})
        main(["-c", path, "config"])
        out = capsys.readouterr().out
        assert "PROBLEM: s19-01" in out
        assert "web-UI port" in out
        assert "4028" in out

    def test_the_bitmain_case_names_the_separate_http_port(self, capsys):
        """The trap: bitmain_http *does* use port 80 - but through its own key."""
        path = write({**BASE,
            "groups": {"g": {
                "schedule": {"windows": [{"days": ["mon"], "ranges": ["01:00-02:00"]}]},
                "sleep": {"enabled": True, "backend": "bitmain_http"}}},
            "miners": [{"id": "sxp-01", "host": "10.0.0.1", "port": 80, "group": "g"}]})
        main(["-c", path, "config"])
        out = capsys.readouterr().out
        assert "sleep.http_port" in out
        assert "set 'port: 4028'" in out

    def test_the_conventional_port_is_not_flagged(self, capsys):
        path = write({**BASE,
            "miners": [{"id": "ok", "host": "10.0.0.1", "port": 4028}]})
        main(["-c", path, "config"])
        assert "PROBLEM" not in capsys.readouterr().out

    def test_an_unusual_but_plausible_port_is_not_flagged(self, capsys):
        # Only actual web ports are called out; a relocated API is legitimate.
        path = write({**BASE,
            "miners": [{"id": "ok", "host": "10.0.0.1", "port": 4029}]})
        main(["-c", path, "config"])
        assert "PROBLEM" not in capsys.readouterr().out

    def test_two_miners_on_one_address_are_flagged(self, capsys):
        path = write({**BASE, "miners": [
            {"id": "a", "host": "10.0.0.1", "port": 4028},
            {"id": "b", "host": "10.0.0.1", "port": 4028}]})
        main(["-c", path, "config"])
        out = capsys.readouterr().out
        assert "same address as a" in out

    def test_lint_notes_also_reach_the_log_at_startup(self, caplog):
        """So a `run` under Task Scheduler records them too, not just `config`."""
        import logging

        from minerwatch.config import load_config

        path = write({**BASE, "miners": [{"id": "z", "host": "10.0.0.1", "port": 443}]})
        with caplog.at_level(logging.WARNING):
            load_config(path)
        assert "web-UI port" in caplog.text


class TestUnreachableReason:
    def test_a_timeout_is_named_rather_than_blank(self):
        """A bare TimeoutError stringifies to '' - exactly what polling a web
        server produces, and 'unreachable' with no reason helps nobody."""
        import asyncio

        from minerwatch.poller import classify

        state, reason = classify(asyncio.TimeoutError())
        assert state.value == "unreachable"
        assert reason == "TimeoutError"

    def test_a_message_is_preserved_when_there_is_one(self):
        from minerwatch.poller import classify

        assert classify(ConnectionRefusedError("connection refused"))[1] == "connection refused"


class TestRehearseActuallyRehearses:
    """A mode named 'rehearse' must never actuate hardware.

    Found in the field: install-task.ps1 -Mode rehearse passed no flags, which
    means "honour each miner's configured dry_run". For a config containing
    `dry_run: false` that is the opposite of a rehearsal, and two live miners
    were scheduled to be slept for real under a mode that promised otherwise.
    """

    def _live_config(self):
        return write({**BASE,
            "groups": {"farm-b": {
                "schedule": {"windows": [{"days": ["mon"], "ranges": ["03:00-03:01"]}]},
                "sleep": {"enabled": True, "dry_run": False,
                          "backend": "cgminer", "cooldown_seconds": 0}}},
            "miners": [{"id": "sxp-01", "host": "127.0.0.1", "port": 4028, "group": "farm-b"}]})

    def test_no_flags_honours_the_config_and_can_be_live(self):
        """The behaviour that made the mode name a lie. Pinned so it stays known."""
        from minerwatch.cli import build_parser

        args = build_parser().parse_args(["-c", self._live_config(), "run"])
        assert args.dry_run is False
        assert args.live is False and args.live_sleep is False
        # cmd_run maps this to sleep_dry=None, i.e. "whatever the config says".

    def test_dry_run_flag_forces_a_rehearsal(self):
        from minerwatch.cli import build_parser

        args = build_parser().parse_args(["-c", self._live_config(), "run", "--dry-run"])
        assert args.dry_run is True

    async def test_forced_rehearsal_beats_a_live_config(self, tmp_path):
        """End to end: sleep_dry_run=True must win over dry_run: false."""
        import threading

        from minerwatch.config import load_config
        from minerwatch.poller import Poller
        from minerwatch.store import init_db
        from sim.miner_sim import SimServer, SimState

        state = SimState(ghs=13500, seed=2)
        server = SimServer(("127.0.0.1", 0), state, control_file=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            path = write({**BASE,
                "groups": {"farm-b": {
                    "schedule": {"windows": [{"days": ["mon"], "ranges": ["03:00-03:01"]}]},
                    "sleep": {"enabled": True, "dry_run": False,
                              "backend": "cgminer", "cooldown_seconds": 0}}},
                "miners": [{"id": "sxp-01", "host": "127.0.0.1",
                            "port": server.server_address[1], "group": "farm-b"}]})
            cfg = load_config(path)
            conn = init_db(":memory:")
            try:
                poller = Poller(cfg, conn, threading.Event(),
                                dry_run=True, sleep_dry_run=True)
                await poller._poll_one(list(cfg[3].values())[0])
                assert state.state == SimState.MINING, "a forced rehearsal actuated hardware"
                actions = [r[0] for r in conn.execute(
                    "SELECT action FROM events WHERE miner='sxp-01' ORDER BY id")]
                assert "would_sleep" in actions
                assert "sleep" not in actions
            finally:
                conn.close()
        finally:
            server.shutdown()
            server.server_close()

    async def test_without_the_override_the_same_config_is_live(self, tmp_path):
        """The contrast case, so the test above cannot pass vacuously."""
        import threading

        from minerwatch.config import load_config
        from minerwatch.poller import Poller
        from minerwatch.store import init_db
        from sim.miner_sim import SimServer, SimState

        state = SimState(ghs=13500, seed=2)
        server = SimServer(("127.0.0.1", 0), state, control_file=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            path = write({**BASE,
                "groups": {"farm-b": {
                    "schedule": {"windows": [{"days": ["mon"], "ranges": ["03:00-03:01"]}]},
                    "sleep": {"enabled": True, "dry_run": False,
                              "backend": "cgminer", "cooldown_seconds": 0}}},
                "miners": [{"id": "sxp-01", "host": "127.0.0.1",
                            "port": server.server_address[1], "group": "farm-b"}]})
            cfg = load_config(path)
            conn = init_db(":memory:")
            try:
                poller = Poller(cfg, conn, threading.Event(),
                                dry_run=True, sleep_dry_run=None)   # honour config
                await poller._poll_one(list(cfg[3].values())[0])
                assert state.state == SimState.SLEEPING
            finally:
                conn.close()
        finally:
            server.shutdown()
            server.server_close()


class TestStatusWhy:
    """An unreachable miner's reason is the whole diagnosis.

    From the field: eleven of twelve miners read `unreachable` and the table
    said nothing about why. The reason was in the database, but only reachable
    by hand-writing SQL.
    """

    def test_translations_cover_the_real_signatures(self):
        from minerwatch.cli import _diagnose

        # Accepted then closed with no data: cgminer's api-allow denying a host.
        assert "api-allow" in _diagnose("invalid JSON")
        # Windows and Linux word a refused connection differently.
        assert "api-listen" in _diagnose("[WinError 10061] ... actively refused it")
        assert "api-listen" in _diagnose("[Errno 111] Connect call failed ('10.0.0.1', 4028)")
        # No reply at all.
        assert "powered down" in _diagnose("TimeoutError")
        assert "powered down" in _diagnose("[WinError 10060] ... timed out")

    def test_an_unrecognised_reason_is_still_shown_verbatim(self):
        from minerwatch.cli import _diagnose

        assert _diagnose("something entirely new") is None

    def _fleet(self, port_a, port_b):
        return write({**BASE,
            "groups": {"g": {"schedule": {"windows": [
                {"days": ["mon","tue","wed","thu","fri","sat","sun"], "ranges": ["00:00-24:00"]}]}}},
            "miners": [
                {"id": "a", "host": "127.0.0.1", "port": port_a, "group": "g"},
                {"id": "b", "host": "127.0.0.1", "port": port_b, "group": "g"},
            ]})

    async def test_identical_failures_are_grouped(self, capsys):
        """A fleet failing the same way has one cause, not twelve."""
        import threading

        from minerwatch.config import load_config
        from minerwatch.poller import Poller
        from minerwatch.store import init_db

        # Two ports with nothing listening produce the same class of error.
        path = self._fleet(4591, 4592)
        cfg = load_config(path)
        conn = init_db(":memory:")
        try:
            poller = Poller(cfg, conn, threading.Event(), dry_run=True, sleep_dry_run=True)
            for m in cfg[3].values():
                await poller._poll_one(m)

            class A:
                config = path
            cmd_status = __import__("minerwatch.cli", fromlist=["cmd_status"]).cmd_status
            cmd_status(A(), cfg, conn)
            out = capsys.readouterr().out
            assert "WHY:" in out
            assert "a, b" in out, "identical reasons must be reported once, not per miner"
        finally:
            conn.close()

    async def test_the_reason_comes_from_the_poll_not_the_watchdog(self, capsys):
        """The watchdog's own event is newer and its reason describes its
        decision ('dry-run: would send restart'), not the connection failure."""
        import threading

        from minerwatch.config import load_config
        from minerwatch.poller import Poller
        from minerwatch.store import init_db

        path = self._fleet(4593, 4594)
        cfg = load_config(path)
        conn = init_db(":memory:")
        try:
            poller = Poller(cfg, conn, threading.Event(), dry_run=True, sleep_dry_run=True)
            for m in cfg[3].values():
                await poller._poll_one(m)
            # The watchdog did write a newer event for each miner. Which one
            # depends on its configured confirmation delay - `waiting_to_restart`
            # while the clock runs, `would_restart` once it expires - and the
            # point of the test is that *neither* reason may reach the WHY
            # section, since both describe the watchdog's decision rather than
            # the connection failure an operator needs to see.
            assert conn.execute(
                "SELECT COUNT(*) FROM events WHERE action IN "
                "('would_restart', 'waiting_to_restart')").fetchone()[0] > 0

            class A:
                config = path
            cmd_status = __import__("minerwatch.cli", fromlist=["cmd_status"]).cmd_status
            cmd_status(A(), cfg, conn)
            out = capsys.readouterr().out
            assert "would send restart" not in out
            assert "restart in" not in out
        finally:
            conn.close()

    async def test_a_healthy_fleet_prints_no_why_section(self, capsys):
        import threading

        from minerwatch.config import load_config
        from minerwatch.poller import Poller
        from minerwatch.store import init_db
        from sim.miner_sim import SimServer, SimState

        state = SimState(ghs=13500, seed=5)
        server = SimServer(("127.0.0.1", 0), state, control_file=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            path = write({**BASE,
                "groups": {"g": {"schedule": {"windows": [
                    {"days": ["mon","tue","wed","thu","fri","sat","sun"],
                     "ranges": ["00:00-24:00"]}]}}},
                "miners": [{"id": "ok", "host": "127.0.0.1",
                            "port": server.server_address[1], "group": "g"}]})
            cfg = load_config(path)
            conn = init_db(":memory:")
            try:
                poller = Poller(cfg, conn, threading.Event(), dry_run=True, sleep_dry_run=True)
                await poller._poll_one(list(cfg[3].values())[0])

                class A:
                    config = path
                cmd_status = __import__("minerwatch.cli", fromlist=["cmd_status"]).cmd_status
                cmd_status(A(), cfg, conn)
                assert "WHY:" not in capsys.readouterr().out
            finally:
                conn.close()
        finally:
            server.shutdown()
            server.server_close()

    async def test_the_id_column_widens_for_long_names(self, capsys):
        from minerwatch.config import load_config
        from minerwatch.store import init_db

        path = write({**BASE, "miners": [
            {"id": "denied-by-api-allow-01", "host": "10.0.0.1", "port": 4028}]})
        cfg = load_config(path)
        conn = init_db(":memory:")
        try:
            class A:
                config = path
            cmd_status = __import__("minerwatch.cli", fromlist=["cmd_status"]).cmd_status
            cmd_status(A(), cfg, conn)
            out = capsys.readouterr().out
            # The id must not run into the STATE column.
            line = [ln for ln in out.splitlines() if ln.startswith("denied-by-api-allow-01")][0]
            assert line.startswith("denied-by-api-allow-01 ")
        finally:
            conn.close()


class TestHashrateColumn:
    """The column that answers 'did the sleep actually work?'"""

    def test_formatting_across_scales(self):
        from minerwatch.cli import _fmt_hashrate
        from minerwatch.models import Event

        def ev(g):
            return Event(ts="t", miner="m", state="mining", ghs=g)

        assert _fmt_hashrate(ev(95170.14)) == "95.2 TH/s"   # a modern S19
        assert _fmt_hashrate(ev(13500.0)) == "13.5 TH/s"    # an S9
        assert _fmt_hashrate(ev(450.0)) == "450 GH/s"       # something small
        assert _fmt_hashrate(ev(0.0)) == "0"                # reported zero
        assert _fmt_hashrate(ev(None)) == "-"               # did not report
        assert _fmt_hashrate(None) == "-"                   # never polled

    def test_zero_and_unknown_are_not_the_same_display(self):
        """'stopped at zero' and 'never answered' must not look alike."""
        from minerwatch.cli import _fmt_hashrate
        from minerwatch.models import Event

        slept = Event(ts="t", miner="m", state="stopped", ghs=0.0)
        gone = Event(ts="t", miner="m", state="unreachable", ghs=None)
        assert _fmt_hashrate(slept) != _fmt_hashrate(gone)

    async def test_status_distinguishes_mining_slept_and_unreachable(self, capsys):
        import threading

        from minerwatch.config import load_config
        from minerwatch.poller import Poller
        from minerwatch.store import init_db
        from sim.miner_sim import SimServer, SimState

        running = SimState(ghs=95170, seed=3, stock=True)
        slept = SimState(ghs=95170, seed=4, stock=True)
        slept.sleep(slept.now())
        servers = []
        for st in (running, slept):
            srv = SimServer(("127.0.0.1", 0), st, control_file=None)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            servers.append(srv)
        try:
            path = write({**BASE,
                "groups": {"g": {"schedule": {"windows": [
                    {"days": ["mon","tue","wed","thu","fri","sat","sun"],
                     "ranges": ["00:00-24:00"]}]}}},
                "miners": [
                    {"id": "run", "host": "127.0.0.1", "port": servers[0].server_address[1], "group": "g"},
                    {"id": "slp", "host": "127.0.0.1", "port": servers[1].server_address[1], "group": "g"},
                    {"id": "off", "host": "127.0.0.1", "port": 4799, "group": "g"},
                ]})
            cfg = load_config(path)
            conn = init_db(":memory:")
            try:
                poller = Poller(cfg, conn, threading.Event(), dry_run=True, sleep_dry_run=True)
                for m in cfg[3].values():
                    await poller._poll_one(m)

                class A:
                    config = path
                cmd_status = __import__("minerwatch.cli", fromlist=["cmd_status"]).cmd_status
                cmd_status(A(), cfg, conn)
                out = capsys.readouterr().out
                lines = {ln.split()[0]: ln for ln in out.splitlines() if ln[:3] in ("run", "slp", "off")}
                assert "TH/s" in lines["run"]
                assert " 0 " in lines["slp"] and "TH/s" not in lines["slp"]
                assert " - " in lines["off"]
            finally:
                conn.close()
        finally:
            for srv in servers:
                srv.shutdown(); srv.server_close()
