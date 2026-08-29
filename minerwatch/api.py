"""Async client for the cgminer/bmminer JSON-over-TCP API.

Every component that talks to a miner socket goes through here so that the
framing rules live in exactly one place.

The protocol is line-less: the client writes a JSON object terminated by a NUL
byte and the miner answers with a JSON object, usually also NUL-terminated.
Two details bite naive implementations:

* A single ``reader.read(n)`` is **not** a message. TCP may split the reply
  across segments, and on a loopback connection under Windows the split is
  common enough to see in practice. The reply must be accumulated until the
  terminator arrives (or the peer closes).
* Some firmwares — and the project's own simulator when run with
  ``--no-null-terminator`` — omit the NUL and rely on connection close to
  delimit the message, so EOF has to be accepted as a valid frame end.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: cgminer's frame delimiter.
NUL = b"\x00"

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 5.0
#: Generous upper bound; a ``stats`` reply from a big miner is a few KiB.
MAX_RESPONSE_BYTES = 1 << 20


class ApiError(Exception):
    """A miner returned a well-formed reply reporting failure."""


def encode_command(command: str, parameter: str | None = None) -> bytes:
    """Serialise a cgminer command to its wire form.

    ``parameter`` is omitted entirely when ``None`` — some firmwares reject a
    request that carries an empty ``parameter`` key.
    """
    payload: dict[str, Any] = {"command": command}
    if parameter is not None:
        payload["parameter"] = parameter
    return json.dumps(payload).encode("utf-8") + NUL


async def read_frame(reader: asyncio.StreamReader, timeout: float = DEFAULT_READ_TIMEOUT) -> bytes:
    """Read one NUL-terminated frame, tolerating firmwares that omit the NUL.

    The whole read is bounded by a single *timeout* budget rather than a
    per-chunk one, so a peer dribbling one byte at a time cannot hold the poll
    loop open indefinitely.
    """

    async def _read() -> bytes:
        buf = bytearray()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                # EOF: the peer closed, which itself delimits the message.
                return bytes(buf)
            buf += chunk
            idx = buf.find(NUL)
            if idx != -1:
                return bytes(buf[:idx])
            if len(buf) > MAX_RESPONSE_BYTES:
                raise ApiError(f"response exceeded {MAX_RESPONSE_BYTES} bytes without terminator")

    return await asyncio.wait_for(_read(), timeout=timeout)


async def request(
    host: str,
    port: int,
    command: str,
    parameter: str | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
) -> bytes:
    """Send one command and return the raw (NUL-stripped) reply bytes.

    Raises on connection, timeout, or framing failure; callers decide how to
    classify those.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=connect_timeout
    )
    try:
        writer.write(encode_command(command, parameter))
        await asyncio.wait_for(writer.drain(), timeout=read_timeout)
        return await read_frame(reader, timeout=read_timeout)
    finally:
        await close_writer(writer)


async def close_writer(writer: asyncio.StreamWriter, timeout: float = 5.0) -> None:
    """Close a stream writer without ever raising.

    ``wait_closed()`` on the Windows Proactor transport can raise
    ``ConnectionResetError`` when the peer has already gone away, which would
    otherwise mask the real error from the caller's ``try`` block.
    """
    try:
        writer.close()
    except Exception:  # pragma: no cover - transport dependent
        return
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=timeout)
    except (Exception, asyncio.CancelledError):  # pragma: no cover
        pass


def parse_response(raw: bytes) -> dict:
    """Decode a reply body into a dict, raising :class:`ApiError` if it is not one."""
    try:
        data = json.loads(raw.rstrip(NUL).decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ApiError(f"unparseable response: {raw[:200]!r}") from exc
    if not isinstance(data, dict):
        raise ApiError(f"unexpected response type: {type(data).__name__}")
    return data


def check_status(data: dict) -> tuple[bool, str]:
    """Interpret a cgminer ``STATUS`` envelope.

    Returns ``(ok, message)``. ``STATUS`` is a list whose first entry carries a
    single-letter code: ``S`` success, ``I`` informational, ``W`` warning,
    ``E`` error, ``F`` fatal. ``I`` is treated as success because several
    firmwares answer ``ascset`` with an informational status on success.
    """
    status_list = data.get("STATUS")
    if isinstance(status_list, str):
        # A few firmwares flatten STATUS to a bare string.
        return status_list in ("S", "I"), data.get("Msg", "")
    if not isinstance(status_list, list) or not status_list:
        return False, f"unexpected response structure: {data}"
    entry = status_list[0]
    if not isinstance(entry, dict):
        return False, f"unexpected STATUS entry: {entry!r}"
    code = entry.get("STATUS")
    msg = entry.get("Msg") or entry.get("Description") or ""
    if code in ("S", "I"):
        return True, msg or "ok"
    return False, msg or f"status {code!r}"
