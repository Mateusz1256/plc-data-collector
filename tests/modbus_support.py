"""Modbus test support: an in-process Modbus TCP server with two units.

Loaded as a pytest plugin from ``conftest.py``. The server is built from
pymodbus ``SimDevice`` objects, whose addresses are zero-based like the wire
protocol, so ``set_registers(1, "hr", 0, [7])`` is read back with ``hr:0``.
Addresses that were never set answer Modbus exception 02.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import pytest_asyncio
from pymodbus.pdu import ModbusPDU
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

UNITS = (1, 2)
_AREAS = ("co", "di", "hr", "ir")
_BIT_AREAS = ("co", "di")
_LAST_ADDRESS = 65535


@dataclass(frozen=True, slots=True)
class ServerRequest:
    """One request the server received."""

    unit: int
    function_code: int
    address: int
    count: int


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ModbusTestServer:
    """Modbus TCP server on a free local port with units 1 and 2."""

    def __init__(self) -> None:
        """Create a stopped server with empty data."""
        self.port = free_port()
        self.endpoint = f"tcp://127.0.0.1:{self.port}"
        self.requests: list[ServerRequest] = []
        self._data: dict[tuple[int, str], dict[int, int | bool]] = {}
        self._server: ModbusTcpServer | None = None

    def set_registers(
        self, unit: int, area: str, address: int, values: Sequence[int]
    ) -> None:
        """Define holding (``hr``) or input (``ir``) registers; call before start."""
        self._set(unit, area, address, [int(v) & 0xFFFF for v in values])

    def set_bits(
        self, unit: int, area: str, address: int, values: Sequence[bool]
    ) -> None:
        """Define coils (``co``) or discrete inputs (``di``); call before start."""
        self._set(unit, area, address, [bool(v) for v in values])

    def _set(
        self, unit: int, area: str, address: int, values: Sequence[int | bool]
    ) -> None:
        if unit not in UNITS or area not in _AREAS:
            raise ValueError(f"unknown unit/area: {unit}/{area}")
        if self._server is not None:
            raise RuntimeError("Set values before start().")
        store = self._data.setdefault((unit, area), {})
        for offset, value in enumerate(values):
            store[address + offset] = value

    def _device(self, unit: int) -> SimDevice:
        blocks: list[list[SimData]] = []
        for area in _AREAS:
            is_bits = area in _BIT_AREAS
            kind = DataType.BITS if is_bits else DataType.REGISTERS
            # A far-away filler keeps every block non-empty; gaps stay invalid.
            filler = SimData(
                _LAST_ADDRESS, values=False if is_bits else 0, datatype=kind
            )
            entries = [
                SimData(address, values=[value], datatype=kind)
                for address, value in sorted(self._data.get((unit, area), {}).items())
            ]
            blocks.append([*entries, filler])
        return SimDevice(unit, simdata=(blocks[0], blocks[1], blocks[2], blocks[3]))

    def _trace(self, sending: bool, pdu: ModbusPDU) -> ModbusPDU:
        if not sending:  # a request arriving at the server
            self.requests.append(
                ServerRequest(
                    unit=pdu.dev_id,
                    function_code=pdu.function_code,
                    address=pdu.address,
                    count=pdu.count,
                )
            )
        return pdu

    async def start(self) -> None:
        """Start listening; the same port is reused after ``stop()``."""
        if self._server is not None:
            return
        server = ModbusTcpServer(
            [self._device(unit) for unit in UNITS],
            address=("127.0.0.1", self.port),
            trace_pdu=self._trace,
        )
        await server.serve_forever(background=True)
        self._server = server

    async def stop(self) -> None:
        """Stop listening and drop every client connection."""
        server, self._server = self._server, None
        if server is not None:
            await server.shutdown()  # type: ignore[no-untyped-call]


@pytest_asyncio.fixture
async def modbus_server() -> AsyncIterator[ModbusTestServer]:
    """Yield a stopped server; set values, then ``await server.start()``."""
    server = ModbusTestServer()
    try:
        yield server
    finally:
        await server.stop()
