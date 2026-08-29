"""Tests for the software power-control drivers.

Both drivers are exercised against the project's simulators rather than mocks,
so the wire format — cgminer's NUL framing and Bitmain's digest-authenticated
CGI — is actually covered.
"""

import asyncio
import json
import threading

import pytest

from minerwatch.backends import (
    BitmainHttpBackend,
    CgminerBackend,
    NullBackend,
    get_backend,
)
from minerwatch.models import Command, Miner, SleepBackend, SleepConfig
from sim.bitmain_http_sim import BitmainHttpServer, MinerConf
from sim.miner_sim import SimServer, SimState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tcp_sim():
    """A running cgminer simulator; yields (state, port)."""
    state = SimState(ghs=13500, seed=1)
    server = SimServer(("127.0.0.1", 0), state, control_file=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def http_sim():
    """A running Bitmain web-UI simulator; yields (conf, port)."""
    conf = MinerConf()
    server = BitmainHttpServer(("127.0.0.1", 0), conf=conf, username="root", password="root")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield conf, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def cgminer_miner(port, **overrides):
    defaults = dict(enabled=True, backend=SleepBackend.CGMINER, timeout_seconds=5)
    cfg = SleepConfig(**{**defaults, **overrides})
    return Miner(id="m1", host="127.0.0.1", port=port, sleep=cfg)


def http_miner(port, **overrides):
    defaults = dict(
        enabled=True,
        backend=SleepBackend.BITMAIN_HTTP,
        http_port=port,
        username="root",
        password="root",
        timeout_seconds=5,
    )
    cfg = SleepConfig(**{**defaults, **overrides})
    return Miner(id="m1", host="127.0.0.1", port=4028, sleep=cfg)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

class TestGetBackend:
    def test_disabled_config_yields_null_backend(self):
        assert isinstance(get_backend(SleepConfig(enabled=False)), NullBackend)

    def test_named_backends(self):
        assert isinstance(
            get_backend(SleepConfig(enabled=True, backend=SleepBackend.CGMINER)), CgminerBackend
        )
        assert isinstance(
            get_backend(SleepConfig(enabled=True, backend=SleepBackend.BITMAIN_HTTP)),
            BitmainHttpBackend,
        )

    async def test_null_backend_reports_why_it_did_nothing(self):
        miner = Miner(id="m", host="127.0.0.1", port=1)
        ok, detail = await NullBackend().sleep(miner)
        assert not ok and "no sleep backend" in detail


# ---------------------------------------------------------------------------
# cgminer backend
# ---------------------------------------------------------------------------

class TestCgminerBackend:
    async def test_sleep_then_wake_round_trip(self, tcp_sim):
        state, port = tcp_sim
        miner = cgminer_miner(port)
        backend = CgminerBackend()

        ok, detail = await backend.sleep(miner)
        assert ok, detail
        assert state.state == SimState.SLEEPING
        assert state.ghs_5s == 0.0

        ok, detail = await backend.wake(miner)
        assert ok, detail
        assert state.state == SimState.MINING
        assert state.ghs_5s > 0

    async def test_first_command_wins_and_is_reported(self, tcp_sim):
        _, port = tcp_sim
        miner = cgminer_miner(port)
        ok, detail = await CgminerBackend().sleep(miner)
        assert ok and "ascset:0,sleep" in detail

    async def test_falls_through_to_the_next_command_on_rejection(self, tcp_sim):
        state, port = tcp_sim
        # "nonsense" is rejected with STATUS E; "pause" then succeeds.
        miner = cgminer_miner(
            port, sleep_commands=(Command("nonsense"), Command("pause"))
        )
        ok, detail = await CgminerBackend().sleep(miner)
        assert ok, detail
        assert "pause" in detail
        assert state.state == SimState.SLEEPING

    async def test_all_commands_rejected_reports_every_failure(self, tcp_sim):
        _, port = tcp_sim
        miner = cgminer_miner(port, sleep_commands=(Command("bogus1"), Command("bogus2")))
        ok, detail = await CgminerBackend().sleep(miner)
        assert not ok
        assert "bogus1" in detail and "bogus2" in detail

    async def test_transport_failure_abandons_the_chain(self, tcp_sim):
        _, port = tcp_sim
        # Port 1 is not listening: the very first attempt fails at connect and
        # there is no point trying the rest.
        miner = cgminer_miner(port, api_port=1, timeout_seconds=1)
        ok, detail = await CgminerBackend().sleep(miner)
        assert not ok
        assert "all sleep commands rejected" not in detail

    async def test_empty_command_list_is_reported(self, tcp_sim):
        _, port = tcp_sim
        miner = cgminer_miner(port, wake_commands=())
        ok, detail = await CgminerBackend().wake(miner)
        assert not ok and "no wake commands configured" in detail

    async def test_api_port_overrides_the_poll_port(self, tcp_sim):
        state, port = tcp_sim
        # Poll port is wrong on purpose; api_port is what must be dialled.
        miner = Miner(
            id="m1",
            host="127.0.0.1",
            port=1,
            sleep=SleepConfig(
                enabled=True, backend=SleepBackend.CGMINER, api_port=port, timeout_seconds=5
            ),
        )
        ok, detail = await CgminerBackend().sleep(miner)
        assert ok, detail
        assert state.state == SimState.SLEEPING

    async def test_sleeping_miner_still_answers_summary(self, tcp_sim):
        """Software sleep must not take the control plane away."""
        state, port = tcp_sim
        miner = cgminer_miner(port)
        await CgminerBackend().sleep(miner)

        from minerwatch import api
        from minerwatch.poller import classify

        raw = await api.request("127.0.0.1", port, "summary")
        assert classify(raw)[0].value == "stopped"
        assert json.loads(raw)["SUMMARY"][0]["Status"] == "Sleeping"


# ---------------------------------------------------------------------------
# Bitmain HTTP backend
# ---------------------------------------------------------------------------

class TestBitmainHttpBackend:
    async def test_sleep_sets_miner_mode_to_one(self, http_sim):
        conf, port = http_sim
        ok, detail = await BitmainHttpBackend().sleep(http_miner(port))
        assert ok, detail
        assert conf.miner_mode == 1

    async def test_wake_sets_miner_mode_back_to_zero(self, http_sim):
        conf, port = http_sim
        miner = http_miner(port)
        await BitmainHttpBackend().sleep(miner)
        ok, detail = await BitmainHttpBackend().wake(miner)
        assert ok, detail
        assert conf.miner_mode == 0

    async def test_rest_of_the_configuration_is_preserved(self, http_sim):
        """Only miner-mode may change: the POST replaces the whole document."""
        conf, port = http_sim
        before = conf.get()
        await BitmainHttpBackend().sleep(http_miner(port))
        after = conf.get()
        assert after.pop("miner-mode") == 1
        before.pop("miner-mode")
        assert after == before

    async def test_already_in_mode_is_success_without_a_write(self, http_sim):
        conf, port = http_sim
        miner = http_miner(port)
        ok, _ = await BitmainHttpBackend().sleep(miner)
        assert ok
        writes = len(conf.writes)
        ok, detail = await BitmainHttpBackend().sleep(miner)
        # Re-sleeping an already-sleeping miner must not reboot bmminer again.
        assert ok and "already" in detail
        assert len(conf.writes) == writes

    async def test_bad_credentials_fail_cleanly(self, http_sim):
        _, port = http_sim
        miner = http_miner(port, password="wrong")
        ok, detail = await BitmainHttpBackend().sleep(miner)
        assert not ok
        assert "401" in detail or "authorization" in detail.lower()

    async def test_unreachable_host_fails_cleanly(self):
        miner = http_miner(1, timeout_seconds=1)
        ok, detail = await BitmainHttpBackend().sleep(miner)
        assert not ok
        # Name the operation that failed, so the event log says which call
        # could not reach the miner rather than just "sleep".
        assert "GET miner conf failed" in detail

    async def test_timeout_names_the_budget_and_warns_it_may_still_land(self, monkeypatch):
        """A thread cannot be cancelled, so the caller must be told.

        ``asyncio.to_thread`` is replaced with a plain coroutine so the timeout
        can be exercised without leaving a real 30-second worker behind for the
        executor to join at interpreter shutdown.
        """
        miner = http_miner(80, timeout_seconds=0.01)

        async def never_finishes(fn, *args, **kwargs):
            await asyncio.sleep(30)

        monkeypatch.setattr(asyncio, "to_thread", never_finishes)
        ok, detail = await BitmainHttpBackend().sleep(miner)
        assert not ok
        assert "may still complete" in detail
        assert "no reply from http://127.0.0.1:80" in detail

    async def test_never_raises_on_garbage_response(self, monkeypatch, http_sim):
        _, port = http_sim
        miner = http_miner(port)

        def boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(BitmainHttpBackend, "_set_mode_blocking", boom)
        ok, detail = await BitmainHttpBackend().sleep(miner)
        assert not ok and "kaboom" in detail

    def test_ipv6_host_is_bracketed(self):
        miner = Miner(
            id="m",
            host="fe80::1",
            port=4028,
            sleep=SleepConfig(enabled=True, backend=SleepBackend.BITMAIN_HTTP, http_port=80),
        )
        assert BitmainHttpBackend()._base_url(miner) == "http://[fe80::1]:80"


class TestDigestSimulator:
    """The simulator's own auth must actually reject bad credentials."""

    def test_unauthenticated_request_is_challenged(self, http_sim):
        import urllib.error
        import urllib.request

        _, port = http_sim
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/cgi-bin/get_miner_conf.cgi", timeout=5
            )
        assert exc.value.code == 401
        assert "Digest" in exc.value.headers.get("WWW-Authenticate", "")


class TestLinkedSimulators:
    """The web-UI simulator can drive a TCP simulator, as one machine would.

    On a real S19 the web UI and the cgminer API are two faces of the same
    miner: setting miner-mode in the UI stops the hashrate the API reports. Two
    unlinked simulator processes do not do that, which makes an end-to-end
    bitmain_http demo look like a failure - MinerWatch sets the mode, keeps
    seeing full hashrate, and correctly concludes the sleep never took effect.
    """

    def test_setting_sleep_mode_writes_the_linked_control_file(self, tmp_path):
        import json as _json

        from sim.miner_sim import SimState, read_control_file

        control = tmp_path / "state-4103.json"
        conf = MinerConf(control_file=str(control))
        assert _json.loads(control.read_text(encoding="utf-8"))["state"] == "mining"

        conf.set({**conf.get(), "miner-mode": 1})
        assert _json.loads(control.read_text(encoding="utf-8"))["state"] == "sleeping"

        # ...and the TCP simulator actually adopts it.
        state = SimState(ghs=100, now=lambda: 0.0)
        read_control_file(str(control), state)
        assert state.state == "sleeping"
        assert state.ghs_5s == 0.0

    def test_returning_to_normal_mode_wakes_the_linked_miner(self, tmp_path):
        from sim.miner_sim import SimState, read_control_file

        control = tmp_path / "state.json"
        conf = MinerConf(control_file=str(control))
        conf.set({**conf.get(), "miner-mode": 1})
        conf.set({**conf.get(), "miner-mode": 0})

        state = SimState(ghs=100, now=lambda: 0.0)
        state.sleep(0.0)
        read_control_file(str(control), state)
        assert state.state == "mining"

    def test_an_unrelated_config_change_does_not_touch_the_link(self, tmp_path):
        control = tmp_path / "state.json"
        conf = MinerConf(control_file=str(control))
        before = control.read_text(encoding="utf-8")
        conf.set({**conf.get(), "freq-level": "80"})
        assert control.read_text(encoding="utf-8") == before

    def test_without_a_link_nothing_is_written(self, tmp_path):
        conf = MinerConf()
        conf.set({**conf.get(), "miner-mode": 1})
        assert list(tmp_path.iterdir()) == []

    async def test_end_to_end_sleep_stops_the_linked_miner(self, tmp_path):
        """The whole point: the backend's HTTP call zeroes the TCP hashrate."""
        import threading

        from minerwatch import api
        from minerwatch.poller import classify
        from sim.bitmain_http_sim import BitmainHttpServer
        from sim.miner_sim import SimServer, SimState, control_file_path

        state = SimState(ghs=13500, seed=3)
        tcp = SimServer(("127.0.0.1", 0), state, control_file=None)
        tcp_port = tcp.server_address[1]
        control = str(tmp_path / f"state-{tcp_port}.json")
        tcp.control_file = control

        conf = MinerConf(control_file=control)
        http = BitmainHttpServer(("127.0.0.1", 0), conf=conf)
        threading.Thread(target=tcp.serve_forever, daemon=True).start()
        threading.Thread(target=http.serve_forever, daemon=True).start()
        try:
            miner = http_miner(http.server_address[1])
            miner.port = tcp_port
            ok, detail = await BitmainHttpBackend().sleep(miner)
            assert ok, detail

            raw = await api.request("127.0.0.1", tcp_port, "summary")
            assert classify(raw)[0].value == "stopped"
        finally:
            tcp.shutdown(); tcp.server_close()
            http.shutdown(); http.server_close()


class TestProbe:
    """Read-only backend checks.

    A dry run never contacts the backend at all, so a fleet can rehearse
    cleanly for weeks and still fail the first time it goes live, on a web-UI
    password nobody ever tested. probe() closes that gap without changing
    anything on the miner.
    """

    async def test_cgminer_probe_reports_the_firmware(self, tcp_sim):
        _, port = tcp_sim
        ok, detail = await CgminerBackend().probe(cgminer_miner(port))
        assert ok, detail
        assert "API reachable" in detail
        assert "Antminer Simulator" in detail

    async def test_cgminer_probe_changes_nothing(self, tcp_sim):
        state, port = tcp_sim
        before = state.state
        await CgminerBackend().probe(cgminer_miner(port))
        assert state.state == before

    async def test_cgminer_probe_on_a_dead_port(self):
        miner = cgminer_miner(1, timeout_seconds=1)
        ok, detail = await CgminerBackend().probe(miner)
        assert not ok and "127.0.0.1:1" in detail

    async def test_http_probe_reports_the_current_mode(self, http_sim):
        conf, port = http_sim
        ok, detail = await BitmainHttpBackend().probe(http_miner(port))
        assert ok, detail
        assert "authenticated" in detail
        assert "miner-mode=0 (normal)" in detail

    async def test_http_probe_reflects_a_sleeping_miner(self, http_sim):
        conf, port = http_sim
        conf.set({**conf.get(), "miner-mode": 1})
        ok, detail = await BitmainHttpBackend().probe(http_miner(port))
        assert ok and "SLEEPING" in detail

    async def test_http_probe_never_writes(self, http_sim):
        conf, port = http_sim
        writes = len(conf.writes)
        await BitmainHttpBackend().probe(http_miner(port))
        assert len(conf.writes) == writes

    async def test_a_wrong_password_says_so_in_those_words(self, http_sim):
        """The failure this command exists to surface before a window closes."""
        _, port = http_sim
        ok, detail = await BitmainHttpBackend().probe(http_miner(port, password="wrong"))
        assert not ok
        assert "username or password is wrong" in detail
        assert "root" in detail

    async def test_an_unreachable_web_ui(self):
        ok, detail = await BitmainHttpBackend().probe(http_miner(9, timeout_seconds=1))
        assert not ok

    async def test_a_reply_that_is_not_the_miner_ui(self, http_sim):
        """Something answers, but it is not a miner."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Plain(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html>hello</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Plain)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            ok, detail = await BitmainHttpBackend().probe(http_miner(srv.server_address[1]))
            assert not ok and "not JSON" in detail
        finally:
            srv.shutdown()
            srv.server_close()

    async def test_the_null_backend_is_skipped(self):
        miner = Miner(id="m", host="127.0.0.1", port=1)
        ok, detail = await NullBackend().probe(miner)
        assert not ok and "no sleep backend" in detail


class TestModeKeyDiscovery:
    """Bitmain renamed the power-mode field between firmware generations.

    From the field: two S19-class miners authenticated fine but their config
    had no 'miner-mode' at all, so a hardcoded key would have failed at the
    first window boundary with nothing to act on.
    """

    def _serve(self, doc):
        import threading

        conf = MinerConf(doc)
        srv = BitmainHttpServer(("127.0.0.1", 0), conf=conf)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return conf, srv

    def _miner(self, port, **kw):
        d = dict(enabled=True, backend=SleepBackend.BITMAIN_HTTP, http_port=port,
                 username="root", password="root", timeout_seconds=5)
        return Miner(id="m", host="127.0.0.1", port=4028, sleep=SleepConfig(**{**d, **kw}))

    BASE = {"pools": [], "api-listen": True, "freq-level": "100"}

    @pytest.mark.parametrize("key", ["miner-mode", "bitmain-work-mode", "work-mode", "miner_mode"])
    async def test_each_known_field_name_is_found(self, key):
        conf, srv = self._serve({**self.BASE, key: 0})
        try:
            miner = self._miner(srv.server_address[1])
            ok, detail = await BitmainHttpBackend().probe(miner)
            assert ok, detail
            assert f"{key}=0" in detail

            ok, _ = await BitmainHttpBackend().sleep(miner)
            assert ok
            assert conf.get()[key] == 1
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_string_valued_field_stays_a_string(self):
        """Some CGI handlers silently ignore an int where they expect a string."""
        conf, srv = self._serve({**self.BASE, "bitmain-work-mode": "0"})
        try:
            miner = self._miner(srv.server_address[1])
            await BitmainHttpBackend().sleep(miner)
            assert conf.get()["bitmain-work-mode"] == "1"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_an_unknown_field_lists_what_the_firmware_does_expose(self):
        """The only way to work out what to point mode_key at."""
        conf, srv = self._serve({**self.BASE, "power-mode": 0, "bitmain-nobeeper": "false"})
        try:
            ok, detail = await BitmainHttpBackend().probe(self._miner(srv.server_address[1]))
            assert not ok
            assert "Fields present:" in detail
            assert "power-mode" in detail and "bitmain-nobeeper" in detail
            assert "sleep.mode_key" in detail
        finally:
            srv.shutdown(); srv.server_close()

    async def test_an_explicit_mode_key_overrides_discovery(self):
        conf, srv = self._serve({**self.BASE, "power-mode": 0})
        try:
            miner = self._miner(srv.server_address[1], mode_key="power-mode")
            ok, detail = await BitmainHttpBackend().probe(miner)
            assert ok and "power-mode=0" in detail
            await BitmainHttpBackend().sleep(miner)
            assert conf.get()["power-mode"] == 1
        finally:
            srv.shutdown(); srv.server_close()

    async def test_an_override_naming_a_missing_field_fails_clearly(self):
        conf, srv = self._serve({**self.BASE, "miner-mode": 0})
        try:
            miner = self._miner(srv.server_address[1], mode_key="not-there")
            ok, detail = await BitmainHttpBackend().probe(miner)
            assert not ok, "an explicit override must not silently fall back to discovery"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_sleep_reports_the_missing_field_too(self):
        """Not just probe: the actuation path must say the same thing."""
        conf, srv = self._serve({**self.BASE, "power-mode": 0})
        try:
            ok, detail = await BitmainHttpBackend().sleep(self._miner(srv.server_address[1]))
            assert not ok
            assert "Fields present:" in detail and "sleep.mode_key" in detail
        finally:
            srv.shutdown(); srv.server_close()


class TestRealWorldS19XP:
    """A stock S19 XP config, captured verbatim from a live miner.

    The config uses 'bitmain-work-mode' rather than 'miner-mode', quotes the
    value as a string, and carries a null field. All three are things a naive
    read-modify-write gets wrong.
    """

    REAL = {
        "pools": [
            {"url": "stratum+tcp://pool.example.com:3333", "user": "account.worker1", "pass": "123"},
            {"url": "stratum+tcp://pool.example.com:443", "user": "account.worker1", "pass": "123"},
            {"url": "", "user": "", "pass": ""},
        ],
        "bitmain-fan-ctrl": False,
        "bitmain-fan-pwm": "100",
        "bitmain-work-mode": "0",
        "bitmain-user-ip-cat": None,
    }

    def _serve(self, doc=None):
        import threading

        conf = MinerConf(doc if doc is not None else self.REAL)
        srv = BitmainHttpServer(("127.0.0.1", 0), conf=conf)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return conf, srv

    def _miner(self, port, **kw):
        d = dict(enabled=True, backend=SleepBackend.BITMAIN_HTTP, http_port=port,
                 username="root", password="root", timeout_seconds=5)
        return Miner(id="sxp-01", host="127.0.0.1", port=4028, sleep=SleepConfig(**{**d, **kw}))

    async def test_the_field_is_discovered(self):
        conf, srv = self._serve()
        try:
            ok, detail = await BitmainHttpBackend().probe(self._miner(srv.server_address[1]))
            assert ok, detail
            assert "bitmain-work-mode=0 (normal)" in detail
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_full_sleep_wake_cycle_preserves_everything_else(self):
        conf, srv = self._serve()
        try:
            miner = self._miner(srv.server_address[1])
            backend = BitmainHttpBackend()

            assert (await backend.sleep(miner))[0]
            after = conf.get()
            assert after["bitmain-work-mode"] == "1", "string type must survive"
            assert after["pools"] == self.REAL["pools"], "pool credentials must not change"
            assert after["bitmain-user-ip-cat"] is None, "null must survive the round trip"
            assert after["bitmain-fan-ctrl"] is False

            assert (await backend.wake(miner))[0]
            assert conf.get()["bitmain-work-mode"] == "0"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_firmware_whose_sleep_is_a_different_number(self):
        """The value mapping is firmware-specific; 0/1 is only the common case."""
        conf, srv = self._serve()
        try:
            miner = self._miner(srv.server_address[1], sleep_value=3)
            assert (await BitmainHttpBackend().sleep(miner))[0]
            assert conf.get()["bitmain-work-mode"] == "3"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_an_unexpected_current_value_is_flagged_not_guessed(self):
        """If the miner sits on a value we do not recognise, say so."""
        doc = {**self.REAL, "bitmain-work-mode": "2"}
        conf, srv = self._serve(doc)
        try:
            ok, detail = await BitmainHttpBackend().probe(self._miner(srv.server_address[1]))
            assert ok
            assert "unrecognised" in detail
        finally:
            srv.shutdown(); srv.server_close()


class TestWriteVerification:
    """Stock firmware answers 'OK!' to a POST it then ignores.

    From the field, an S19 XP: the CGI returned
    {"stats":"success","code":"M000","msg":"OK!"} and the config still read
    bitmain-work-mode=0 immediately afterwards. Trusting the reply meant a
    fleet would log "sleep OK" every night while continuing to hash.
    """

    REAL = {
        "pools": [{"url": "stratum+tcp://pool.example.com:3333",
                   "user": "account.worker1", "pass": "123"}],
        "bitmain-fan-ctrl": False,
        "bitmain-fan-pwm": "100",
        "bitmain-work-mode": "0",
        "bitmain-user-ip-cat": None,
    }

    class LyingConf(MinerConf):
        """Accepts the POST, records it, and never applies it."""

        def set(self, conf):
            with self._lock:
                self.writes.append(conf)

    def _serve(self, conf):
        import threading

        srv = BitmainHttpServer(("127.0.0.1", 0), conf=conf)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def _miner(self, port, **kw):
        d = dict(enabled=True, backend=SleepBackend.BITMAIN_HTTP, http_port=port,
                 username="root", password="root", timeout_seconds=5)
        return Miner(id="sxp-01", host="127.0.0.1", port=4028, sleep=SleepConfig(**{**d, **kw}))

    async def test_a_setting_that_does_not_persist_is_a_failure(self):
        conf = self.LyingConf(self.REAL)
        srv = self._serve(conf)
        try:
            ok, detail = await BitmainHttpBackend().sleep(self._miner(srv.server_address[1]))
            assert not ok, "a silently discarded write must not report success"
            assert "did not persist" in detail
            assert "still reads" in detail
            assert conf.writes, "the POST should still have been attempted"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_setting_that_persists_reports_verified(self):
        conf = MinerConf(self.REAL)
        srv = self._serve(conf)
        try:
            ok, detail = await BitmainHttpBackend().sleep(self._miner(srv.server_address[1]))
            assert ok and "verified" in detail
            assert conf.get()["bitmain-work-mode"] == "1"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_the_failure_names_the_next_thing_to_try(self):
        """The message must route the operator onward, not just say 'failed'."""
        conf = self.LyingConf(self.REAL)
        srv = self._serve(conf)
        try:
            _, detail = await BitmainHttpBackend().sleep(self._miner(srv.server_address[1]))
            assert "diagnose sxp-01" in detail
            # And it should say which shapes were already ruled out.
            assert "bitmain-work-mode" in detail and "miner-mode" in detail
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_no_op_write_still_reports_success(self):
        """Already in the requested mode: nothing to verify, nothing to reboot."""
        conf = MinerConf({**self.REAL, "bitmain-work-mode": "1"})
        srv = self._serve(conf)
        try:
            ok, detail = await BitmainHttpBackend().sleep(self._miner(srv.server_address[1]))
            assert ok and "already" in detail
        finally:
            srv.shutdown(); srv.server_close()


class TestFormEncoding:
    """Some CGI handlers want the old _ant_-prefixed form encoding."""

    def test_pools_are_flattened_and_padded_to_three(self):
        conf = {"pools": [{"url": "u1", "user": "w1", "pass": "p1"}],
                "bitmain-work-mode": "1", "bitmain-fan-ctrl": False,
                "bitmain-user-ip-cat": None}
        body, ctype = BitmainHttpBackend._encode_conf(conf, "form")
        text = body.decode()
        assert ctype == "application/x-www-form-urlencoded"
        assert "_ant_pool1url=u1" in text
        assert "_ant_pool2url=" in text and "_ant_pool3url=" in text
        assert "_ant_work_mode=1" in text

    def test_booleans_and_nulls_are_rendered_the_cgi_way(self):
        conf = {"pools": [], "bitmain-fan-ctrl": False, "bitmain-user-ip-cat": None}
        text = BitmainHttpBackend._encode_conf(conf, "form")[0].decode()
        assert "_ant_fan_ctrl=false" in text
        assert "_ant_user_ip_cat=" in text

    def test_json_remains_the_default(self):
        body, ctype = BitmainHttpBackend._encode_conf({"pools": [], "a": 1}, "json")
        assert ctype == "application/json"
        assert json.loads(body) == {"pools": [], "a": 1}


class TestDiagnoseWrite:
    """Find the request shape a stubborn firmware honours.

    Trying several shapes is only safe because every attempt is verified: one
    that does not take is proven to have changed nothing, and the run aborts if
    any field other than the power mode moves.
    """

    REAL = {
        "pools": [{"url": "stratum+tcp://pool.example.com:3333", "user": "account.w1", "pass": "123"}],
        "bitmain-fan-ctrl": False,
        "bitmain-fan-pwm": "100",
        "bitmain-work-mode": "0",
        "bitmain-user-ip-cat": None,
    }

    def _miner(self, port, **kw):
        d = dict(enabled=True, backend=SleepBackend.BITMAIN_HTTP, http_port=port,
                 username="root", password="root", timeout_seconds=5)
        return Miner(id="sxp-01", host="127.0.0.1", port=4028, sleep=SleepConfig(**{**d, **kw}))

    def _picky_server(self, accept):
        """A miner that says OK to everything but only honours `accept`."""
        import http.server
        import threading
        import urllib.parse

        from sim.bitmain_http_sim import BitmainHandler

        conf = MinerConf(self.REAL)

        class Handler(BitmainHandler):
            def do_POST(self):
                if not self._authenticated():
                    self._read_body(); self._challenge(); return
                body = self._read_body().decode()
                honoured = False
                if accept == "form" and "_ant_work_mode=" in body:
                    fields = dict(urllib.parse.parse_qsl(body))
                    c = self.server.conf.get()
                    c["bitmain-work-mode"] = fields["_ant_work_mode"]
                    self.server.conf.set(c)
                    honoured = True
                elif accept == "no-nulls":
                    try:
                        doc = json.loads(body)
                        if all(v is not None for v in doc.values()):
                            self.server.conf.set(doc)
                            honoured = True
                    except ValueError:
                        pass
                elif accept == "any-json":
                    try:
                        self.server.conf.set(json.loads(body))
                        honoured = True
                    except ValueError:
                        pass
                # Always claims success, like the real thing.
                self._send_json(200, {"stats": "success", "code": "M000", "msg": "OK!"})

        class Server(http.server.ThreadingHTTPServer):
            def __init__(self, addr):
                self.conf = conf
                self.username, self.password, self.verbose = "root", "root", False
                super().__init__(addr, Handler)

        srv = Server(("127.0.0.1", 0))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return conf, srv

    async def test_it_finds_a_form_only_firmware(self):
        conf, srv = self._picky_server("form")
        try:
            results = await BitmainHttpBackend().diagnose_write(self._miner(srv.server_address[1]))
            winners = [label for label, ok, _ in results if ok]
            assert winners == ["form-encoded"]
        finally:
            srv.shutdown(); srv.server_close()

    async def test_it_finds_a_firmware_that_refuses_nulls(self):
        conf, srv = self._picky_server("no-nulls")
        try:
            results = await BitmainHttpBackend().diagnose_write(self._miner(srv.server_address[1]))
            winners = [label for label, ok, _ in results if ok]
            assert winners and "nulls" in winners[0]
        finally:
            srv.shutdown(); srv.server_close()

    async def test_the_original_value_is_restored(self):
        conf, srv = self._picky_server("any-json")
        try:
            await BitmainHttpBackend().diagnose_write(self._miner(srv.server_address[1]))
            assert conf.get()["bitmain-work-mode"] == "0", "diagnose must put the miner back"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_it_stops_once_a_shape_works(self):
        conf, srv = self._picky_server("any-json")
        try:
            results = await BitmainHttpBackend().diagnose_write(self._miner(srv.server_address[1]))
            assert results[-1][1] is True, "no further shapes should be tried after a win"
            assert len(results) < len(BitmainHttpBackend.WRITE_VARIANTS)
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_firmware_that_honours_nothing_reports_every_failure(self):
        conf, srv = self._picky_server("nothing")
        try:
            results = await BitmainHttpBackend().diagnose_write(self._miner(srv.server_address[1]))
            assert not any(ok for _, ok, _ in results)
            assert len(results) == len(BitmainHttpBackend.WRITE_VARIANTS)
            assert conf.get()["bitmain-work-mode"] == "0"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_it_aborts_if_an_unrelated_field_moves(self):
        """A shape that corrupts other settings must stop the run immediately."""
        import http.server
        import threading

        from sim.bitmain_http_sim import BitmainHandler

        conf = MinerConf(self.REAL)

        class Destructive(BitmainHandler):
            def do_POST(self):
                if not self._authenticated():
                    self._read_body(); self._challenge(); return
                self._read_body()
                c = self.server.conf.get()
                c["bitmain-fan-pwm"] = "0"     # wrong field, and not the mode
                self.server.conf.set(c)
                self._send_json(200, {"stats": "success", "msg": "OK!"})

        class Server(http.server.ThreadingHTTPServer):
            def __init__(self, addr):
                self.conf = conf
                self.username, self.password, self.verbose = "root", "root", False
                super().__init__(addr, Destructive)

        srv = Server(("127.0.0.1", 0))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            results = await BitmainHttpBackend().diagnose_write(self._miner(srv.server_address[1]))
            assert len(results) == 1, "must stop at the first sign of collateral damage"
            assert "changed unexpectedly" in results[0][2]
            assert "bitmain-fan-pwm" in results[0][2]
        finally:
            srv.shutdown(); srv.server_close()


class TestCgminerDiagnose:
    """Ask the firmware what it supports before sending anything.

    cgminer's `check` reports Exists and Access per command, so "does this
    firmware implement a sleep, and are we even allowed to call it" is
    answerable read-only. Access=N is the common and confusing case: the
    command exists, and the miner refuses it because api-allow grants no W.
    """

    def _serve(self, allow_privileged=True):
        import threading

        state = SimState(ghs=95170, seed=6, stock=True)
        srv = SimServer(("127.0.0.1", 0), state, control_file=None,
                        allow_privileged=allow_privileged)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return state, srv

    def _miner(self, port):
        return Miner(id="m", host="127.0.0.1", port=port,
                     sleep=SleepConfig(enabled=True, backend=SleepBackend.CGMINER,
                                       timeout_seconds=5))

    async def test_a_writable_firmware_finds_a_working_command(self):
        state, srv = self._serve(allow_privileged=True)
        try:
            results = await CgminerBackend().diagnose_write(self._miner(srv.server_address[1]))
            tried = [(label, ok) for label, ok, _ in results if label.startswith("try ")]
            assert tried and tried[0][1] is True
            assert any(label.startswith("undo") for label, _, _ in results)
        finally:
            srv.shutdown(); srv.server_close()

    async def test_the_miner_is_left_running(self):
        """Whatever is sent must be undone."""
        state, srv = self._serve(allow_privileged=True)
        try:
            await CgminerBackend().diagnose_write(self._miner(srv.server_address[1]))
            assert state.state == SimState.MINING
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_read_only_api_is_reported_as_a_permission_problem(self):
        state, srv = self._serve(allow_privileged=False)
        try:
            results = await CgminerBackend().diagnose_write(self._miner(srv.server_address[1]))
            ascset = [r for r in results if r[0] == "check ascset"][0]
            assert ascset[1] is False
            assert "Access=N" in ascset[2]
            assert "api-allow" in ascset[2]
        finally:
            srv.shutdown(); srv.server_close()

    async def test_nothing_is_sent_when_nothing_is_permitted(self):
        state, srv = self._serve(allow_privileged=False)
        try:
            results = await CgminerBackend().diagnose_write(self._miner(srv.server_address[1]))
            assert not any(label.startswith("try ") for label, _, _ in results)
            assert any("no usable sleep command" in detail for _, _, detail in results)
            assert state.state == SimState.MINING
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_command_the_firmware_lacks_is_reported_as_missing(self):
        state, srv = self._serve(allow_privileged=True)
        try:
            results = await CgminerBackend().diagnose_write(self._miner(srv.server_address[1]))
            quit_row = [r for r in results if r[0] == "check quit"][0]
            assert quit_row[1] is False and "Exists=N" in quit_row[2]
        finally:
            srv.shutdown(); srv.server_close()

    async def test_an_unreachable_miner_does_not_raise(self):
        miner = Miner(id="m", host="127.0.0.1", port=1,
                      sleep=SleepConfig(enabled=True, backend=SleepBackend.CGMINER,
                                        timeout_seconds=1))
        results = await CgminerBackend().diagnose_write(miner)
        assert results and not any(ok for _, ok, _ in results)


class TestDiagnoseWithoutCheckCommand:
    """bmminer is a cgminer fork and several builds dropped `check`.

    From the field, an S19 XP answered "Invalid command" to `check` itself, so
    the read-only enumeration collapsed and reported nothing useful. Probing
    the commands directly distinguishes the same three cases from the refusal
    text alone.
    """

    def _serve(self, **kw):
        import threading

        state = SimState(ghs=95170, seed=8, stock=True)
        srv = SimServer(("127.0.0.1", 0), state, control_file=None,
                        supports_check=False, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return state, srv

    def _miner(self, port):
        return Miner(id="m", host="127.0.0.1", port=port,
                     sleep=SleepConfig(enabled=True, backend=SleepBackend.CGMINER,
                                       timeout_seconds=5))

    async def test_it_falls_back_and_says_so(self):
        state, srv = self._serve()
        try:
            results = await CgminerBackend().diagnose_write(self._miner(srv.server_address[1]))
            first = results[0]
            assert first[0] == "check command" and first[1] is False
            assert "no `check`" in first[2] and "probing" in first[2]
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_working_sleep_is_still_found(self):
        state, srv = self._serve()
        try:
            results = await CgminerBackend().diagnose_write(self._miner(srv.server_address[1]))
            tried = [(l, ok) for l, ok, _ in results if l.startswith("try ")]
            assert tried and tried[0][1] is True
            assert state.state == SimState.MINING, "the probe must undo itself"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_permission_refusal_is_named_as_such(self):
        """The actionable case: the command is there, the miner says no."""
        state, srv = self._serve(allow_privileged=False)
        try:
            results = await CgminerBackend().diagnose_write(self._miner(srv.server_address[1]))
            tried = [d for l, ok, d in results if l.startswith("try ")]
            assert tried and all("lacks privileged access" in d for d in tried)
            assert state.state == SimState.MINING
        finally:
            srv.shutdown(); srv.server_close()

    async def test_a_missing_command_is_named_as_such(self):
        """The genuinely-unsupported case, distinct from a refusal."""
        state, srv = self._serve(supports_sleep=False)
        try:
            results = await CgminerBackend().diagnose_write(self._miner(srv.server_address[1]))
            tried = [d for l, ok, d in results if l.startswith("try ")]
            assert tried and all("does not implement" in d for d in tried)
        finally:
            srv.shutdown(); srv.server_close()

    async def test_restart_and_quit_are_never_sent(self):
        """A diagnostic must not be the thing that takes a miner down."""
        state, srv = self._serve()
        try:
            results = await CgminerBackend().diagnose_write(self._miner(srv.server_address[1]))
            sent = [l for l, _, _ in results if l.startswith("try ")]
            assert not any("restart" in l or "quit" in l for l in sent)
            assert state.state != SimState.RESTARTING
        finally:
            srv.shutdown(); srv.server_close()


class TestAsymmetricFirmware:
    """An S19 XP captured from a live rack.

    get_miner_conf.cgi returns "bitmain-work-mode": "0"; saving Work Mode in
    the web UI posts "miner-mode": 1 with Content-Type text/plain. Echoing the
    document back under the field it was read from is accepted, answered
    "OK!", and discarded. Read/write symmetry was the wrong assumption.
    """

    from sim.bitmain_http_sim import AsymmetricConf as _Conf

    def _serve(self, **kw):
        import threading

        conf = self._Conf(**kw)
        srv = BitmainHttpServer(("127.0.0.1", 0), conf=conf)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return conf, srv

    def _miner(self, port, **kw):
        d = dict(enabled=True, backend=SleepBackend.BITMAIN_HTTP, http_port=port,
                 username="root", password="root", timeout_seconds=5)
        return Miner(id="sxp-01", host="127.0.0.1", port=4028, sleep=SleepConfig(**{**d, **kw}))

    async def test_sleep_and_wake_work(self):
        conf, srv = self._serve()
        try:
            miner = self._miner(srv.server_address[1])
            backend = BitmainHttpBackend()

            ok, detail = await backend.sleep(miner)
            assert ok, detail
            assert "via miner-mode" in detail and "verified" in detail
            assert conf.miner_mode == 1

            ok, detail = await backend.wake(miner)
            assert ok, detail
            assert conf.miner_mode == 0
        finally:
            srv.shutdown(); srv.server_close()

    async def test_it_works_when_the_content_type_is_also_checked(self):
        conf, srv = self._serve(require_content_type="text/plain;charset=UTF-8")
        try:
            ok, detail = await BitmainHttpBackend().sleep(self._miner(srv.server_address[1]))
            assert ok, detail
            assert conf.miner_mode == 1
        finally:
            srv.shutdown(); srv.server_close()

    async def test_the_read_side_name_is_dropped_from_the_write(self):
        conf, srv = self._serve()
        try:
            await BitmainHttpBackend().sleep(self._miner(srv.server_address[1]))
            posted = conf.writes[-1]
            assert "miner-mode" in posted
            assert "bitmain-work-mode" not in posted, "the read-side name must not be sent"
            assert isinstance(posted["miner-mode"], int), "the UI sends an int"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_nulls_become_zero_as_the_browser_sends_them(self):
        conf, srv = self._serve()
        try:
            await BitmainHttpBackend().sleep(self._miner(srv.server_address[1]))
            assert conf.writes[-1]["bitmain-user-ip-cat"] == "0"
        finally:
            srv.shutdown(); srv.server_close()

    async def test_pool_credentials_survive(self):
        """Every write posts the whole document, credentials included."""
        conf, srv = self._serve()
        try:
            before = conf.get()["pools"]
            miner = self._miner(srv.server_address[1])
            await BitmainHttpBackend().sleep(miner)
            await BitmainHttpBackend().wake(miner)
            assert conf.get()["pools"] == before
        finally:
            srv.shutdown(); srv.server_close()

    async def test_the_mirror_profile_still_fails_on_this_firmware(self):
        """Proves the alias is what fixes it, not something incidental."""
        conf, srv = self._serve()
        try:
            miner = self._miner(srv.server_address[1], write_profile="mirror")
            ok, detail = await BitmainHttpBackend().sleep(miner)
            assert not ok
            assert "did not persist" in detail
        finally:
            srv.shutdown(); srv.server_close()

    async def test_diagnose_identifies_the_browser_shape(self):
        conf, srv = self._serve(require_content_type="text/plain;charset=UTF-8")
        try:
            results = await BitmainHttpBackend().diagnose_write(self._miner(srv.server_address[1]))
            winners = [label for label, ok, _ in results if ok]
            assert winners and "browser shape" in winners[0]
            assert conf.miner_mode == 0, "diagnose must restore the original mode"
        finally:
            srv.shutdown(); srv.server_close()
