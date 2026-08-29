"""Tests for the shared cgminer transport.

The framing rules are the part most likely to break under a different TCP
stack, so they are exercised directly rather than only through the simulator.
"""

import asyncio
import json

import pytest

from minerwatch import api


class FakeReader:
    """StreamReader stand-in that hands out preset chunks.

    Used to reproduce a reply split across TCP segments, which is what a single
    ``reader.read()`` gets wrong and what the loopback stack on Windows
    produces often enough to matter.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n=-1):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class SlowReader(FakeReader):
    async def read(self, n=-1):
        await asyncio.sleep(10)
        return b""


class TestEncodeCommand:
    def test_no_parameter_key_when_none(self):
        # Some firmwares reject a request carrying an empty "parameter".
        raw = api.encode_command("summary")
        assert raw.endswith(b"\x00")
        assert json.loads(raw[:-1]) == {"command": "summary"}

    def test_parameter_included(self):
        raw = api.encode_command("ascset", "0,sleep")
        assert json.loads(raw[:-1]) == {"command": "ascset", "parameter": "0,sleep"}


class TestReadFrame:
    async def test_reassembles_split_frame(self):
        body = b'{"STATUS":[{"STATUS":"S"}]}'
        reader = FakeReader([body[:10], body[10:], b"\x00"])
        assert await api.read_frame(reader) == body

    async def test_stops_at_terminator_ignoring_trailing_bytes(self):
        reader = FakeReader([b'{"a":1}\x00garbage-after'])
        assert await api.read_frame(reader) == b'{"a":1}'

    async def test_eof_terminates_frame_without_null(self):
        # --no-null-terminator mode, and some real firmwares.
        reader = FakeReader([b'{"a":1}', b""])
        assert await api.read_frame(reader) == b'{"a":1}'

    async def test_oversize_without_terminator_is_rejected(self):
        chunk = b"x" * 65536
        reader = FakeReader([chunk] * ((api.MAX_RESPONSE_BYTES // 65536) + 2))
        with pytest.raises(api.ApiError, match="without terminator"):
            await api.read_frame(reader)

    async def test_timeout_is_bounded_overall(self):
        with pytest.raises(asyncio.TimeoutError):
            await api.read_frame(SlowReader([]), timeout=0.05)


class TestParseResponse:
    def test_strips_terminator(self):
        assert api.parse_response(b'{"a":1}\x00') == {"a": 1}

    def test_non_json_raises(self):
        with pytest.raises(api.ApiError, match="unparseable"):
            api.parse_response(b"not json")

    def test_non_object_raises(self):
        with pytest.raises(api.ApiError, match="unexpected response type"):
            api.parse_response(b"[1,2,3]")

    def test_invalid_utf8_is_replaced_not_fatal(self):
        # A truncated multibyte sequence should surface as a parse error, not a
        # UnicodeDecodeError escaping to the caller.
        with pytest.raises(api.ApiError):
            api.parse_response(b'{"a": "\xff\xfe"')


class TestCheckStatus:
    def test_success(self):
        ok, msg = api.check_status({"STATUS": [{"STATUS": "S", "Msg": "OK"}]})
        assert ok and msg == "OK"

    def test_informational_counts_as_success(self):
        # ascset acknowledgements come back as "I" on several firmwares.
        ok, _ = api.check_status({"STATUS": [{"STATUS": "I", "Msg": "ASC 0 set sleep"}]})
        assert ok

    def test_error(self):
        ok, msg = api.check_status({"STATUS": [{"STATUS": "E", "Msg": "unknown command"}]})
        assert not ok and msg == "unknown command"

    def test_flattened_status_string(self):
        assert api.check_status({"STATUS": "S", "Msg": "fine"})[0] is True
        assert api.check_status({"STATUS": "E", "Msg": "nope"})[0] is False

    def test_missing_status(self):
        ok, msg = api.check_status({"SUMMARY": []})
        assert not ok and "unexpected response structure" in msg

    def test_non_dict_entry(self):
        ok, msg = api.check_status({"STATUS": ["S"]})
        assert not ok and "unexpected STATUS entry" in msg


class TestRequest:
    async def test_round_trip_against_a_real_socket(self, unused_tcp_port_factory=None):
        """Talk to a throwaway asyncio server to cover connect/write/read/close."""
        received = []

        async def handle(reader, writer):
            received.append(await reader.readuntil(b"\x00"))
            writer.write(b'{"STATUS":[{"STATUS":"S","Msg":"OK"}]}\x00')
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            raw = await api.request("127.0.0.1", port, "summary")
        assert json.loads(received[0][:-1]) == {"command": "summary"}
        assert api.check_status(api.parse_response(raw))[0] is True

    async def test_connection_refused_propagates(self):
        # Port 1 on loopback is not listening; the caller classifies the error.
        with pytest.raises((OSError, asyncio.TimeoutError)):
            await api.request("127.0.0.1", 1, "summary", connect_timeout=1, read_timeout=1)
