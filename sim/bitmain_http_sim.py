"""Fake Bitmain stock-firmware web interface.

Implements just enough of the S17/S19 CGI surface to exercise the
``bitmain_http`` sleep backend end to end:

* ``GET  /cgi-bin/get_miner_conf.cgi`` -> the current miner configuration JSON
* ``POST /cgi-bin/set_miner_conf.cgi`` -> replace it (this is how ``miner-mode``
  is switched between 0 = normal and 1 = sleep)

Both endpoints sit behind HTTP Digest authentication, as the real firmware
does, because digest is the part most likely to break silently in a client.

Run it standalone::

    python sim/bitmain_http_sim.py --port 8080 --username root --password root
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Put the repository root on sys.path before importing the package: running
# these files as plain scripts (``python sim/miner_sim.py``) puts only ``sim/``
# on the path. Both spellings are needed because which one resolves depends on
# whether the file was launched as a script or as ``python -m sim.<module>``.
try:
    from sim import _bootstrap  # noqa: F401
except ImportError:  # pragma: no cover - script-launch path
    import _bootstrap  # noqa: F401

from minerwatch.compat import ALLOW_REUSE_ADDRESS, write_text_atomic

REALM = "antMiner Configuration"
OPAQUE = "5ccc069c403ebaf9f0171e9517f40e41"

#: Shape of a real S19 miner configuration, trimmed to the fields that matter.
DEFAULT_CONF = {
    "pools": [
        {"url": "stratum+tcp://pool.example:3333", "user": "worker.1", "pass": "x"},
    ],
    "api-listen": True,
    "api-network": True,
    "api-groups": "A:stats:pools:devs:summary:version",
    "api-allow": "A:0/0,W:*",
    "bitmain-fan-ctrl": False,
    "bitmain-fan-pwm": "100",
    "freq-level": "100",
    "miner-mode": 0,
    "bitmain-work-mode": "0",
}


#: miner-mode values, as Bitmain stock firmware defines them.
MODE_NORMAL = 0
MODE_SLEEP = 1

#: How each miner-mode maps onto the TCP simulator's state machine.
_MODE_TO_STATE = {MODE_NORMAL: "mining", MODE_SLEEP: "sleeping"}


class AsymmetricConf:
    """Models the S19 XP captured in the field.

    Reads back "bitmain-work-mode" as a string; only honours a write that
    carries the mode under "miner-mode". A document echoed back under the
    read-side name is accepted, answered "OK!", and discarded — which is what
    made this so hard to see.
    """

    READ_KEY = "bitmain-work-mode"
    WRITE_KEY = "miner-mode"

    def __init__(self, conf: dict | None = None, require_content_type: str | None = None):
        self._lock = threading.RLock()
        self._conf = json.loads(json.dumps(conf if conf is not None else {
            "pools": [{"url": "stratum+tcp://pool.example.com:3333",
                       "user": "account.worker1", "pass": "123"}],
            "bitmain-fan-ctrl": False,
            "bitmain-fan-pwm": "100",
            "bitmain-work-mode": "0",
            "bitmain-user-ip-cat": None,
        }))
        self.writes: list[dict] = []
        self.require_content_type = require_content_type
        self.control_file = None
        self.last_content_type: str | None = None

    def get(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._conf))

    def set(self, conf: dict) -> None:
        with self._lock:
            self.writes.append(conf)
            if self.require_content_type and self.last_content_type != self.require_content_type:
                return                      # wrong Content-Type: silently ignored
            if self.WRITE_KEY not in conf:
                return                      # wrong field name: silently ignored
            self._conf[self.READ_KEY] = str(int(conf[self.WRITE_KEY]))
            for key, value in conf.items():
                if key not in (self.WRITE_KEY,):
                    self._conf[key] = value

    @property
    def miner_mode(self) -> int:
        with self._lock:
            try:
                return int(self._conf.get(self.READ_KEY, 0))
            except (TypeError, ValueError):
                return 0


class MinerConf:
    """Thread-safe holder for the simulated miner configuration.

    Optionally *linked* to a running TCP simulator: on a real S19 the web UI and
    the cgminer API are two faces of one machine, so setting ``miner-mode`` in
    the UI stops the hashrate the API reports. Two unlinked simulator processes
    do not behave that way, which makes an end-to-end ``bitmain_http`` demo look
    like a failure — MinerWatch sets the mode, keeps seeing full hashrate, and
    correctly concludes the sleep never took effect.

    The link is the TCP simulator's existing control file: writing
    ``{"state": "sleeping"}`` to it is applied on that simulator's next request.
    """

    def __init__(self, conf: dict | None = None, control_file: str | None = None):
        self._lock = threading.RLock()
        self._conf = json.loads(json.dumps(conf if conf is not None else DEFAULT_CONF))
        #: Every accepted POST, so a test can assert what the client sent.
        self.writes: list[dict] = []
        self.control_file = control_file
        if control_file:
            self._publish(self.miner_mode)

    def get(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._conf))

    def set(self, conf: dict) -> None:
        with self._lock:
            before = self.miner_mode
            self._conf = json.loads(json.dumps(conf))
            self.writes.append(self._conf)
            after = self.miner_mode
        if self.control_file and after != before:
            self._publish(after)

    def _publish(self, mode: int) -> None:
        """Push the current mode onto the linked TCP simulator."""
        state = _MODE_TO_STATE.get(mode)
        if state is None:
            return
        try:
            write_text_atomic(self.control_file, json.dumps({"state": state}))
        except OSError:
            # A simulator is not worth crashing over a control-file write.
            pass

    @property
    def miner_mode(self) -> int:
        with self._lock:
            try:
                return int(self._conf.get("miner-mode", 0))
            except (TypeError, ValueError):
                return 0


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _parse_auth_header(header: str) -> dict[str, str]:
    """Parse a ``Digest k=v, k="v"`` header into a dict.

    Hand-rolled rather than regex-per-field because clients differ on which
    values they quote (``qop=auth`` and ``nc=00000001`` are conventionally
    unquoted, everything else quoted).
    """
    if not header or not header.strip().lower().startswith("digest "):
        return {}
    out: dict[str, str] = {}
    body = header.strip()[len("digest "):]
    for part in _split_top_level(body):
        field = part.strip()
        if not field or "=" not in field:
            continue
        key, _, value = field.partition("=")
        out[key.strip().lower()] = value.strip().strip('"')
    return out


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside double quotes."""
    parts, buf, in_quotes = [], [], False
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def expected_digest_response(
    username: str, password: str, method: str, params: dict[str, str]
) -> str:
    """Compute the digest a compliant client should send, per RFC 2617."""
    ha1 = _md5(f"{username}:{REALM}:{password}")
    ha2 = _md5(f"{method}:{params.get('uri', '')}")
    qop = params.get("qop")
    if qop:
        return _md5(
            f"{ha1}:{params.get('nonce','')}:{params.get('nc','')}:"
            f"{params.get('cnonce','')}:{qop}:{ha2}"
        )
    return _md5(f"{ha1}:{params.get('nonce','')}:{ha2}")


class BitmainHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AntMinerSim/1.0"

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _challenge(self) -> None:
        nonce = _md5(f"{time.time()}:{REALM}")
        self.send_response(401)
        self.send_header(
            "WWW-Authenticate",
            f'Digest realm="{REALM}", qop="auth", nonce="{nonce}", opaque="{OPAQUE}"',
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authenticated(self) -> bool:
        params = _parse_auth_header(self.headers.get("Authorization", ""))
        if not params:
            return False
        srv = self.server
        expected = expected_digest_response(srv.username, srv.password, self.command, params)
        if params.get("username") != srv.username:
            return False
        # Constant-time compare is overkill for a simulator but costs nothing.
        return _consteq(params.get("response", ""), expected)

    # -- routes -------------------------------------------------------------

    def do_GET(self):  # noqa: N802 - stdlib naming
        if not self._authenticated():
            self._challenge()
            return
        if self.path.startswith("/cgi-bin/get_miner_conf.cgi"):
            self._send_json(200, self.server.conf.get())
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - stdlib naming
        if not self._authenticated():
            # Read and discard the body so the connection stays usable for the
            # authenticated retry.
            self._read_body()
            self._challenge()
            return
        body = self._read_body()
        # Record what the client claimed to be sending; some firmware checks.
        if hasattr(self.server.conf, "last_content_type"):
            self.server.conf.last_content_type = self.headers.get("Content-Type")
        if not self.path.startswith("/cgi-bin/set_miner_conf.cgi"):
            self._send_json(404, {"error": "not found"})
            return
        try:
            conf = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send_json(400, {"stats": "failed", "error": "malformed JSON"})
            return
        if not isinstance(conf, dict):
            self._send_json(400, {"stats": "failed", "error": "expected an object"})
            return
        self.server.conf.set(conf)
        self._send_json(200, {"stats": "success", "code": "M000"})

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return b""
        return self.rfile.read(length) if length > 0 else b""


def _consteq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


class BitmainHttpServer(ThreadingHTTPServer):
    # Same Windows caveat as the TCP simulator: SO_REUSEADDR there permits
    # hijacking a live port rather than reusing a TIME_WAIT one.
    allow_reuse_address = ALLOW_REUSE_ADDRESS
    daemon_threads = True

    def __init__(self, address, conf=None, username="root", password="root", verbose=False,
                 control_file=None):
        self.conf = conf if conf is not None else MinerConf(control_file=control_file)
        self.username = username
        self.password = password
        self.verbose = verbose
        super().__init__(address, BitmainHandler)


def build_arg_parser():
    p = argparse.ArgumentParser(description="Fake Bitmain stock-firmware web UI")
    p.add_argument("--port", type=int, required=True, help="TCP port to bind")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p.add_argument("--username", default="root")
    p.add_argument("--password", default="root")
    p.add_argument("--miner-mode", type=int, default=0, help="Initial miner-mode (0 normal, 1 sleep)")
    p.add_argument("--linked-port", type=int, default=None,
                   help="Port of a miner_sim to drive, so miner-mode actually stops its hashrate")
    p.add_argument("--control-file", type=str, default=None,
                   help="Explicit control file of the miner_sim to drive (overrides --linked-port)")
    p.add_argument("--verbose", action="store_true", help="Log every request")
    return p


def main():
    args = build_arg_parser().parse_args()
    control_file = args.control_file
    if control_file is None and args.linked_port is not None:
        from sim.miner_sim import control_file_path

        control_file = control_file_path(args.linked_port)

    conf = MinerConf({**DEFAULT_CONF, "miner-mode": args.miner_mode}, control_file=control_file)
    server = BitmainHttpServer(
        (args.host, args.port),
        conf=conf,
        username=args.username,
        password=args.password,
        verbose=args.verbose,
    )
    addr = server.server_address
    link = f" linked to {control_file}" if control_file else ""
    print(
        f"bitmain_http_sim listening on {addr[0]}:{addr[1]} "
        f"miner-mode={conf.miner_mode}{link}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
