"""S7 test support: a python-snap7 server on a free port and a fake transport.

Loaded as a pytest plugin from ``conftest.py``.

``S7TestServer`` wraps ``snap7.server.Server`` (DB, M, I and Q areas), which
answers a Read Var item with return code 0x0A for an area that is not
registered and 0x05 for a range outside a registered one. It never sends a
header error or 0x03, so those cases use ``FakeTransport``: it parses the
request bytes itself, answers from its own memory and checks that requests and
responses fit the PDU it reports.
"""

from __future__ import annotations

import asyncio
import struct
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest_asyncio
from net_support import free_port
from snap7.server import Server
from snap7.type import SrvArea, SrvEvent

AREA_I = 0x81
AREA_Q = 0x82
AREA_M = 0x83
AREA_DB = 0x84

# python-snap7 server's answer to the order-code SZL request.
SERVER_ORDER_CODE = "6ES7 315-2EH14-0AB0"

_SERVER_AREAS = {
    AREA_DB: SrvArea.DB,
    AREA_M: SrvArea.MK,
    AREA_I: SrvArea.PE,
    AREA_Q: SrvArea.PA,
}


class S7TestServer:
    """python-snap7 server on a free local port; set memory before ``start()``."""

    def __init__(self) -> None:
        """Create a stopped server with no memory areas."""
        self.port = free_port()
        self.endpoint = f"tcp://127.0.0.1:{self.port}"
        self._areas: dict[tuple[int, int], bytearray] = {}
        self._server: Server | None = None
        self._release = threading.Event()
        self._release.set()

    def set_area(self, area_code: int, data: bytes, *, db: int = 0) -> None:
        """Register DB ``db`` (``AREA_DB``) or the M/I/Q area with ``data``."""
        if self._server is not None:
            raise RuntimeError("Set memory before start().")
        self._areas[(area_code, db)] = bytearray(data)

    def stall_reads(self) -> None:
        """Make the server hold every read until ``release_reads()``."""
        self._release.clear()

    def release_reads(self) -> None:
        """Let held reads answer."""
        self._release.set()

    def _on_read(self, _event: SrvEvent) -> None:
        self._release.wait(timeout=10)

    async def start(self) -> None:
        """Start listening; ``start()`` after ``stop()`` reuses the port."""
        if self._server is not None:
            return
        server = Server(log=False)
        for (area_code, db), data in self._areas.items():
            server.register_area(_SERVER_AREAS[area_code], db, data)
        server.set_read_events_callback(self._on_read)
        server.start(tcp_port=self.port)
        self._server = server

    async def stop(self) -> None:
        """Stop listening and end the client handlers."""
        self._release.set()
        server, self._server = self._server, None
        if server is not None:
            await asyncio.to_thread(server.destroy)


@pytest_asyncio.fixture
async def s7_server() -> AsyncIterator[S7TestServer]:
    """Yield a stopped server; set memory, then ``await server.start()``."""
    server = S7TestServer()
    try:
        yield server
    finally:
        await server.stop()


@dataclass(frozen=True)
class FakeItem:
    """One Read Var item as the fake transport parsed it from the request."""

    area_code: int
    db: int
    start: int
    length: int


_JOB = struct.Struct(">BBHHHH")
_ITEM = struct.Struct(">BBBBHHBBH")
_ACK_DATA = struct.Struct(">BBHHHHBB")


def parse_request(pdu: bytes) -> tuple[int, list[FakeItem]]:
    """Return the pdu_ref and items of a Read Var request (byte addressing)."""
    _, _, _, pdu_ref, param_length, _ = _JOB.unpack_from(pdu)
    function, count = pdu[_JOB.size], pdu[_JOB.size + 1]
    assert function == 0x04, f"not a Read Var request: {function:#x}"
    assert param_length == 2 + 12 * count
    items = []
    for index in range(count):
        fields = _ITEM.unpack_from(pdu, _JOB.size + 2 + 12 * index)
        _, _, _, transport, length, db, area, high, low = fields
        assert transport == 0x02, "items use transport size BYTE"
        bit_address = (high << 16) | low
        assert bit_address % 8 == 0, "items start on a byte"
        items.append(FakeItem(area, db, bit_address // 8, length))
    return pdu_ref, items


def build_response(
    pdu_ref: int, answers: list[tuple[int, bytes]], *, header_error: int = 0
) -> bytes:
    """Build an ACK_DATA Read Var response; ``answers`` is (return code, data)."""
    if header_error:
        return _ACK_DATA.pack(
            0x32, 0x03, 0, pdu_ref, 0, 0, header_error >> 8, header_error & 0xFF
        )
    data = bytearray()
    for index, (code, payload) in enumerate(answers):
        if code != 0xFF:
            data += bytes((code, 0x00, 0x00, 0x00))
            continue
        data += struct.pack(">BBH", code, 0x04, len(payload) * 8) + payload
        if len(payload) % 2 and index < len(answers) - 1:
            data += b"\x00"
    params = bytes((0x04, len(answers)))
    header = _ACK_DATA.pack(0x32, 0x03, 0, pdu_ref, len(params), len(data), 0, 0)
    return header + params + bytes(data)


Refuse = Callable[[FakeItem], int | None]


@dataclass
class FakeTransport:
    """Scripted ``S7Transport`` answering from ``memory`` keyed (area, db).

    ``refuse(item)`` may return a return code for an item; an area that is not
    in ``memory`` answers 0x0A and a range outside it 0x05. ``header_error``
    (class << 8 | code) rejects every request; ``fail`` is raised by every
    exchange; ``cpu_error`` is raised by ``cpu_description`` and leaves a late
    answer for the next exchange.
    """

    memory: dict[tuple[int, int], bytes] = field(default_factory=dict)
    pdu_length: int = 480
    cpu: str | None = "6ES7 214-1AG40-0XB0"
    refuse: Refuse | None = None
    header_error: int = 0
    fail: BaseException | None = None
    connect_error: BaseException | None = None
    cpu_error: BaseException | None = None
    connect_delay_s: float = 0.0
    # After a failed CPU query a late answer sits in the socket: the next
    # exchange returns it, until connect() opens a clean link.
    stale: bool = False
    connects: int = 0
    closes: int = 0
    cpu_requests: int = 0
    requests: list[list[FakeItem]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    async def connect(
        self, host: str, port: int, rack: int, slot: int, timeout_s: float
    ) -> int:
        self.connects += 1
        if self.connect_delay_s:
            await asyncio.sleep(self.connect_delay_s)
        if self.connect_error is not None:
            raise self.connect_error
        self.stale = False
        return self.pdu_length

    async def exchange(self, pdu: bytes, timeout_s: float) -> bytes:
        assert len(pdu) <= self.pdu_length, "request larger than the PDU"
        pdu_ref, items = parse_request(pdu)
        self.requests.append(items)
        self.timeouts.append(timeout_s)
        if self.fail is not None:
            raise self.fail
        if self.stale:
            self.stale = False
            return bytes.fromhex("3207000000010000000c")  # an SZL (user data) answer
        if self.header_error:
            return build_response(pdu_ref, [], header_error=self.header_error)
        response = build_response(pdu_ref, [self._answer(item) for item in items])
        assert len(response) <= self.pdu_length, "response larger than the PDU"
        return response

    def _answer(self, item: FakeItem) -> tuple[int, bytes]:
        if self.refuse is not None:
            code = self.refuse(item)
            if code is not None:
                return code, b""
        data = self.memory.get((item.area_code, item.db))
        if data is None:
            return 0x0A, b""
        if item.start + item.length > len(data):
            return 0x05, b""
        return 0xFF, bytes(data[item.start : item.start + item.length])

    async def close(self) -> None:
        self.closes += 1

    async def cpu_description(self, timeout_s: float) -> str | None:
        self.cpu_requests += 1
        if self.cpu_error is not None:
            self.stale = True
            raise self.cpu_error
        return self.cpu
