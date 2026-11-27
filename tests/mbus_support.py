"""M-Bus test support: the pyMeterBus oracle, the simulator and a pty pair.

Loaded as a pytest plugin from ``conftest.py``.

* ``oracle_compare`` checks the records the codec decodes from one long frame
  against pyMeterBus (a test-only dependency). pyMeterBus scales values with
  floats, so values agree within 1e-9 relative; the codec's own tests pin the
  exact decimals.
* ``documented_meters`` / ``documented_records`` parse the simulator's
  hand-written tables (between ``<!-- meters:begin -->`` and
  ``<!-- meters:end -->`` in ``simulators/mbus.py``), the independent
  reference the driver and demo tests check against.
* ``mbus_sim`` serves the five demo meters over TCP on a free port, with a
  clock the test moves.
* ``MemoryTransport`` puts a ``SimulatedBus`` behind the transport interface
  without a socket: silence costs no time.
* ``BusHost`` is the part of the collector host the M-Bus Panel actions use;
  ``running_host`` pretends a worker runs on a memory transport.
* ``pty_pair`` opens a raw pseudo-terminal pair (POSIX only).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import meterbus  # type: ignore[import-untyped]
import pytest_asyncio
from net_support import free_port

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.mbus_scan import ScanRegistry
from plc_data_collector.domain import ConnectionConfig
from plc_data_collector.protocols import DriverRegistry, build_driver_registry
from plc_data_collector.protocols.mbus import MBusDriver
from plc_data_collector.protocols.mbus_bus import BusTransportError, MBusBus
from plc_data_collector.protocols.mbus_codec import (
    Frame,
    FrameReader,
    LongFrame,
    MBusRecord,
    ShortFrame,
    parse_rsp_ud,
)
from plc_data_collector.protocols.mbus_tags import Function, Quantity, convert
from plc_data_collector.simulators import mbus as simulator

__all__ = [
    "BusHost",
    "DocumentedMeter",
    "DocumentedRecord",
    "FakeClock",
    "MBusSimServer",
    "MemoryTransport",
    "documented_meters",
    "documented_records",
    "free_port",
    "long_frames",
    "mbus_connection",
    "mbus_sim",
    "memory_host",
    "memory_transport",
    "oracle_compare",
    "pty_pair",
    "running_host",
]

# Oracle

_RELATIVE = Decimal("1e-9")
_FLOAT32 = Decimal(2) ** -23
_DIF_REAL = 0x5
# pyMeterBus drops records without data (DIF data field 0x0 and 0x8).
_NO_DATA_FIELDS = frozenset({0x0, 0x8})

# pyMeterBus record type -> (quantity, the unit its value is expressed in).
_TYPES: dict[str, tuple[Quantity, str]] = {
    "VIFUnit.ENERGY_WH": (Quantity.ENERGY, "Wh"),
    "VIFUnit.ENERGY_J": (Quantity.ENERGY, "J"),
    "VIFUnit.VOLUME": (Quantity.VOLUME, "m3"),
    "VIFUnit.MASS": (Quantity.MASS, "kg"),
    "VIFUnit.ON_TIME": (Quantity.ON_TIME, "s"),
    "VIFUnit.OPERATING_TIME": (Quantity.OPERATING_TIME, "s"),
    "VIFUnit.POWER_W": (Quantity.POWER, "W"),
    "VIFUnit.POWER_J_H": (Quantity.POWER, "J/h"),
    "VIFUnit.VOLUME_FLOW": (Quantity.VOLUME_FLOW, "m3/h"),
    "VIFUnit.VOLUME_FLOW_EXT": (Quantity.VOLUME_FLOW, "m3/min"),
    "VIFUnit.VOLUME_FLOW_EXT_S": (Quantity.VOLUME_FLOW, "m3/s"),
    "VIFUnit.MASS_FLOW": (Quantity.MASS_FLOW, "kg/h"),
    "VIFUnit.FLOW_TEMPERATURE": (Quantity.FLOW_TEMPERATURE, "C"),
    "VIFUnit.RETURN_TEMPERATURE": (Quantity.RETURN_TEMPERATURE, "C"),
    "VIFUnit.TEMPERATURE_DIFFERENCE": (Quantity.TEMPERATURE_DIFFERENCE, "K"),
    "VIFUnit.EXTERNAL_TEMPERATURE": (Quantity.EXTERNAL_TEMPERATURE, "C"),
    "VIFUnit.PRESSURE": (Quantity.PRESSURE, "bar"),
    "VIFUnit.UNITS_FOR_HCA": (Quantity.HCA_UNITS, ""),
    "VIFUnitExt.VOLTS": (Quantity.VOLTAGE, "V"),
    "VIFUnitExt.AMPERE": (Quantity.CURRENT, "A"),
}
# VIF 0xFB energy: pyMeterBus types both rows "Energy"; MWh is unit Wh and GJ
# is unit "Reserved", both scaled to the base unit.
_FB_ENERGY = {"MeasureUnit.WH": "Wh", "Reserved": "J"}

_FUNCTIONS = {
    "FunctionType.INSTANTANEOUS_VALUE": Function.INSTANTANEOUS,
    "FunctionType.MAXIMUM_VALUE": Function.MAXIMUM,
    "FunctionType.MINIMUM_VALUE": Function.MINIMUM,
    "FunctionType.ERROR_STATE_VALUE": Function.ERROR,
}


@dataclass(frozen=True)
class _Theirs:
    quantity: Quantity | None
    unit: str | None
    storage: int
    tariff: int
    subunit: int
    function: Function | None
    value: Any


def _theirs(record: Any) -> _Theirs:
    info = record.interpreted
    kind = info["type"]
    quantity: Quantity | None
    unit: str | None
    if kind == "Energy" and info["unit"] in _FB_ENERGY:
        quantity, unit = Quantity.ENERGY, _FB_ENERGY[info["unit"]]
    else:
        quantity, unit = _TYPES.get(kind, (None, None))
    return _Theirs(
        quantity=quantity,
        unit=unit,
        storage=info["storage_number"],
        tariff=info.get("tariff", 0),
        subunit=info.get("device", 0),
        function=_FUNCTIONS.get(info["function"]),
        value=info["value"],
    )


def _data_records(telegram: Any) -> list[Any]:
    skipped = {"FunctionType.MORE_RECORDS_FOLLOW", "FunctionType.SPECIAL_FUNCTION"}
    return [r for r in telegram.records if r.interpreted["function"] not in skipped]


def long_frames(data: bytes) -> list[LongFrame]:
    """The long frames in ``data`` (single characters and short frames skipped)."""
    reader = FrameReader()
    reader.feed(data)
    frames = []
    while (frame := reader.next_frame()) is not None:
        if isinstance(frame, LongFrame):
            frames.append(frame)
    return frames


def _agree(where: str, ours: MBusRecord, theirs: _Theirs) -> None:
    assert ours.key is not None
    assert ours.family is not None
    assert ours.unit is not None
    assert ours.exact_value is not None
    assert theirs.quantity == ours.key.quantity, where
    assert theirs.storage == ours.key.storage, where
    assert theirs.tariff == ours.key.tariff, where
    assert theirs.subunit == ours.key.subunit, where
    assert theirs.function == ours.key.function, where
    assert theirs.unit is not None, where
    expected = Decimal(theirs.value)
    got = convert(Decimal(ours.exact_value), ours.unit, theirs.unit, ours.family)
    # A real keeps its float32 binary value in pyMeterBus; the codec takes the
    # shortest decimal that reads back as the same float32.
    relative = _FLOAT32 if ours.raw[0] & 0x0F == _DIF_REAL else _RELATIVE
    assert abs(got - expected) <= relative * abs(expected), (where, got, expected)


def oracle_compare(
    data: bytes, label: str, cannot: frozenset[int] = frozenset()
) -> tuple[int, set[int]]:
    """Compare one frame's records with pyMeterBus.

    ``cannot`` lists record indexes pyMeterBus is known to get wrong. Returns
    how many records were compared and which listed indexes were skipped.
    Unsupported records and value errors are not compared.
    """
    compared = 0
    skipped: set[int] = set()
    for frame in long_frames(data):
        ours = [
            record
            for record in parse_rsp_ud(frame).records
            if record.raw[0] & 0x0F not in _NO_DATA_FIELDS
        ]
        theirs = _data_records(meterbus.load(data))
        assert len(ours) == len(theirs), label
        for index, (mine, other) in enumerate(zip(ours, theirs, strict=True)):
            if mine.key is None or mine.exact_value is None:
                continue  # unsupported or a value error: nothing to compare
            if index in cannot:
                skipped.add(index)
                continue
            _agree(f"{label} record {index}", mine, _theirs(other))
            compared += 1
    return compared, skipped


# The simulator's documented tables


@dataclass(frozen=True)
class DocumentedMeter:
    number: int
    primary: int
    id_digits: str
    manufacturer: str
    version: int
    medium: int
    status: int
    telegrams: int


@dataclass(frozen=True)
class DocumentedRecord:
    """One row of the records table; ``quantity`` is ``"date"`` when unsupported."""

    meter: int
    telegram: int
    quantity: str
    unit: str
    storage: int
    tariff: int
    subunit: int
    function: str
    qualifiers: tuple[str, ...]
    initial: str


def _table(header: str) -> list[list[str]]:
    doc = simulator.__doc__ or ""
    lines = doc[doc.index("<!-- meters:begin -->") : doc.index("<!-- meters:end -->")]
    rows = lines.splitlines()
    start = rows.index(header)
    assert set(rows[start + 1].replace("|", "").split()) == {"---"}
    table = []
    for line in rows[start + 2 :]:
        if not line.startswith("|"):
            break
        table.append([cell.strip() for cell in line.strip("|").split("|")])
    return table


def documented_meters() -> list[DocumentedMeter]:
    """The meters table of the simulator docstring."""
    header = (
        "| Meter | Primary | ID | Manufacturer | Version | Medium | Status "
        "| Telegrams |"
    )
    return [
        DocumentedMeter(
            number=int(number),
            primary=int(primary),
            id_digits=id_digits,
            manufacturer=manufacturer,
            version=int(version, 16),
            medium=int(medium, 16),
            status=int(status, 16),
            telegrams=int(telegrams),
        )
        for number, primary, id_digits, manufacturer, version, medium, status, (
            telegrams
        ) in _table(header)
    ]


def documented_records() -> list[DocumentedRecord]:
    """The records table of the simulator docstring, in telegram order."""
    header = (
        "| Meter | Tg | Quantity | Unit | St | Tariff | Sub | Function "
        "| Qualifiers | Initial |"
    )
    return [
        DocumentedRecord(
            meter=int(meter),
            telegram=int(telegram),
            quantity=quantity,
            unit="" if unit == "-" else unit,
            storage=int(storage),
            tariff=int(tariff),
            subunit=int(subunit),
            function=function,
            qualifiers=() if qualifiers == "-" else tuple(qualifiers.split(", ")),
            initial=initial,
        )
        for meter, telegram, quantity, unit, storage, tariff, subunit, function, (
            qualifiers
        ), initial in _table(header)
    ]


# The simulator over TCP


class FakeClock:
    """A clock that moves only when the test says so."""

    def __init__(self, now: float = 0.0) -> None:
        """Start at ``now`` seconds."""
        self.now = now

    def __call__(self) -> float:
        """The current time in seconds."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += seconds


class MBusSimServer:
    """The five demo meters served over TCP on a free local port."""

    def __init__(self) -> None:
        """Create a stopped server; ``bus.meters`` may be changed before start."""
        self.clock = FakeClock()
        self.bus = simulator.SimulatedBus(simulator.demo_meters(), clock=self.clock)
        self.port = free_port()
        self.endpoint = f"tcp://127.0.0.1:{self.port}"
        self._server: simulator.TcpSimulator | None = None

    async def start(self) -> None:
        """Start listening."""
        if self._server is None:
            self._server = await simulator.serve_tcp(self.bus, "127.0.0.1", self.port)

    async def stop(self) -> None:
        """Stop listening and drop every client connection."""
        server, self._server = self._server, None
        if server is not None:
            await server.close()


@pytest_asyncio.fixture
async def mbus_sim() -> AsyncIterator[MBusSimServer]:
    """Yield a started simulator of the five demo meters."""
    server = MBusSimServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# The simulator in memory

_REQ_UD2 = 0x4B  # FCB and FCV masked out


class MemoryTransport:
    """``MBusTransport`` straight onto a ``SimulatedBus``.

    Every write yields to the event loop once, as real I/O does, so other
    tasks waiting for the bus get their turn. ``requests`` lists the
    addresses of the REQ_UD2 frames, ``frames`` every request frame;
    ``on_write`` (if set) runs before each request is answered; ``lose``
    makes writes fail as a lost connection.
    """

    def __init__(self, bus: simulator.SimulatedBus) -> None:
        """Wrap ``bus``; the transport starts closed."""
        self._bus = bus
        self._pending = b""
        self.lose = False
        self.opens = 0
        self.is_open = False
        self.requests: list[int] = []
        self.frames: list[Frame] = []
        self.on_write: Callable[[Frame], Awaitable[None]] | None = None

    async def open(self) -> None:
        """Count the open."""
        self.opens += 1
        self.is_open = True
        self._pending = b""

    async def write(self, data: bytes) -> None:
        """Answer the request from the simulated bus."""
        if self.lose:
            raise BusTransportError("mbus_connection_lost")
        await asyncio.sleep(0)
        reader = FrameReader()
        reader.feed(data)
        frame = reader.next_frame()
        assert frame is not None
        self.frames.append(frame)
        if isinstance(frame, ShortFrame) and frame.control & 0x4F == _REQ_UD2:
            self.requests.append(frame.address)
        if self.on_write is not None:
            await self.on_write(frame)
        self._pending += self._bus.handle(data) or b""

    async def read_frame(self, timeout_s: float) -> Frame | None:
        """The answer to the last request, at once; ``None`` on silence."""
        reader = FrameReader()
        reader.feed(self._pending)
        self._pending = b""
        return reader.next_frame()

    async def flush_input(self) -> None:
        """Drop an unread answer."""
        self._pending = b""

    async def close(self) -> None:
        """Mark the transport closed."""
        self.is_open = False

    def describe(self) -> str:
        """The transport kind."""
        return "Memory"


def memory_transport(meters: list[simulator.SimulatedMeter]) -> MemoryTransport:
    """A memory transport onto a bus of ``meters`` whose clock stays at 0."""
    return MemoryTransport(simulator.SimulatedBus(meters, clock=lambda: 0.0))


# Panel actions


def mbus_connection(
    endpoint: str = "tcp://127.0.0.1:10001", **options: object
) -> ConnectionConfig:
    """Connection ``heat`` with a short timeout and scan timeout (50 ms)."""
    return ConnectionConfig(
        id="heat",
        protocol="mbus",
        endpoint=endpoint,
        timeout_ms=200,
        protocol_options={"scan_timeout_ms": 50, **options},
    )


class BusHost:
    """The part of the collector host the M-Bus Panel actions use."""

    def __init__(self, registry: DriverRegistry) -> None:
        """A host with no running Connection."""
        self.registry = registry
        self.applying = False
        self.running_config: CollectorConfig | None = None
        self.buses: dict[str, MBusBus] = {}
        self.mbus_scans = ScanRegistry(self)

    def mbus_bus(self, connection_id: str) -> MBusBus | None:
        """The running Connection's bus."""
        return self.buses.get(connection_id)

    def run(self, connection: ConnectionConfig, bus: MBusBus) -> None:
        """Pretend ``connection``'s worker runs on ``bus``."""
        self.running_config = CollectorConfig(
            connections=(connection,), tag_groups=(), tags=()
        )
        self.buses[connection.id] = bus


def memory_host(transport: MemoryTransport) -> BusHost:
    """A host whose M-Bus drivers (and so temporary buses) use ``transport``."""
    return BusHost(
        build_driver_registry(
            [("mbus", lambda c: MBusDriver(c, transport_factory=lambda: transport))]
        )
    )


@asynccontextmanager
async def running_host(
    transport: MemoryTransport, connection: ConnectionConfig
) -> AsyncIterator[tuple[BusHost, MBusDriver]]:
    """A host whose worker for ``connection`` runs on ``transport``."""
    host = memory_host(transport)
    driver = MBusDriver(connection, transport_factory=lambda: transport)
    await driver.connect()
    host.run(connection, driver.bus)
    try:
        yield host, driver
    finally:
        await host.mbus_scans.cancel_all("Scan stopped: the test ended.")
        await driver.disconnect()


# Pseudo-terminal


@contextmanager
def pty_pair() -> Iterator[tuple[int, int, str]]:
    """A raw pty: master fd (for ``serve_serial``), slave fd, slave path.

    POSIX only; the caller skips on Windows. The slave fd stays open so the
    pty lives while a serial transport opens and closes the path. Both ends
    are closed on exit.
    """
    if sys.platform == "win32":  # pragma: no cover - guarded by the caller
        raise RuntimeError("pseudo-terminals are POSIX only")
    import pty
    import tty

    master, slave = pty.openpty()
    try:
        tty.setraw(slave)
        yield master, slave, os.ttyname(slave)
    finally:
        os.close(slave)
        os.close(master)
