"""Fake Antminer TCP server implementing a subset of the cgminer JSON API.

Used to exercise MinerWatch — including the sleep/wake path — without real
hardware. Understands ``summary``, ``stats``, ``restart``, and the ``ascset``
/ ``pause`` / ``resume`` commands the sleep backends issue.
"""

import argparse
import json
import os
import random
import socket
import socketserver
import threading
import time

# Put the repository root on sys.path before importing the package: running
# these files as plain scripts (``python sim/miner_sim.py``) puts only ``sim/``
# on the path. Both spellings are needed because which one resolves depends on
# whether the file was launched as a script or as ``python -m sim.<module>``.
try:
    from sim import _bootstrap  # noqa: F401
except ImportError:  # pragma: no cover - script-launch path
    import _bootstrap  # noqa: F401

from minerwatch.compat import ALLOW_REUSE_ADDRESS, read_text, write_text_atomic

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Track the last-known content per control-file path for change detection.
# Content rather than mtime: NTFS timestamps are coarse enough (and FAT/exFAT
# volumes coarse enough by 2 seconds) that two quick edits can share an mtime,
# which made a control-file change silently fail to apply on the Windows host.
_control_file_state = {}
# ThreadingTCPServer runs each connection on its own thread and every handler
# touches the cache, so it needs a lock.
_control_file_lock = threading.RLock()


class SimState:
    MINING = "mining"
    STOPPED = "stopped"
    RESTARTING = "restarting"
    #: Software-slept: the API still answers, hashrate is zero, and the miner
    #: reports a distinct status so a consumer can tell an intentional stop
    #: from a fault.
    SLEEPING = "sleeping"

    VALID_STATES = (MINING, STOPPED, RESTARTING, SLEEPING)

    def __init__(self, ghs=13500.0, restart_secs=10, now=None, seed=None, stock=False):
        #: Emit only the fields stock bmminer sends. The convenience fields
        #: below ("Status" and "state" inside SUMMARY) do not exist on real
        #: firmware, and a simulator that invents them lets a classifier which
        #: depends on them pass every test while failing on every real miner -
        #: which is exactly what happened.
        self.stock = stock
        self.state = self.MINING
        self.ghs = ghs
        self.ghs_5s = ghs
        self.restart_secs = restart_secs
        self.elapsed = 0
        self._now = now or time.time
        self._start_time = self._now()
        self._restart_until = 0.0
        self._lock = threading.RLock()
        self._temps = [68, 70, 72]
        self._fans = [4800, 5000]
        self._rng = random.Random(seed)

    def now(self):
        return self._now()

    def tick(self, now):
        with self._lock:
            self._tick_locked(now)

    def _tick_locked(self, now):
        if self.state == self.RESTARTING and now >= self._restart_until:
            self.state = self.MINING
            self.elapsed = 0
            self._start_time = now
        if self.state == self.MINING:
            elapsed = int(now - self._start_time)
            if elapsed > self.elapsed:
                self.elapsed = elapsed
        if self.state == self.MINING:
            self.ghs_5s = self.ghs + self._rng.uniform(-0.05 * self.ghs, 0.05 * self.ghs)
        elif self.state in (self.STOPPED, self.RESTARTING, self.SLEEPING):
            self.ghs_5s = 0.0

    def set_state(self, new_state, now):
        with self._lock:
            self.state = new_state
            if new_state == self.MINING:
                self.elapsed = 0
                self._start_time = now
                self.ghs_5s = self.ghs + self._rng.uniform(-0.05 * self.ghs, 0.05 * self.ghs)
            elif new_state in (self.STOPPED, self.SLEEPING):
                self.ghs_5s = 0.0
            elif new_state == self.RESTARTING:
                self.ghs_5s = 0.0
                self._restart_until = now + self.restart_secs

    def restart(self, now):
        self.set_state(self.RESTARTING, now)

    def sleep(self, now):
        """Enter software sleep. Idempotent."""
        self.set_state(self.SLEEPING, now)

    def wake(self, now):
        """Leave software sleep and resume hashing. Idempotent."""
        self.set_state(self.MINING, now)

    def get_status_string(self):
        return {
            "mining": "Alive",
            "stopped": "Sick",
            "restarting": "Restarting",
            "sleeping": "Sleeping",
        }[self.state]

    def summary(self):
        with self._lock:
            alive = self.state == self.MINING
            summary = {
                "Elapsed": self.elapsed,
                "GHS 5s": round(self.ghs_5s, 2) if alive else 0.0,
                "GHS av": round(self.ghs, 2),
                "MHS 5s": round(self.ghs_5s * 1000, 2) if alive else 0.0,
                "Accepted": self._rng.randint(1000, 5000),
                "Rejected": self._rng.randint(0, 100),
                "Hardware Errors": self._rng.randint(0, 10),
            }
            if not self.stock:
                # Convenience fields for tests; absent on real firmware.
                summary["Status"] = self.get_status_string()
                summary["state"] = self.state
            return summary

    def stats(self):
        with self._lock:
            alive = self.state == self.MINING
            if alive:
                temps = [round(t + self._rng.uniform(-2, 2), 1) for t in self._temps]
                fans = [int(f + self._rng.uniform(-200, 200)) for f in self._fans]
            elif self.state == self.SLEEPING:
                # A slept miner keeps its fans turning slowly; it is powered.
                temps = [35.0, 35.0, 35.0]
                fans = [1200, 1200]
            else:
                temps = [30.0, 30.0, 30.0]
                fans = [0, 0]
            return {
                "temp1": temps[0], "temp2": temps[1], "temp3": temps[2],
                "temps": temps,
                "fan1": fans[0], "fan2": fans[1],
                "fans": fans,
                "GHS 5s": round(self.ghs_5s, 2) if alive else 0.0,
                "frequency": 500,
                "chain_acn": 3 if alive else 0,
                "Elapsed": self.elapsed,
                "miner-mode": 1 if self.state == self.SLEEPING else 0,
            }

    def maybe_apply_control(self, data):
        with self._lock:
            state_str = data.get("state", "mining")
            if state_str not in self.VALID_STATES:
                return  # invalid state — silently ignore
            if state_str != self.state:
                self.set_state(state_str, self._now())
            ghs_5s = data.get("ghs_5s")
            if ghs_5s is not None:
                self.ghs_5s = float(ghs_5s)
                self.ghs = float(ghs_5s)
            temps = data.get("temps")
            if temps is not None and len(temps) >= 3:
                self._temps = [float(t) for t in temps[:3]]
            fans_data = data.get("fans")
            if fans_data is not None and len(fans_data) >= 2:
                self._fans = [float(f) for f in fans_data[:2]]
            rs = data.get("restart_secs")
            if rs is not None:
                self.restart_secs = float(rs)

    def to_dict(self):
        with self._lock:
            return {
                "state": self.state,
                "ghs_5s": self.ghs_5s,
                "temps": self._temps,
                "fans": self._fans,
                "restart_secs": self.restart_secs,
            }


def control_file_path(port):
    return os.path.join(SCRIPT_DIR, f"state-{port}.json")


def read_control_file(path, state):
    """Apply the control file to *state* if its content changed since last read.

    Compares content rather than mtime. The file is a few hundred bytes, so
    reading it on every request is free, and it removes a whole class of
    "my edit did not take effect" bugs on filesystems with coarse timestamps.
    """
    try:
        text = read_text(path)
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    with _control_file_lock:
        if _control_file_state.get(path) == text:
            return None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError):
            # Do not cache unparseable content: the writer may be mid-save and
            # the next read should try again.
            return None
        if not isinstance(data, dict):
            return None
        # Only cache after a successful parse.
        _control_file_state[path] = text

    state.maybe_apply_control(data)
    return data


def write_control_file(path, state):
    data = state.to_dict()
    text = json.dumps(data)
    # Atomic + explicit UTF-8: a reader must never observe a half-written file,
    # and the locale default encoding differs between the Linux dev box and the
    # Windows host.
    write_text_atomic(path, text)
    with _control_file_lock:
        _control_file_state[path] = text
    return data


def ensure_control_file(path, state, force=False):
    """Make the control file reflect *state* at startup.

    ``force=True`` overwrites an existing file. The simulators pass it whenever
    an explicit start state was requested on the command line: otherwise a
    leftover ``state-<port>.json`` from a previous run is applied on the first
    client request and silently overrides ``--stopped`` / ``--sleeping``, which
    looks exactly like the flag being ignored.

    A failure to write is not fatal — the simulator is still perfectly usable
    without a control file, and on Windows the write can be refused simply
    because another process has the file open.
    """
    try:
        if force or not os.path.exists(path):
            write_control_file(path, state)
        # Otherwise leave the existing file alone: adopting it on the first
        # request is the documented behaviour.
    except OSError as exc:
        print(f"warning: could not write control file {path}: {exc}", flush=True)
    return path


STATUS_OK = {"STATUS": "S", "Code": 11, "Msg": "OK"}
STATUS_ERR = {"STATUS": "E", "Code": 22, "Msg": "Error"}
DESCRIPTION = "minerwatch-sim 0.1"


def make_envelope(payload_key, payload, status, when):
    return {
        "STATUS": [{**status, "When": when, "Description": DESCRIPTION}],
        payload_key: payload,
        "id": 1,
    }


def _parse_ascset(parameter):
    """Split an ``ascset`` parameter such as ``"0,sleep"`` into (index, verb, args).

    Returns ``(None, None, None)`` when the parameter is not in that shape.
    """
    if not isinstance(parameter, str):
        return None, None, None
    parts = parameter.split(",")
    if len(parts) < 2:
        return None, None, None
    try:
        index = int(parts[0])
    except (TypeError, ValueError):
        return None, None, None
    return index, parts[1].strip().lower(), parts[2:]


class MinerHandler(socketserver.StreamRequestHandler):
    def handle(self):
        sim_server = self.server.sim_server
        state = sim_server.state
        use_null = sim_server.use_null_terminator
        now = state.now()
        state.tick(now)

        cf = sim_server.control_file
        if cf:
            read_control_file(cf, state)

        self.request.settimeout(2.0)
        raw = b""
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                raw += chunk
                if b"\x00" in chunk:
                    # Null terminator found — strip and stop
                    raw = raw[: raw.index(b"\x00") + 1]
                    break
                # No null terminator: if client is done sending,
                # the next recv will time out. Accumulate until timeout.
        except (socket.timeout, TimeoutError):
            pass
        except OSError:
            return
        if not raw:
            return

        now = state.now()
        state.tick(now)

        try:
            req = json.loads(raw.decode("utf-8", errors="replace").strip("\x00").strip())
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(make_envelope("SUMMARY", [], {**STATUS_ERR, "Msg": "Malformed request"}, int(now)), use_null)
            return
        if not isinstance(req, dict):
            self._send(make_envelope("SUMMARY", [], {**STATUS_ERR, "Msg": "Malformed request"}, int(now)), use_null)
            return

        resp = self._dispatch(req, state, int(now))
        self._send(resp, use_null)

    def _dispatch(self, req, state, when):
        cmd = str(req.get("command", "")).strip().lower()
        parameter = req.get("parameter")

        if cmd == "summary":
            return make_envelope("SUMMARY", [state.summary()], STATUS_OK, when)
        if cmd == "stats":
            return make_envelope("STATS", [state.stats()], STATUS_OK, when)
        if cmd == "version":
            # Read-only; every real firmware implements it, which is why the
            # `check` command uses it to prove the API is reachable.
            return make_envelope(
                "VERSION",
                [{
                    "BMMiner": "1.0.0",
                    "API": "3.1",
                    "Miner": "1.0.0.3",
                    "CompileTime": "Fri Jan 1 00:00:00 UTC 2027",
                    "Type": "Antminer Simulator",
                }],
                {**STATUS_OK, "Code": 22, "Msg": "BMMiner versions"},
                when,
            )
        if cmd == "restart":
            state.restart(state.now())
            return make_envelope("SUMMARY", [state.summary()], STATUS_OK, when)
        if cmd == "check" and not self.server.supports_check:
            return make_envelope(
                "SUMMARY", [], {**STATUS_ERR, "Code": 14, "Msg": "Invalid command"}, when,
            )
        if cmd == "check":
            # cgminer's introspection command: Exists says whether the firmware
            # implements it, Access whether this caller may run it. Privileged
            # commands are refused unless api-allow grants W to the caller.
            target = str(parameter or "").strip().lower()
            known = {"summary", "stats", "version", "restart", "ascset",
                     "pause", "resume", "check", "config"}
            privileged = {"restart", "ascset", "pause", "resume", "quit"}
            exists = "Y" if target in known else "N"
            access = "N" if target in privileged and not self.server.allow_privileged else "Y"
            return make_envelope(
                "CHECK", [{"Exists": exists, "Access": access}],
                {**STATUS_OK, "Code": 72, "Msg": "Check command"}, when,
            )
        if cmd in ("ascset", "pause", "resume") and not self.server.supports_sleep:
            # Firmware with no software sleep at all: the command is unknown.
            return make_envelope(
                "SUMMARY", [], {**STATUS_ERR, "Code": 14, "Msg": "Invalid command"}, when,
            )
        if cmd in ("ascset", "pause", "resume", "restart") and not self.server.allow_privileged:
            return make_envelope(
                "SUMMARY", [],
                {**STATUS_ERR, "Code": 45, "Msg": f"Access denied to '{cmd}' command"}, when,
            )
        if cmd == "ascset":
            return self._ascset(parameter, state, when)
        if cmd == "pause":
            # bosminer-style pause: identical effect to ascset 0,sleep.
            state.sleep(state.now())
            return make_envelope("SUMMARY", [state.summary()], {**STATUS_OK, "Msg": "Paused"}, when)
        if cmd == "resume":
            state.wake(state.now())
            return make_envelope("SUMMARY", [state.summary()], {**STATUS_OK, "Msg": "Resumed"}, when)
        return make_envelope("SUMMARY", [], {**STATUS_ERR, "Msg": f"Unknown command: {cmd}"}, when)

    def _ascset(self, parameter, state, when):
        index, verb, _args = _parse_ascset(parameter)
        if verb is None:
            return make_envelope(
                "SUMMARY", [], {**STATUS_ERR, "Msg": f"Invalid ascset parameter: {parameter!r}"}, when
            )
        if verb == "sleep":
            state.sleep(state.now())
            msg = f"ASC {index} set sleep"
        elif verb == "wake":
            state.wake(state.now())
            msg = f"ASC {index} set wake"
        else:
            return make_envelope(
                "SUMMARY", [], {**STATUS_ERR, "Msg": f"Unknown ascset option: {verb}"}, when
            )
        # Real firmware answers ascset with an informational status.
        return make_envelope("SUMMARY", [state.summary()], {"STATUS": "I", "Code": 118, "Msg": msg}, when)

    def _send(self, resp, use_null):
        payload = json.dumps(resp, ensure_ascii=False)
        if use_null:
            payload += "\x00"
        try:
            self.wfile.write(payload.encode("utf-8"))
        except OSError:
            # Client hung up mid-reply; nothing useful to do.
            pass


class SimServer(socketserver.ThreadingTCPServer):
    # On Windows SO_REUSEADDR does not mean "reuse a TIME_WAIT socket", it
    # means "steal a port another process is actively listening on". Enabling
    # it there lets a second simulator bind the same port and answer half the
    # connections, which is maddening to debug. compat gates it per platform.
    allow_reuse_address = ALLOW_REUSE_ADDRESS
    daemon_threads = True

    def __init__(self, server_address, state, poll_secs=1.0, control_file=None,
                 use_null_terminator=True, allow_privileged=True, supports_check=True,
                 supports_sleep=True):
        # Assign attributes before binding/activating: ThreadingTCPServer can
        # begin accepting as soon as activation completes, and a handler that
        # arrives first would find self.state missing.
        self.state = state
        self.poll_secs = poll_secs
        self.control_file = control_file
        self.use_null_terminator = use_null_terminator
        #: Whether privileged commands are permitted, as api-allow controls on
        #: real firmware. A read-only API is a common and confusing default.
        self.allow_privileged = allow_privileged
        #: Bitmain's bmminer is a cgminer fork and several builds dropped the
        #: `check` introspection command entirely.
        self.supports_check = supports_check
        #: Stock Bitmain builds frequently implement no sleep at all.
        self.supports_sleep = supports_sleep
        self.sim_server = self
        super().__init__(server_address, MinerHandler)


def build_arg_parser():
    p = argparse.ArgumentParser(description="Fake Antminer TCP simulator (cgminer JSON API)")
    p.add_argument("--port", type=int, required=True, help="TCP port to bind")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p.add_argument("--stopped", action="store_true", help="Start in STOPPED state")
    p.add_argument("--sleeping", action="store_true", help="Start in SLEEPING state")
    p.add_argument("--ghs", type=float, default=13500, help="Default GHS (default: 13500)")
    p.add_argument("--restart-secs", type=float, default=10, help="Seconds in RESTARTING state (default: 10)")
    p.add_argument("--poll-secs", type=float, default=1.0, help="Control file poll interval (default: 1.0)")
    p.add_argument("--control-file", type=str, default=None, help="Path to control JSON file")
    p.add_argument("--seed", type=int, default=None, help="Random seed for deterministic output")
    p.add_argument("--no-null-terminator", action="store_true", help="Disable null-byte terminator")
    p.add_argument("--no-sleep-support", action="store_true",
                   help="Answer 'Invalid command' to sleep commands, as stock builds do")
    p.add_argument("--no-check-command", action="store_true",
                   help="Answer 'Invalid command' to `check`, as bmminer builds do")
    p.add_argument("--read-only", action="store_true",
                   help="Refuse privileged commands, as api-allow without W does")
    p.add_argument("--stock", action="store_true",
                   help="Emit only the fields stock bmminer sends (no Status/state in SUMMARY)")
    return p


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    now = time.time
    state = SimState(ghs=args.ghs, restart_secs=args.restart_secs, now=now,
                     seed=args.seed, stock=args.stock)

    explicit_state = args.stopped or args.sleeping
    if args.stopped:
        state.set_state(SimState.STOPPED, now())
    elif args.sleeping:
        state.set_state(SimState.SLEEPING, now())

    control_file = args.control_file
    if control_file is None:
        control_file = control_file_path(args.port)
    # An explicit start state on the command line beats a stale control file
    # left behind by a previous run.
    ensure_control_file(control_file, state, force=explicit_state)

    use_null = not args.no_null_terminator
    server = SimServer(
        (args.host, args.port),
        state,
        poll_secs=args.poll_secs,
        control_file=control_file,
        use_null_terminator=use_null,
        allow_privileged=not args.read_only,
        supports_check=not args.no_check_command,
        supports_sleep=not args.no_sleep_support,
    )
    addr = server.server_address
    print(f"miner_sim listening on {addr[0]}:{addr[1]} state={state.state} null_terminator={use_null}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
