import json

from minerwatch.poller import classify
from minerwatch.models import State


def _mining_payload(ghs_5s=13500.0, status="Alive"):
    return json.dumps({
        "STATUS": [{"Status": "Alive", "When": 1700000000}],
        "SUMMARY": [{"GHS 5s": ghs_5s, "Status": status, "Elapsed": 100}],
        "id": 1,
    }).encode("utf-8")


def _stopped_payload(ghs_5s=0.0, status="Sick"):
    return json.dumps({
        "STATUS": [{"Status": status, "When": 1700000000}],
        "SUMMARY": [{"GHS 5s": ghs_5s, "Status": status, "Elapsed": 0}],
        "id": 1,
    }).encode("utf-8")


class TestClassify:
    def test_mining(self):
        state, reason = classify(_mining_payload(13500.0, "Alive"))
        assert state == State.MINING

    def test_stopped_zero_ghs(self):
        state, reason = classify(_stopped_payload(0.0, "Sick"))
        assert state == State.STOPPED

    def test_stopped_status_dead(self):
        state, reason = classify(_stopped_payload(0.0, "Dead"))
        assert state == State.STOPPED

    def test_trailing_null_stripped(self):
        raw = json.dumps({
            "STATUS": [{"Status": "Sick", "When": 1700000000}],
            "SUMMARY": [{"GHS 5s": 0.0, "Status": "Sick", "Elapsed": 0}],
            "id": 1,
        }).encode("utf-8") + b"\x00\x00"
        state, reason = classify(raw)
        assert state == State.STOPPED

    def test_garbage_bytes(self):
        state, reason = classify(b"not json at all \xff\xfe")
        assert state == State.UNREACHABLE

    def test_exception(self):
        state, reason = classify(ConnectionRefusedError("connection refused"))
        assert state == State.UNREACHABLE
        assert "connection refused" in reason

    def test_sim_shape(self):
        raw = json.dumps({
            "STATUS": [{"Status": "Alive", "When": 1700000000}],
            "SUMMARY": [{"GHS 5s": 0.0, "Status": "Alive", "state": "stopped", "Elapsed": 0}],
            "id": 1,
        }).encode("utf-8")
        state, reason = classify(raw)
        assert state == State.STOPPED


# ---------------------------------------------------------------------------
# End-to-end: poll -> classify -> sleep/wake -> watchdog hand-off
# ---------------------------------------------------------------------------

import asyncio
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from minerwatch.models import (
    Miner,
    Range,
    Schedule,
    SleepBackend,
    SleepConfig,
    Window,
)
from minerwatch.poller import Poller
from minerwatch.store import init_db
from sim.miner_sim import SimServer, SimState

ALWAYS = Schedule(
    timezone=timezone.utc,
    windows=[Window(days=frozenset(range(7)), ranges=[Range(start=0, end=1440)])],
)
NEVER = Schedule(timezone=timezone.utc, windows=[])


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def sim():
    state = SimState(ghs=13500, seed=7)
    server = SimServer(("127.0.0.1", 0), state, control_file=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def build_poller(conn, miners, sleep_dry_run=False):
    config = (15, ":memory:", "UTC", miners)
    return Poller(config, conn, threading.Event(), dry_run=True, sleep_dry_run=sleep_dry_run)


def live_sleep_miner(port, schedule):
    return Miner(
        id="m1",
        host="127.0.0.1",
        port=port,
        schedule=schedule,
        sleep=SleepConfig(
            enabled=True,
            backend=SleepBackend.CGMINER,
            dry_run=False,
            cooldown_seconds=0,
            grace_seconds=0,
            timeout_seconds=5,
        ),
    )


def actions(conn, miner_id="m1"):
    return [
        r[0]
        for r in conn.execute(
            "SELECT action FROM events WHERE miner = ? ORDER BY id", (miner_id,)
        ).fetchall()
    ]


class TestPollerSleepIntegration:
    async def test_miner_outside_its_window_is_slept(self, conn, sim):
        state, port = sim
        miner = live_sleep_miner(port, NEVER)  # never a working window
        poller = build_poller(conn, {miner.id: miner})

        await poller._poll_one(miner)

        assert state.state == SimState.SLEEPING
        assert "sleep" in actions(conn)

    async def test_slept_miner_is_not_restarted_by_the_watchdog(self, conn, sim):
        """The regression this whole design exists to prevent."""
        state, port = sim
        miner = live_sleep_miner(port, NEVER)
        poller = build_poller(conn, {miner.id: miner})
        poller.watchdog.consider = AsyncMock()

        await poller._poll_one(miner)   # sleeps it
        await poller._poll_one(miner)   # sees it stopped, must stay hands-off
        await poller._poll_one(miner)

        assert state.state == SimState.SLEEPING
        poller.watchdog.consider.assert_not_awaited()

    async def test_window_reopening_wakes_the_miner(self, conn, sim):
        state, port = sim
        asleep = live_sleep_miner(port, NEVER)
        poller = build_poller(conn, {asleep.id: asleep})
        await poller._poll_one(asleep)
        assert state.state == SimState.SLEEPING

        # Same miner id, but its window is now open.
        awake = live_sleep_miner(port, ALWAYS)
        await poller._poll_one(awake)
        assert state.state == SimState.MINING
        assert "wake" in actions(conn)

    async def test_a_genuinely_dead_miner_still_reaches_the_watchdog(self, conn, sim):
        state, port = sim
        miner = live_sleep_miner(port, ALWAYS)   # window open
        state.set_state(SimState.STOPPED, state.now())
        poller = build_poller(conn, {miner.id: miner})
        poller.watchdog.consider = AsyncMock()

        await poller._poll_one(miner)

        poller.watchdog.consider.assert_awaited_once()

    async def test_unreachable_miner_reaches_the_watchdog(self, conn):
        miner = live_sleep_miner(1, ALWAYS)  # nothing listening on port 1
        poller = build_poller(conn, {miner.id: miner})
        poller.watchdog.consider = AsyncMock()

        await poller._poll_one(miner)

        poller.watchdog.consider.assert_awaited_once()
        assert actions(conn)[0] == "alert"

    async def test_sleep_dry_run_leaves_hardware_alone(self, conn, sim):
        state, port = sim
        miner = live_sleep_miner(port, NEVER)
        poller = build_poller(conn, {miner.id: miner}, sleep_dry_run=True)

        await poller._poll_one(miner)

        assert state.state == SimState.MINING          # untouched
        assert "would_sleep" in actions(conn)          # but intent is recorded

    async def test_sleeping_miner_classifies_as_stopped(self, conn, sim):
        state, port = sim
        state.sleep(state.now())
        miner = live_sleep_miner(port, ALWAYS)
        poller = build_poller(conn, {miner.id: miner})
        poller.watchdog.consider = AsyncMock()

        await poller._poll_one(miner)

        row = conn.execute("SELECT state FROM events ORDER BY id LIMIT 1").fetchone()
        assert row[0] == "stopped"


class TestPollerLoopRobustness:
    async def test_one_failing_miner_does_not_stop_the_others(self, conn, sim):
        """A raising poll must not cancel its siblings or break run().

        Exercises Poller.run itself: with a plain gather() the RuntimeError
        propagates out of run() and the supervision loop dies, taking every
        other miner with it.
        """
        state, port = sim
        good = live_sleep_miner(port, ALWAYS)
        bad = Miner(id="bad", host="127.0.0.1", port=port, schedule=ALWAYS)
        config = (1, ":memory:", "UTC", {"bad": bad, "m1": good})
        stop = threading.Event()
        poller = Poller(config, conn, stop, dry_run=True, sleep_dry_run=True)

        polled = []
        original = poller._poll_one

        async def flaky(miner):
            if miner.id == "bad":
                raise RuntimeError("kaboom")
            polled.append(miner.id)
            await original(miner)
            stop.set()  # one full cycle is enough

        poller._poll_one = flaky

        await poller.run()  # must return normally, not raise

        assert polled == ["m1"], "the healthy miner was never polled"

    async def test_a_raising_poll_is_logged_not_swallowed(self, conn, sim, caplog):
        _, port = sim
        bad = Miner(id="bad", host="127.0.0.1", port=port, schedule=ALWAYS)
        config = (1, ":memory:", "UTC", {"bad": bad})
        stop = threading.Event()
        poller = Poller(config, conn, stop, dry_run=True, sleep_dry_run=True)

        async def explode(miner):
            stop.set()
            raise RuntimeError("kaboom")

        poller._poll_one = explode
        with caplog.at_level("ERROR"):
            await poller.run()
        assert "kaboom" in caplog.text

    async def test_run_exits_promptly_when_stopped(self, conn, sim):
        """A long poll interval must not delay shutdown."""
        _, port = sim
        miner = live_sleep_miner(port, ALWAYS)
        config = (3600, ":memory:", "UTC", {miner.id: miner})
        stop = threading.Event()
        poller = Poller(config, conn, stop, dry_run=True, sleep_dry_run=True)

        async def stopper():
            await asyncio.sleep(0.1)
            stop.set()

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.gather(poller.run(), stopper())
        # Would be 3600s without the interruptible sleep.
        assert loop.time() - started < 5


# ---------------------------------------------------------------------------
# Real firmware payload shapes
# ---------------------------------------------------------------------------

class TestRealFirmwarePayloads:
    """Payloads as stock bmminer/cgminer actually sends them.

    From the field: all twelve miners in a live fleet reported `unreachable`
    with "unexpected payload" while hashing normally. classify() required
    `Status == "Alive"` inside SUMMARY, and stock firmware does not put a
    Status field there at all - it lives in the STATUS envelope, and per-device
    "Alive" comes from the `devs` command. The simulator did send it, so no
    test ever caught this: the fake miner had been built to match the
    implementation rather than the protocol.
    """

    def _payload(self, summary, status=None):
        doc = {"id": 1}
        if status is not None:
            doc["STATUS"] = status
        if summary is not None:
            doc["SUMMARY"] = summary
        return json.dumps(doc).encode()

    def test_a_healthy_s19_is_mining(self):
        raw = self._payload(
            [{"Elapsed": 183041, "GHS 5s": 95170.14, "GHS av": 94800.22,
              "Found Blocks": 0, "Getworks": 4231, "Accepted": 15832,
              "Rejected": 41, "Hardware Errors": 112, "Best Share": 8123456}],
            [{"STATUS": "S", "When": 1756139219, "Code": 11, "Msg": "Summary",
              "Description": "bmminer 1.0.0"}],
        )
        assert classify(raw)[0] == State.MINING

    def test_a_slept_s19_reports_zero_and_is_stopped(self):
        raw = self._payload(
            [{"Elapsed": 42, "GHS 5s": 0.0, "GHS av": 0.0, "Accepted": 0}],
            [{"STATUS": "S", "Code": 11, "Msg": "Summary"}],
        )
        assert classify(raw)[0] == State.STOPPED

    def test_firmware_reporting_only_mhs(self):
        raw = self._payload([{"Elapsed": 900, "MHS 5s": 13500000.0}])
        assert classify(raw)[0] == State.MINING

    def test_firmware_reporting_ths(self):
        raw = self._payload([{"Elapsed": 900, "THS 5s": 95.17}])
        assert classify(raw)[0] == State.MINING

    def test_a_hashrate_quoted_as_a_string(self):
        raw = self._payload([{"GHS 5s": "95170.14", "Elapsed": 10}])
        assert classify(raw)[0] == State.MINING

    def test_the_instantaneous_rate_wins_over_the_average(self):
        """A miner that just stopped still shows a healthy lifetime average."""
        raw = self._payload([{"GHS 5s": 0.0, "GHS av": 94800.0, "Elapsed": 183041}])
        assert classify(raw)[0] == State.STOPPED

    def test_an_api_allow_denial_is_unreachable_not_stopped(self):
        """The dangerous case: read as 'stopped', the watchdog restarts a
        healthy miner that merely refused to talk to us."""
        raw = self._payload(
            None,
            [{"STATUS": "E", "When": 1756139219, "Code": 45,
              "Msg": "Access denied to 'summary' command",
              "Description": "bmminer 1.0.0"}],
        )
        state, reason = classify(raw)
        assert state == State.UNREACHABLE
        assert "Access denied" in reason

    def test_a_reply_with_no_hashrate_names_the_keys_it_did_send(self):
        raw = self._payload([{"Elapsed": 10, "Accepted": 3}])
        state, reason = classify(raw)
        assert state == State.UNREACHABLE
        assert "no hashrate field" in reason
        assert "Accepted" in reason and "Elapsed" in reason

    def test_an_explicit_sick_status_still_beats_hashrate(self):
        """Firmwares that do send Status must keep working."""
        raw = self._payload([{"GHS 5s": 95170.0, "Status": "Sick"}])
        assert classify(raw)[0] == State.STOPPED

    def test_a_summary_free_success_envelope_is_unreachable(self):
        raw = self._payload(None, [{"STATUS": "S", "Code": 11, "Msg": "Summary"}])
        assert classify(raw)[0] == State.UNREACHABLE

    def test_the_old_simulator_shape_still_classifies(self):
        """The simulator sends a superset; it must not regress."""
        raw = self._payload(
            [{"GHS 5s": 13500.0, "Status": "Alive", "state": "mining", "Elapsed": 100}],
            [{"STATUS": "S", "Code": 11, "Msg": "OK"}],
        )
        assert classify(raw)[0] == State.MINING


class TestAgainstStockFirmwareSimulator:
    """End to end against a simulator that sends only stock bmminer fields.

    The default simulator sends "Status" and "state" inside SUMMARY as a
    convenience. Real firmware does not, and that difference hid a bug that
    made every healthy miner read as unreachable. These tests run the whole
    poll path against the honest payload.
    """

    async def test_a_stock_miner_reads_as_mining(self, conn):
        state = SimState(ghs=95170, seed=11, stock=True)
        assert "Status" not in state.summary()
        server = SimServer(("127.0.0.1", 0), state, control_file=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            miner = live_sleep_miner(server.server_address[1], ALWAYS)
            poller = build_poller(conn, {miner.id: miner}, sleep_dry_run=True)
            await poller._poll_one(miner)
            row = conn.execute(
                "SELECT state, action FROM events ORDER BY id LIMIT 1").fetchone()
            assert row == ("mining", "none")
        finally:
            server.shutdown()
            server.server_close()

    async def test_a_stock_miner_that_is_slept_reads_as_stopped(self, conn):
        state = SimState(ghs=95170, seed=11, stock=True)
        server = SimServer(("127.0.0.1", 0), state, control_file=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            miner = live_sleep_miner(server.server_address[1], NEVER)
            poller = build_poller(conn, {miner.id: miner})
            await poller._poll_one(miner)   # outside window -> sleeps it
            assert state.state == SimState.SLEEPING
            await poller._poll_one(miner)
            rows = [r[0] for r in conn.execute(
                "SELECT state FROM events WHERE action IN ('none','alert','expected_off')"
                " ORDER BY id")]
            assert rows[-1] == "stopped"
        finally:
            server.shutdown()
            server.server_close()
