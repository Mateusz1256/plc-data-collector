"""M-Bus transports and the shared bus, against the simulator over TCP and a pty."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
import os
import sys
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest
from mbus_support import (
    FakeClock,
    MBusSimServer,
    MemoryTransport,
    documented_meters,
    documented_records,
    free_port,
)

from plc_data_collector.domain import TransientCommunicationError
from plc_data_collector.protocols.mbus_bus import (
    BusTransportError,
    MBusBus,
    MBusTransport,
    MeterGarbled,
    MeterNoReply,
    MeterReading,
    SerialTransport,
    TcpTransport,
)
from plc_data_collector.protocols.mbus_codec import (
    ADDRESS_SECONDARY,
    Frame,
    FrameReader,
    GarbledFrame,
    LongFrame,
    MeterHeader,
    ShortFrame,
)
from plc_data_collector.protocols.mbus_tags import MeterAddress, parse_meter_address
from plc_data_collector.simulators import mbus as simulator

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="pseudo-terminals are POSIX"
)
TIMEOUT_S = 0.5
SND_NKE = 0x40
REQ_UD2 = 0x4B  # FCB and FCV masked out


def _primary(address: int) -> MeterAddress:
    return MeterAddress(primary=address, secondary=None)


def _tcp_bus(
    port: int, *, timeout_s: float = TIMEOUT_S, max_telegrams: int = 8
) -> MBusBus:
    return MBusBus(
        lambda: TcpTransport("127.0.0.1", port, connect_timeout_s=timeout_s),
        timeout_s=timeout_s,
        max_telegrams=max_telegrams,
    )


def _quantities(reading: MeterReading) -> list[tuple[str, int]]:
    return [
        (
            "date" if r.key is None else str(r.key.quantity),
            0 if r.key is None else r.key.storage,
        )
        for r in reading.records
    ]


def _documented(meter: int, telegrams: int = 16) -> list[tuple[str, int]]:
    return [
        (r.quantity, 0 if r.quantity == "date" else r.storage)  # date: no key
        for r in documented_records()
        if r.meter == meter and r.telegram <= telegrams
    ]


def _id_of(meter: int) -> str:
    return next(m.id_digits for m in documented_meters() if m.number == meter)


@contextlib.asynccontextmanager
async def _opened(bus: MBusBus) -> AsyncIterator[MBusBus]:
    await bus.open()
    try:
        yield bus
    finally:
        await bus.close()


# A gateway in front of the simulated bus, for timing and request counting


def _kind(frame: Frame) -> tuple[str, int]:
    if isinstance(frame, ShortFrame):
        if frame.control == SND_NKE:
            return ("snd_nke", frame.address)
        if frame.control & 0x4F == REQ_UD2:
            return ("req_ud2", frame.address)
    if isinstance(frame, LongFrame):
        return ("select", frame.address)
    return ("other", -1)


@dataclass
class Gateway:
    """A TCP gateway serving the five demo meters, with test hooks.

    ``segment`` splits each reply into pieces of that many bytes, sent a few
    milliseconds apart. ``before_reply`` may return bytes to send ahead of the
    bus's own reply to a request, and ``slow_tail`` bytes to send 0.1 s after
    those. ``after_reply`` may return bytes to send a little later (a late
    echo). Every request frame is recorded.
    """

    segment: int | None = None
    before_reply: Callable[[Frame], bytes | None] | None = None
    slow_tail: Callable[[Frame], bytes | None] | None = None
    after_reply: Callable[[Frame, bytes], bytes | None] | None = None
    clock: FakeClock = field(default_factory=FakeClock)
    requests: list[tuple[str, int]] = field(default_factory=list)
    late_sent: asyncio.Event = field(default_factory=asyncio.Event)
    port: int = field(default_factory=free_port)
    meters: list[simulator.SimulatedMeter] | None = None

    def __post_init__(self) -> None:
        meters = simulator.demo_meters() if self.meters is None else self.meters
        self.bus = simulator.SimulatedBus(meters, clock=self.clock)
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", self.port)

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        await self._server.wait_closed()

    async def _send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        step = self.segment or len(data)
        for start in range(0, len(data), step):
            writer.write(data[start : start + step])
            await writer.drain()
            if self.segment:
                await asyncio.sleep(0.005)

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        frames = FrameReader()
        try:
            while data := await reader.read(512):
                frames.feed(data)
                while (frame := frames.next_frame()) is not None:
                    self.requests.append(_kind(frame))
                    if self.before_reply and (extra := self.before_reply(frame)):
                        await self._send(writer, extra)
                    if self.slow_tail and (tail := self.slow_tail(frame)):
                        await asyncio.sleep(0.1)
                        await self._send(writer, tail)
                    reply = self.bus.answer(frame)
                    if reply is None:
                        continue
                    await self._send(writer, reply)
                    if self.after_reply and (late := self.after_reply(frame, reply)):
                        await asyncio.sleep(0.05)
                        await self._send(writer, late)
                        self.late_sent.set()
        except (OSError, GarbledFrame):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()


@contextlib.asynccontextmanager
async def _gateway(**hooks: object) -> AsyncIterator[Gateway]:
    gateway = Gateway(**hooks)  # type: ignore[arg-type]
    await gateway.start()
    try:
        yield gateway
    finally:
        await gateway.stop()


@contextlib.asynccontextmanager
async def _connected(
    *, timeout_s: float = TIMEOUT_S, max_telegrams: int = 8, **hooks: object
) -> AsyncIterator[tuple[Gateway, MBusBus]]:
    """A gateway and an open bus to it."""
    async with _gateway(**hooks) as gateway:
        bus = _tcp_bus(gateway.port, timeout_s=timeout_s, max_telegrams=max_telegrams)
        async with _opened(bus):
            yield gateway, bus


# Reading meters over TCP


async def test_reads_a_meter_by_primary_address(mbus_sim: MBusSimServer) -> None:
    async with _opened(_tcp_bus(mbus_sim.port)) as bus:
        assert bus.is_open
        async with bus.turn() as turn:
            reading = await turn.read_meter(_primary(2))
    assert not bus.is_open
    assert reading.header.id_digits == _id_of(2)
    assert reading.header.manufacturer == "PDC"
    assert _quantities(reading) == _documented(2)


async def test_reads_a_meter_by_secondary_address(mbus_sim: MBusSimServer) -> None:
    async with _opened(_tcp_bus(mbus_sim.port)) as bus:
        async with bus.turn() as turn:
            hidden = await turn.read_meter(parse_meter_address("87654321"))
        async with bus.turn() as turn:
            full = await turn.read_meter(parse_meter_address("1234567840830104"))
    assert hidden.header.id_digits == "87654321"  # primary 0: secondary only
    assert _quantities(hidden) == _documented(4)
    assert full.header.id_digits == "12345678"
    assert _quantities(full) == _documented(1)


async def test_multi_telegram_meter_is_joined_up_to_max_telegrams() -> None:
    async with _connected() as (gateway, bus), bus.turn() as turn:
        both = await turn.read_meter(_primary(1))
    assert _quantities(both) == _documented(1)
    assert gateway.requests == [("snd_nke", 1), ("req_ud2", 1), ("req_ud2", 1)]

    async with _connected(max_telegrams=1) as (gateway, bus):
        async with bus.turn() as turn:
            first = await turn.read_meter(_primary(1))
        async with bus.turn() as turn:
            again = await turn.read_meter(_primary(1))
    assert _quantities(first) == _documented(1, telegrams=1)
    # Stopped early, so the next read starts the meter over.
    assert _quantities(again) == _documented(1, telegrams=1)
    assert gateway.requests == [("snd_nke", 1), ("req_ud2", 1)] * 2


def _values(reading: MeterReading) -> list[object]:
    return [r.exact_value for r in reading.records if r.key is not None]


@pytest.mark.parametrize(("meter", "by_id"), [(1, "12345678"), (2, "12399999")])
async def test_one_meter_read_by_primary_and_by_id_is_never_a_repeat(
    meter: int, by_id: str
) -> None:
    async with _connected() as (gateway, bus):
        readings = []
        for address in (_primary(meter), parse_meter_address(by_id), _primary(meter)):
            gateway.clock.advance(3600)
            async with bus.turn() as turn:
                readings.append(await turn.read_meter(address))
    access = [r.header.access_no for r in readings]
    assert access == sorted(set(access))  # every telegram 1 is a fresh one
    for reading in readings:
        assert reading.header.id_digits == by_id
        assert _quantities(reading) == _documented(meter)
    # Counters (energy, volume, operating time) go up between the reads.
    counters = [
        [
            r.exact_value
            for r in reading.records
            if r.key is not None
            and r.key.storage == 0
            and str(r.key.quantity) in ("energy", "volume", "operating_time")
        ]
        for reading in readings
    ]
    assert counters[0]
    for before, after in itertools.pairwise(counters):
        assert all(b < a for b, a in zip(before, after, strict=True))  # type: ignore[operator]
    # The ID read shared the primary read's FCB state: it needed no repeat
    # guard (one REQ_UD2 per telegram), and the second primary read no SND_NKE.
    telegrams = next(m.telegrams for m in documented_meters() if m.number == meter)
    assert gateway.requests.count(("req_ud2", ADDRESS_SECONDARY)) == telegrams
    assert gateway.requests.count(("snd_nke", meter)) == 1


async def test_a_repeated_telegram_is_detected_by_its_access_number() -> None:
    async with _connected() as (gateway, bus):
        async with bus.turn() as turn:
            first = await turn.read_meter(_primary(2))
        gateway.clock.advance(3600)
        # A wildcard ID: the bus cannot know the meter's FCB, sends FCB 1
        # again, and the meter repeats its last telegram.
        async with bus.turn() as turn:
            second = await turn.read_meter(parse_meter_address("1239FFFF"))
    assert second.header.id_digits == first.header.id_digits
    assert second.header.access_no != first.header.access_no
    assert _values(second) != _values(first)
    assert gateway.requests.count(("req_ud2", ADDRESS_SECONDARY)) == 2


async def test_fcb_alternates_and_snd_nke_only_before_the_first_read() -> None:
    fcbs: list[bool] = []

    def note(frame: Frame) -> None:
        if isinstance(frame, ShortFrame) and frame.control & 0x4F == REQ_UD2:
            fcbs.append(bool(frame.control & 0x20))
        return None

    async with _connected(before_reply=note) as (gateway, bus):
        for _ in range(3):
            async with bus.turn() as turn:
                await turn.read_meter(_primary(2))
    assert gateway.requests == [("snd_nke", 2)] + [("req_ud2", 2)] * 3
    assert fcbs == [True, False, True]


async def test_a_missing_meter_is_no_reply_after_exactly_two_requests() -> None:
    async with _connected(timeout_s=0.2) as (gateway, bus):
        async with bus.turn() as turn:
            with pytest.raises(MeterNoReply):
                await turn.read_meter(_primary(9))
        assert bus.is_open  # a meter problem does not close the bus
    assert gateway.requests == [("snd_nke", 9), ("snd_nke", 9)]


async def test_a_secondary_collision_is_garbled_and_the_bus_is_deselected() -> None:
    async with _connected(timeout_s=0.2) as (gateway, bus):
        async with bus.turn() as turn:
            with pytest.raises(MeterGarbled):
                await turn.read_meter(parse_meter_address("12FFFFFF"))
        async with bus.turn() as turn:
            reading = await turn.read_meter(_primary(2))
    assert reading.header.id_digits == _id_of(2)
    selects = [r for r in gateway.requests if r[0] == "select"]
    assert len(selects) == 2  # one retry
    # The read ends with a deselect, answered garbled by both meters (harmless).
    first_primary = gateway.requests.index(("snd_nke", 2))
    assert gateway.requests[first_primary - 1] == ("snd_nke", ADDRESS_SECONDARY)


# Turns


async def test_a_second_turn_waits_for_the_first(mbus_sim: MBusSimServer) -> None:
    order: list[str] = []
    inside = asyncio.Event()
    release = asyncio.Event()

    async with _opened(_tcp_bus(mbus_sim.port)) as bus:

        async def first() -> None:
            async with bus.turn() as turn:
                order.append("first in")
                inside.set()
                await release.wait()
                await turn.read_meter(_primary(2))
                order.append("first out")

        async def second() -> None:
            await inside.wait()
            order.append("second waiting")
            async with bus.turn() as turn:
                order.append("second in")
                await turn.read_meter(_primary(3))

        tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
        await inside.wait()
        await asyncio.sleep(0.05)
        assert order == ["first in", "second waiting"]
        release.set()
        await asyncio.gather(*tasks)
    assert order == ["first in", "second waiting", "first out", "second in"]


async def test_a_turn_on_a_closed_bus_is_not_connected(mbus_sim: MBusSimServer) -> None:
    bus = _tcp_bus(mbus_sim.port)
    with pytest.raises(BusTransportError) as caught:
        async with bus.turn():
            pass
    assert caught.value.code == "mbus_not_connected"


# Probes (scan)


async def test_probe_primary(mbus_sim: MBusSimServer) -> None:
    async with _opened(_tcp_bus(mbus_sim.port)) as bus:
        async with bus.turn() as turn:
            assert await turn.probe_primary(5, 0.2) == "ack"
        async with bus.turn() as turn:
            assert await turn.probe_primary(4, 0.2) == "none"
        async with bus.turn() as turn:
            assert await turn.probe_primary(0, 0.2) == "none"  # primary 0 is hidden
        async with bus.turn() as turn:
            reading = await turn.read_meter(_primary(5))
    assert reading.header.id_digits == _id_of(5)


async def test_probe_primary_collision() -> None:
    meters = simulator.demo_meters()
    meters[1].primary = 1  # meter 2 shares meter 1's address
    port = free_port()
    sim = await simulator.serve_tcp(
        simulator.SimulatedBus(meters, clock=FakeClock()), "127.0.0.1", port
    )
    try:
        async with _opened(_tcp_bus(port, timeout_s=0.2)) as bus:
            async with bus.turn() as turn:
                assert await turn.probe_primary(1, 0.2) == "collision"
            async with bus.turn() as turn:
                assert await turn.probe_primary(3, 0.2) == "ack"
    finally:
        await sim.close()


async def test_probe_secondary_and_read_the_selected_header() -> None:
    async with _connected(timeout_s=0.2) as (gateway, bus):
        async with bus.turn() as turn:
            assert await turn.probe_secondary(_pattern("3FFFFFFF"), 0.2) == "none"
        async with bus.turn() as turn:
            assert await turn.probe_secondary(_pattern("1FFFFFFF"), 0.2) == "collision"
        async with bus.turn() as turn:
            assert await turn.probe_secondary(_pattern("8FFFFFFF"), 0.2) == "ack"
            header = await turn.read_selected_header(0.2)
        async with bus.turn() as turn:
            reading = await turn.read_meter(_primary(1))
    assert header is not None
    assert header.id_digits == "87654321"
    assert reading.header.id_digits == "12345678"
    # A turn that left meters selected ends with a deselect; "none" does not.
    assert gateway.requests == [
        ("select", ADDRESS_SECONDARY),
        ("select", ADDRESS_SECONDARY),
        ("snd_nke", ADDRESS_SECONDARY),
        ("select", ADDRESS_SECONDARY),
        ("req_ud2", ADDRESS_SECONDARY),
        ("snd_nke", ADDRESS_SECONDARY),
        ("snd_nke", 1),
        ("req_ud2", 1),
        ("req_ud2", 1),
    ]


def _pattern(text: str) -> bytes:
    address = parse_meter_address(text).secondary
    assert address is not None
    return address.pattern()


# Review Focus 1: split replies and stale replies


async def test_replies_split_into_three_byte_segments_are_assembled() -> None:
    async with _connected(segment=3) as (gateway, bus):
        readings = {}
        for meter in (1, 2, 3, 5):
            async with bus.turn() as turn:
                readings[meter] = await turn.read_meter(_primary(meter))
        async with bus.turn() as turn:
            hidden = await turn.read_meter(parse_meter_address("87654321"))
    for meter, reading in readings.items():
        assert reading.header.id_digits == _id_of(meter)
        assert _quantities(reading) == _documented(meter)
    assert hidden.header.id_digits == _id_of(4)
    # No request needed a retry.
    assert gateway.requests.count(("req_ud2", 1)) == 2


async def test_a_stale_reply_between_turns_is_flushed() -> None:
    def echo(frame: Frame, reply: bytes) -> bytes | None:
        kind = _kind(frame)
        return reply if kind == ("req_ud2", 2) else None

    async with _connected(after_reply=echo) as (gateway, bus):
        async with bus.turn() as turn:
            await turn.read_meter(_primary(2))
        await asyncio.wait_for(gateway.late_sent.wait(), 2.0)
        await asyncio.sleep(0.05)  # the late copy of meter 2 is in the socket
        async with bus.turn() as turn:
            reading = await turn.read_meter(_primary(3))
    assert reading.header.id_digits == _id_of(3)
    # Flushed at the start of the turn: meter 3 needed no retry.
    assert gateway.requests == [
        ("snd_nke", 2),
        ("req_ud2", 2),
        ("snd_nke", 3),
        ("req_ud2", 3),
    ]


async def test_a_stale_reply_inside_a_turn_is_never_taken_for_the_next_meter() -> None:
    stale: list[bytes] = []

    def remember(frame: Frame, reply: bytes) -> bytes | None:
        if _kind(frame) == ("req_ud2", 2):
            stale.append(reply)
        return None

    def inject(frame: Frame) -> bytes | None:
        if _kind(frame) == ("req_ud2", 3) and stale:
            return stale.pop()  # meter 2's reply arrives just before meter 3's
        return None

    async with _connected(before_reply=inject, after_reply=remember) as (gateway, bus):
        async with bus.turn() as turn:
            await turn.read_meter(_primary(2))
        async with bus.turn() as turn:
            reading = await turn.read_meter(_primary(3))
        async with bus.turn() as turn:
            after = await turn.read_meter(_primary(2))
    assert reading.header.id_digits == _id_of(3)
    assert after.header.id_digits == _id_of(2)
    # The wrong address was treated as garbled: drained, then one retry.
    assert gateway.requests.count(("req_ud2", 3)) == 2


async def test_garbage_ahead_of_a_reply_is_drained_before_the_retry() -> None:
    sent: list[bool] = []

    def noise(frame: Frame) -> bytes | None:
        if _kind(frame) == ("req_ud2", 2) and not sent:
            sent.append(True)
            return b"\x00\x68"
        return None

    hooked = _connected(timeout_s=0.3, before_reply=noise)
    async with hooked as (gateway, bus), bus.turn() as turn:
        reading = await turn.read_meter(_primary(2))
    assert reading.header.id_digits == _id_of(2)
    assert gateway.requests == [("snd_nke", 2), ("req_ud2", 2), ("req_ud2", 2)]


async def test_the_tail_of_a_garbled_reply_never_answers_the_retry() -> None:
    def garbage(frame: Frame) -> bytes | None:
        return b"\x00" if _kind(frame) == ("snd_nke", 9) else None

    def tail(frame: Frame) -> bytes | None:
        # An E5 arriving well after the garbage, still inside the reply window.
        return b"\xe5" if _kind(frame) == ("snd_nke", 9) else None

    hooked = _connected(timeout_s=0.4, before_reply=garbage, slow_tail=tail)
    async with hooked as (gateway, bus), bus.turn() as turn:
        with pytest.raises(MeterGarbled):
            await turn.read_meter(_primary(9))
    # Each E5 was discarded with its window: no REQ_UD2 followed.
    assert gateway.requests == [("snd_nke", 9), ("snd_nke", 9)]


# TCP transport errors


async def test_tcp_connect_refused_is_connect_failed() -> None:
    bus = _tcp_bus(free_port())
    with pytest.raises(BusTransportError) as caught:
        await bus.open()
    assert caught.value.code == "mbus_connect_failed"
    assert isinstance(caught.value, TransientCommunicationError)
    assert not bus.is_open


async def test_a_dropped_gateway_is_connection_lost_and_reopen_works() -> None:
    server = MBusSimServer()
    await server.start()
    bus = _tcp_bus(server.port)
    await bus.open()
    try:
        await server.stop()
        with pytest.raises(BusTransportError) as caught:
            async with bus.turn() as turn:
                await turn.read_meter(_primary(2))
        assert caught.value.code == "mbus_connection_lost"
        assert not bus.is_open
        await server.start()
        await bus.open()
        async with bus.turn() as turn:
            reading = await turn.read_meter(_primary(2))
        assert reading.header.id_digits == _id_of(2)
    finally:
        await bus.close()
        await server.stop()


async def test_tcp_connect_timeout_is_connect_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()

    async def never(*args: object, **kwargs: object) -> object:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(loop, "create_connection", never)
    bus = _tcp_bus(free_port(), timeout_s=0.1)
    with pytest.raises(BusTransportError) as caught:
        await bus.open()
    assert caught.value.code == "mbus_connect_failed"
    assert not bus.is_open


def test_describe() -> None:
    assert TcpTransport("gw", 10001, connect_timeout_s=1.0).describe() == "TCP gateway"
    assert SerialTransport("/dev/ttyUSB0", 2400).describe() == "Serial 2400 baud"
    transport: MBusTransport = SerialTransport("COM3", 9600)
    assert transport.describe() == "Serial 9600 baud"


# Serial over a pty (Review Focus 2)


@dataclass
class _Pty:
    master: int
    slave: int
    path: str
    task: asyncio.Task[None]

    async def unplug(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        os.close(self.master)

    def release(self) -> None:
        os.close(self.slave)


async def _serve_pty(bus: simulator.SimulatedBus) -> _Pty:
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(slave)
    task = asyncio.create_task(simulator.serve_serial(bus, master))
    return _Pty(master, slave, os.ttyname(slave), task)


def _fds() -> int | None:
    path = "/proc/self/fd"
    return len(os.listdir(path)) if os.path.isdir(path) else None


@POSIX_ONLY
async def test_serial_reads_meters_over_a_pty() -> None:
    sim = simulator.SimulatedBus(simulator.demo_meters(), clock=FakeClock())
    port = await _serve_pty(sim)
    try:
        bus = MBusBus(
            lambda: SerialTransport(port.path, 2400),
            timeout_s=0.5,
            max_telegrams=8,
        )
        async with _opened(bus):
            async with bus.turn() as turn:
                first = await turn.read_meter(_primary(1))
            async with bus.turn() as turn:
                hidden = await turn.read_meter(parse_meter_address("87654321"))
            async with bus.turn() as turn:
                with pytest.raises(MeterNoReply):
                    await turn.read_meter(_primary(9))
        assert _quantities(first) == _documented(1)
        assert hidden.header.id_digits == "87654321"
    finally:
        await port.unplug()
        port.release()


@POSIX_ONLY
async def test_serial_unplugged_mid_read_is_connection_lost_without_leaks() -> None:
    threads = threading.active_count()
    fds = _fds()
    current: list[_Pty] = [
        await _serve_pty(simulator.SimulatedBus(simulator.demo_meters(), FakeClock()))
    ]
    bus = MBusBus(
        lambda: SerialTransport(current[0].path, 2400),
        timeout_s=2.0,
        max_telegrams=8,
    )
    await bus.open()
    try:
        async with bus.turn() as turn:
            await turn.read_meter(_primary(2))

        async def read_missing() -> None:
            async with bus.turn() as turn:
                await turn.read_meter(_primary(9))  # waits for a reply

        reading = asyncio.create_task(read_missing())
        await asyncio.sleep(0.2)
        await current[0].unplug()
        with pytest.raises(BusTransportError) as caught:
            await asyncio.wait_for(reading, 3.0)
        assert caught.value.code == "mbus_connection_lost"
        assert isinstance(caught.value, TransientCommunicationError)
        assert not bus.is_open
        current[0].release()

        # Plugged back in (as a new pty).
        current[0] = await _serve_pty(
            simulator.SimulatedBus(simulator.demo_meters(), FakeClock())
        )
        await bus.open()
        async with bus.turn() as turn:
            again = await turn.read_meter(_primary(2))
        assert again.header.id_digits == _id_of(2)
    finally:
        await bus.close()
        await current[0].unplug()
        current[0].release()
    assert threading.active_count() == threads
    if fds is not None:
        assert _fds() == fds


@POSIX_ONLY
async def test_serial_device_missing_is_connect_failed() -> None:
    threads = threading.active_count()
    bus = MBusBus(
        lambda: SerialTransport("/dev/does-not-exist-mbus", 2400),
        timeout_s=0.5,
        max_telegrams=8,
    )
    with pytest.raises(BusTransportError) as caught:
        await bus.open()
    assert caught.value.code == "mbus_connect_failed"
    assert not bus.is_open
    assert threading.active_count() == threads


class _ConstantAccess(simulator.SimulatedMeter):
    """A meter that sends the same access number in every telegram."""

    def header(self, access_no: int) -> bytes:
        return simulator.SimulatedMeter.header(self, 7)


def _constant_access(meter: simulator.SimulatedMeter) -> simulator.SimulatedMeter:
    fields = dataclasses.fields(meter)
    return _ConstantAccess(**{f.name: getattr(meter, f.name) for f in fields if f.init})


async def test_a_known_fcb_skips_the_repeat_guard() -> None:
    meters = simulator.demo_meters()
    meters[1] = _constant_access(meters[1])  # meter 2, one telegram
    async with _connected(meters=meters) as (gateway, bus):
        readings = []
        for _ in range(3):
            gateway.clock.advance(3600)
            async with bus.turn() as turn:
                readings.append(await turn.read_meter(_primary(2)))
    assert {r.header.access_no for r in readings} == {7}
    # One SND_NKE, then exactly one REQ_UD2 per read.
    assert gateway.requests == [("snd_nke", 2)] + [("req_ud2", 2)] * 3


class _NoResetOnDeselect(simulator.SimulatedBus):
    """Meters that keep their FCB state across SND_NKE 253 (some do)."""

    def _snd_nke(self, address: int) -> bytes | None:
        if address != ADDRESS_SECONDARY:
            return super()._snd_nke(address)
        targets = self._addressed(address)
        for index in targets:
            self._states[index].selected = False
        return b"\xe5" if targets else None


def _same_id_meters() -> tuple[list[simulator.SimulatedMeter], MBusBus]:
    """Meter 2 (water, 12399999) and a heat meter with the same ID digits."""
    water = dataclasses.replace(simulator.demo_meters()[1], primary=0)
    heat = dataclasses.replace(water, medium=0x04)
    meters = [heat, water]
    transport = MemoryTransport(_NoResetOnDeselect(meters, clock=lambda: 0.0))
    bus = MBusBus(lambda: transport, timeout_s=TIMEOUT_S, max_telegrams=8)
    return meters, bus


HEAT_ADDRESS = "1239999940830104"
WATER_ADDRESS = "1239999940830107"


async def test_meters_with_one_id_keep_their_own_link_state() -> None:
    _, bus = _same_id_meters()
    access: dict[int, list[int]] = {0x04: [], 0x07: []}
    async with _opened(bus):
        for address in (HEAT_ADDRESS, WATER_ADDRESS) * 2:
            async with bus.turn() as turn:
                reading = await turn.read_meter(parse_meter_address(address))
            access[reading.header.medium].append(reading.header.access_no)
    # Every read got a fresh telegram, never the other meter's FCB's repeat.
    assert access == {0x04: [1, 2], 0x07: [1, 2]}


async def test_a_wildcard_read_matching_two_known_meters_guesses_the_fcb() -> None:
    meters, bus = _same_id_meters()
    async with _opened(bus):
        for address in (HEAT_ADDRESS, WATER_ADDRESS):
            async with bus.turn() as turn:
                await turn.read_meter(parse_meter_address(address))
        meters[1].id_digits = "55555555"  # only the heat meter matches now
        async with bus.turn() as turn:
            reading = await turn.read_meter(parse_meter_address("12399999"))
    # Both known meters match "12399999": the FCB is a guess, the repeat is
    # spotted by its access number and a fresh telegram requested.
    assert reading.header.medium == 0x04
    assert reading.header.access_no == 2


async def test_reading_a_selected_header_forgets_the_meters_fcb() -> None:
    async with _connected() as (gateway, bus):
        async with bus.turn() as turn:
            await turn.read_meter(_primary(2))
        async with bus.turn() as turn:
            assert await turn.probe_secondary(_pattern("12399999"), TIMEOUT_S) == "ack"
            header = await turn.read_selected_header(TIMEOUT_S)
        async with bus.turn() as turn:
            again = await turn.read_meter(_primary(2))
    assert header is not None
    assert header.id_digits == "12399999"
    assert again.header.id_digits == "12399999"
    # The header read used a guessed FCB: the next read starts over.
    assert gateway.requests.count(("snd_nke", 2)) == 2


def test_repeat_guard_skips_meters_that_do_not_count_telegrams() -> None:
    from dataclasses import replace

    from plc_data_collector.protocols.mbus_bus import _repeated

    header = MeterHeader("12345678", "PDC", 0x4083, 1, 4, "heat", 7, 0)
    assert _repeated(header, (7,))
    assert _repeated(header, (6, 7))
    assert not _repeated(header, (6,))
    assert not _repeated(replace(header, access_no=8), (6, 7))
    # Two telegrams with one access number: the meter does not count.
    assert not _repeated(header, (7, 7))
    assert not _repeated(header, ())
