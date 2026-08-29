import json
import os
import socket
import threading
import time

import pytest

from sim.miner_sim import (
    SimState,
    SimServer,
    ensure_control_file,
    read_control_file,
    write_control_file,
)


@pytest.fixture
def fake_clock():
    t = [1000.0]

    def now():
        return t[0]

    def advance(secs):
        t[0] += secs

    return now, advance


def query(host, port, command, use_null=True):
    payload = json.dumps({"command": command})
    if use_null:
        payload += "\x00"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((host, port))
    s.sendall(payload.encode("utf-8"))
    raw = s.recv(65536)
    s.close()
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    raw_str = raw.decode("utf-8").rstrip("\x00")
    return json.loads(raw_str)


def start_server(state, port=0, poll_secs=1.0, control_file=None, use_null=True):
    server = SimServer(("127.0.0.1", port), state, poll_secs=poll_secs, control_file=control_file, use_null_terminator=use_null)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    server._server_thread = t
    time.sleep(0.05)
    return server


class TestSimState:
    def test_initial_mining(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        assert st.state == "mining"
        assert st.ghs == 100
        assert st.elapsed == 0

    def test_tick_increases_elapsed(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.tick(now())
        advance(5)
        st.tick(now())
        assert st.elapsed >= 4

    def test_stopped_state(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.set_state("stopped", now())
        st.tick(now())
        assert st.state == "stopped"
        assert st.ghs_5s == 0.0
        assert st.get_status_string() == "Sick"

    def test_restart_transition(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, restart_secs=3, now=now)
        t0 = now()
        st.restart(t0)
        assert st.state == "restarting"
        assert st.ghs_5s == 0.0
        assert st.get_status_string() == "Restarting"
        advance(4)
        st.tick(now())
        assert st.state == "mining"
        assert st.ghs_5s > 0

    def test_summary_alive(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        s = st.summary()
        assert s["Status"] == "Alive"
        assert s["GHS 5s"] > 0
        assert s["Accepted"] >= 1000

    def test_summary_stopped(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.set_state("stopped", now())
        s = st.summary()
        assert s["Status"] == "Sick"
        assert s["GHS 5s"] == 0.0

    def test_stats_temps_fans(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        s = st.stats()
        assert len(s["temps"]) == 3
        assert len(s["fans"]) == 2
        assert s["temp1"] > 50
        assert s["fan1"] > 1000

    def test_stats_idle(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.set_state("stopped", now())
        s = st.stats()
        for t in s["temps"]:
            assert t == 30.0
        for f in s["fans"]:
            assert f == 0

    def test_maybe_apply_control_newer_seq(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.maybe_apply_control({"state": "stopped"})
        assert st.state == "stopped"

    def test_maybe_apply_control_mining(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.set_state("stopped", now())
        st.maybe_apply_control({"state": "mining"})
        assert st.state == "mining"
        assert st.ghs_5s > 0

    def test_maybe_apply_control_invalid_state(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.maybe_apply_control({"state": "bogus"})
        assert st.state == "mining"

    def test_maybe_apply_control_ghs(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.maybe_apply_control({"ghs_5s": 999})
        assert st.ghs_5s == 999
        assert st.ghs == 999

    def test_maybe_apply_control_temps_fans(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.maybe_apply_control({"temps": [50, 51, 52], "fans": [3000, 3100]})
        assert st._temps == [50, 51, 52]
        assert st._fans == [3000.0, 3100.0]

    def test_control_file_write_read(self, fake_clock, tmp_path):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        cf = str(tmp_path / "state-test.json")
        write_control_file(cf, st)
        assert os.path.exists(cf)
        with open(cf) as f:
            data = json.load(f)
        assert data["state"] == "mining"
        assert data["ghs_5s"] > 0

    def test_control_file_read_applies(self, fake_clock, tmp_path):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        cf = str(tmp_path / "state-test.json")
        with open(cf, "w") as f:
            json.dump({"state": "stopped", "seq": 10}, f)
        read_control_file(cf, st)
        assert st.state == "stopped"

    def test_ensure_control_file_creates(self, fake_clock, tmp_path):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        cf = str(tmp_path / "state-new.json")
        path = ensure_control_file(cf, st)
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["state"] == "mining"


class TestServer:
    def test_summary_valid_json(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st, port=0)
        port = server.server_address[1]
        try:
            resp = query("127.0.0.1", port, "summary")
            assert "STATUS" in resp
            assert resp["STATUS"][0]["STATUS"] == "S"
            assert "SUMMARY" in resp
            s = resp["SUMMARY"][0]
            assert s["Status"] == "Alive"
            assert s["GHS 5s"] > 0
        finally:
            server.shutdown()

    def test_elapsed_increasing(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st, port=0)
        port = server.server_address[1]
        try:
            advance(2)
            resp1 = query("127.0.0.1", port, "summary")
            e1 = resp1["SUMMARY"][0]["Elapsed"]
            advance(3)
            resp2 = query("127.0.0.1", port, "summary")
            e2 = resp2["SUMMARY"][0]["Elapsed"]
            assert e2 > e1
        finally:
            server.shutdown()

    def test_stats_has_temps_fans(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st, port=0)
        port = server.server_address[1]
        try:
            resp = query("127.0.0.1", port, "stats")
            assert "STATS" in resp
            s = resp["STATS"][0]
            assert "temps" in s
            assert "fans" in s
            assert len(s["temps"]) == 3
            assert len(s["fans"]) == 2
        finally:
            server.shutdown()

    def test_stopped_ghs_zero(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.set_state("stopped", now())
        server = start_server(st, port=0)
        port = server.server_address[1]
        try:
            resp = query("127.0.0.1", port, "summary")
            s = resp["SUMMARY"][0]
            assert s["GHS 5s"] == 0.0
            assert s["Status"] != "Alive"
        finally:
            server.shutdown()

    def test_restart_lifecycle(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, restart_secs=3, now=now)
        server = start_server(st, port=0)
        port = server.server_address[1]
        try:
            resp0 = query("127.0.0.1", port, "summary")
            assert resp0["SUMMARY"][0]["GHS 5s"] > 0

            resp_r = query("127.0.0.1", port, "restart")
            assert resp_r["SUMMARY"][0]["GHS 5s"] == 0.0
            assert resp_r["STATUS"][0]["STATUS"] == "S"

            resp1 = query("127.0.0.1", port, "summary")
            assert resp1["SUMMARY"][0]["GHS 5s"] == 0.0

            advance(5)
            st.tick(now())

            resp2 = query("127.0.0.1", port, "summary")
            assert resp2["SUMMARY"][0]["GHS 5s"] > 0
        finally:
            server.shutdown()

    def test_restart_elapsed_reset(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, restart_secs=2, now=now)
        server = start_server(st, port=0)
        port = server.server_address[1]
        try:
            advance(5)
            st.tick(now())
            resp_pre = query("127.0.0.1", port, "summary")
            elapsed_pre = resp_pre["SUMMARY"][0]["Elapsed"]
            assert elapsed_pre >= 4

            query("127.0.0.1", port, "restart")
            advance(3)
            st.tick(now())

            resp_post = query("127.0.0.1", port, "summary")
            assert resp_post["SUMMARY"][0]["Elapsed"] < elapsed_pre
        finally:
            server.shutdown()

    def test_control_file_forces_stopped(self, fake_clock, tmp_path):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        cf = str(tmp_path / "state-ctrl.json")
        write_control_file(cf, st)
        server = start_server(st, port=0, poll_secs=0.01, control_file=cf)
        port = server.server_address[1]
        try:
            resp = query("127.0.0.1", port, "summary")
            assert resp["SUMMARY"][0]["Status"] == "Alive"

            with open(cf, "w") as f:
                json.dump({"state": "stopped", "seq": 99}, f)

            advance(0.1)
            st.tick(now())

            resp2 = query("127.0.0.1", port, "summary")
            assert resp2["SUMMARY"][0]["Status"] == "Sick"

            with open(cf, "w") as f:
                json.dump({"state": "mining", "seq": 100}, f)

            advance(0.1)
            st.tick(now())

            resp3 = query("127.0.0.1", port, "summary")
            assert resp3["SUMMARY"][0]["Status"] == "Alive"
        finally:
            server.shutdown()

    def test_control_file_stopped_ghs_zero(self, fake_clock, tmp_path):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        cf = str(tmp_path / "state-ghszero.json")
        write_control_file(cf, st)
        server = start_server(st, port=0, poll_secs=0.01, control_file=cf)
        port = server.server_address[1]
        try:
            resp = query("127.0.0.1", port, "summary")
            assert resp["SUMMARY"][0]["GHS 5s"] > 0

            # Manual edit — no seq field, just state
            with open(cf, "w") as f:
                json.dump({"state": "stopped"}, f)

            advance(0.1)
            st.tick(now())

            resp2 = query("127.0.0.1", port, "summary")
            assert resp2["SUMMARY"][0]["GHS 5s"] == 0.0
            assert resp2["SUMMARY"][0]["Status"] == "Sick"
        finally:
            server.shutdown()

    def test_control_file_ghs_override(self, fake_clock, tmp_path):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        cf = str(tmp_path / "state-ghs.json")
        write_control_file(cf, st)
        server = start_server(st, port=0, poll_secs=0.01, control_file=cf)
        port = server.server_address[1]
        try:
            with open(cf, "w") as f:
                json.dump({"ghs_5s": 5000, "seq": 50}, f)

            advance(0.1)
            st.tick(now())

            resp = query("127.0.0.1", port, "summary")
            assert abs(resp["SUMMARY"][0]["GHS 5s"] - 5000) < 500
        finally:
            server.shutdown()

    def test_malformed_request(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st, port=0)
        port = server.server_address[1]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("127.0.0.1", port))
            s.sendall(b"not-json\x00")
            raw = s.recv(65536)
            s.close()

            if raw.endswith(b"\x00"):
                raw = raw[:-1]
            resp = json.loads(raw.decode("utf-8").rstrip("\x00"))
            assert resp["STATUS"][0]["STATUS"] == "E"

            resp2 = query("127.0.0.1", port, "summary")
            assert resp2["STATUS"][0]["STATUS"] == "S"
        finally:
            server.shutdown()

    def test_unknown_command(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st, port=0)
        port = server.server_address[1]
        try:
            resp = query("127.0.0.1", port, "nonexistent")
            assert resp["STATUS"][0]["STATUS"] == "E"

            resp2 = query("127.0.0.1", port, "summary")
            assert resp2["STATUS"][0]["STATUS"] == "S"
        finally:
            server.shutdown()

    def test_null_terminator_present(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st, port=0, use_null=True)
        port = server.server_address[1]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("127.0.0.1", port))
            s.sendall(b'{"command":"summary"}\x00')
            raw = s.recv(65536)
            s.close()
            assert raw.endswith(b"\x00")
        finally:
            server.shutdown()

    def test_no_null_terminator(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st, port=0, use_null=False)
        port = server.server_address[1]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("127.0.0.1", port))
            s.sendall(b'{"command":"summary"}')
            raw = s.recv(65536)
            s.close()
            assert not raw.endswith(b"\x00")
        finally:
            server.shutdown()

    def test_two_ports_two_state_files(self, fake_clock, tmp_path):
        now, advance = fake_clock
        cf1 = str(tmp_path / "state-9001.json")
        cf2 = str(tmp_path / "state-9002.json")

        st1 = SimState(ghs=100, now=now)
        st2 = SimState(ghs=200, now=now)

        server1 = start_server(st1, port=0, control_file=cf1)
        server2 = start_server(st2, port=0, control_file=cf2)
        port1 = server1.server_address[1]
        port2 = server2.server_address[1]
        try:
            write_control_file(cf1, st1)
            write_control_file(cf2, st2)

            resp1 = query("127.0.0.1", port1, "summary")
            resp2 = query("127.0.0.1", port2, "summary")
            assert resp1["SUMMARY"][0]["GHS av"] == 100
            assert resp2["SUMMARY"][0]["GHS av"] == 200

            with open(cf1, "w") as f:
                json.dump({"state": "stopped", "seq": 10}, f)
            with open(cf2, "w") as f:
                json.dump({"state": "stopped", "seq": 10}, f)

            advance(0.1)
            st1.tick(now())
            st2.tick(now())

            resp1b = query("127.0.0.1", port1, "summary")
            resp2b = query("127.0.0.1", port2, "summary")
            assert resp1b["SUMMARY"][0]["Status"] == "Sick"
            assert resp2b["SUMMARY"][0]["Status"] == "Sick"
        finally:
            server1.shutdown()
            server2.shutdown()


# ---------------------------------------------------------------------------
# Software sleep support
# ---------------------------------------------------------------------------

def query_cmd(host, port, command, parameter=None, use_null=True):
    """Send a command with an optional parameter and return the parsed reply."""
    payload = {"command": command}
    if parameter is not None:
        payload["parameter"] = parameter
    text = json.dumps(payload)
    if use_null:
        text += "\x00"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((host, port))
    s.sendall(text.encode("utf-8"))
    raw = b""
    while b"\x00" not in raw:
        chunk = s.recv(65536)
        if not chunk:
            break
        raw += chunk
    s.close()
    return json.loads(raw.decode("utf-8").rstrip("\x00"))


class TestSimSleepState:
    def test_sleep_zeroes_hashrate_and_reports_sleeping(self, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        st.sleep(now())
        assert st.state == "sleeping"
        assert st.ghs_5s == 0.0
        assert st.get_status_string() == "Sleeping"
        assert st.summary()["GHS 5s"] == 0.0

    def test_wake_resumes_hashing(self, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        st.sleep(now())
        st.wake(now())
        assert st.state == "mining"
        assert st.ghs_5s > 0

    def test_sleep_is_idempotent(self, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        st.sleep(now())
        st.sleep(now())
        assert st.state == "sleeping"

    def test_sleeping_miner_stays_asleep_across_ticks(self, fake_clock):
        now, advance = fake_clock
        st = SimState(ghs=100, now=now)
        st.sleep(now())
        advance(600)
        st.tick(now())
        # Unlike RESTARTING, sleep has no timeout: it persists until woken.
        assert st.state == "sleeping"
        assert st.ghs_5s == 0.0

    def test_sleeping_miner_is_still_powered(self, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        st.sleep(now())
        stats = st.stats()
        # Fans still turning distinguishes a slept miner from a dead one.
        assert stats["fan1"] > 0
        assert stats["miner-mode"] == 1

    def test_control_file_can_force_sleeping(self, tmp_path, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        path = str(tmp_path / "state.json")
        write_control_file(path, st)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"state": "sleeping"}, f)
        read_control_file(path, st)
        assert st.state == "sleeping"


class TestSimSleepCommands:
    def test_ascset_sleep_and_wake(self, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st)
        try:
            port = server.server_address[1]
            resp = query_cmd("127.0.0.1", port, "ascset", "0,sleep")
            assert resp["STATUS"][0]["STATUS"] == "I"
            assert st.state == "sleeping"

            resp = query_cmd("127.0.0.1", port, "ascset", "0,wake")
            assert resp["STATUS"][0]["STATUS"] == "I"
            assert st.state == "mining"
        finally:
            server.shutdown()
            server.server_close()

    def test_pause_and_resume(self, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st)
        try:
            port = server.server_address[1]
            assert query_cmd("127.0.0.1", port, "pause")["STATUS"][0]["STATUS"] == "S"
            assert st.state == "sleeping"
            assert query_cmd("127.0.0.1", port, "resume")["STATUS"][0]["STATUS"] == "S"
            assert st.state == "mining"
        finally:
            server.shutdown()
            server.server_close()

    def test_unknown_ascset_option_is_an_error_not_a_crash(self, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st)
        try:
            port = server.server_address[1]
            resp = query_cmd("127.0.0.1", port, "ascset", "0,turbo")
            assert resp["STATUS"][0]["STATUS"] == "E"
            assert st.state == "mining"
        finally:
            server.shutdown()
            server.server_close()

    def test_malformed_ascset_parameter_is_rejected(self, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        server = start_server(st)
        try:
            port = server.server_address[1]
            resp = query_cmd("127.0.0.1", port, "ascset", "garbage")
            assert resp["STATUS"][0]["STATUS"] == "E"
        finally:
            server.shutdown()
            server.server_close()

    def test_summary_of_a_slept_miner_survives_a_restart_command(self, fake_clock):
        """restart wins over sleep; it is a stronger action."""
        now, _ = fake_clock
        st = SimState(ghs=100, restart_secs=1, now=now)
        server = start_server(st)
        try:
            port = server.server_address[1]
            query_cmd("127.0.0.1", port, "ascset", "0,sleep")
            query_cmd("127.0.0.1", port, "restart")
            assert st.state == "restarting"
        finally:
            server.shutdown()
            server.server_close()


class TestControlFileRobustness:
    def test_change_is_detected_even_with_an_identical_mtime(self, tmp_path, fake_clock):
        """Content-based detection, not mtime.

        Coarse filesystem timestamps (2s on FAT/exFAT, and NTFS under rapid
        writes) let two different edits share an mtime, which used to make a
        control-file change silently fail to apply.
        """
        import os

        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        path = str(tmp_path / "state.json")
        write_control_file(path, st)
        stamp = os.stat(path)

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"state": "stopped"}, f)
        os.utime(path, (stamp.st_atime, stamp.st_mtime))  # forge the old mtime

        read_control_file(path, st)
        assert st.state == "stopped"

    def test_unchanged_content_is_not_reapplied(self, tmp_path, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        path = str(tmp_path / "state.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"state": "stopped"}, f)
        assert read_control_file(path, st) is not None
        assert read_control_file(path, st) is None

    def test_unparseable_content_is_not_cached(self, tmp_path, fake_clock):
        """A half-written file must be retried, not remembered as seen."""
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        path = str(tmp_path / "state.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"state": "stop')  # truncated
        assert read_control_file(path, st) is None
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"state": "stopped"}, f)
        assert read_control_file(path, st) is not None
        assert st.state == "stopped"

    def test_written_file_is_utf8_with_lf(self, tmp_path, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        path = str(tmp_path / "state.json")
        write_control_file(path, st)
        with open(path, "rb") as f:
            raw = f.read()
        assert b"\r\n" not in raw
        json.loads(raw.decode("utf-8"))

    def test_missing_file_is_ignored(self, tmp_path, fake_clock):
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        assert read_control_file(str(tmp_path / "absent.json"), st) is None
        assert st.state == "mining"


class TestServerPlatformSafety:
    def test_state_is_available_by_the_time_the_socket_is_bound(self, fake_clock):
        """A handler must never find a half-built server.

        Checked at bind time, not after __init__ returns: the socket starts
        accepting during activation, so attributes assigned *after*
        super().__init__() would be missing for the first connection. Reading
        them afterwards would pass either way.
        """
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)
        observed = {}

        class Probe(SimServer):
            def server_bind(self):
                observed["state"] = getattr(self, "state", None)
                observed["sim_server"] = getattr(self, "sim_server", None)
                observed["use_null"] = getattr(self, "use_null_terminator", None)
                super().server_bind()

        server = Probe(("127.0.0.1", 0), st)
        try:
            assert observed["state"] is st
            assert observed["sim_server"] is server
            assert observed["use_null"] is True
        finally:
            server.server_close()


class TestSimStartupFlags:
    def test_an_explicit_start_state_beats_a_stale_control_file(self, tmp_path, fake_clock):
        """--stopped / --sleeping must not be undone by a leftover file.

        control_file_path() defaults to a fixed name per port, so a second run
        finds the previous run's file and used to adopt it on the first client
        request - which looks exactly like the flag being ignored.
        """
        now, _ = fake_clock
        path = str(tmp_path / "state.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"state": "mining"}, f)   # leftover from a previous run

        st = SimState(ghs=100, now=now)
        st.set_state("sleeping", now())
        ensure_control_file(path, st, force=True)

        read_control_file(path, st)
        assert st.state == "sleeping"

    def test_without_an_explicit_flag_an_existing_file_is_still_honoured(self, tmp_path, fake_clock):
        now, _ = fake_clock
        path = str(tmp_path / "state.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"state": "stopped"}, f)

        st = SimState(ghs=100, now=now)
        ensure_control_file(path, st)           # force=False: leave it alone
        read_control_file(path, st)
        assert st.state == "stopped"

    def test_an_unwritable_control_file_does_not_abort_the_simulator(self, tmp_path, fake_clock, monkeypatch):
        """On Windows the write can be refused simply because a scanner has it open."""
        now, _ = fake_clock
        st = SimState(ghs=100, now=now)

        def refuse(*args, **kwargs):
            raise PermissionError(32, "locked")

        monkeypatch.setattr("sim.miner_sim.write_text_atomic", refuse)
        # Must not raise: the simulator is perfectly usable without the file.
        ensure_control_file(str(tmp_path / "state.json"), st, force=True)
