"""M-Bus simulator: EN 13757-2 link layer, the documented meters, transports."""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal

import pytest
from mbus_support import (
    FakeClock,
    MBusSimServer,
    documented_meters,
    documented_records,
    oracle_compare,
    pty_pair,
)

from plc_data_collector.protocols.mbus_codec import (
    ACK,
    ADDRESS_BROADCAST,
    ADDRESS_SECONDARY,
    FrameReader,
    GarbledFrame,
    LongFrame,
    MBusRecord,
    Telegram,
    parse_rsp_ud,
    req_ud2,
    select_secondary,
    snd_nke,
)
from plc_data_collector.protocols.mbus_tags import Quantity
from plc_data_collector.simulators import mbus as simulator
from plc_data_collector.simulators.mbus import SimulatedBus, SimulatedMeter

ACK_BYTES = bytes((ACK,))


def _bus(clock: FakeClock | None = None) -> SimulatedBus:
    return SimulatedBus(simulator.demo_meters(), clock=clock or FakeClock())


def _pattern(text: str) -> bytes:
    """``12345678`` or 16 hex digits (ID, manufacturer, version, medium)."""
    text = text.ljust(16, "F")
    id_part = bytes.fromhex(text[:8])[::-1]
    manufacturer = bytes.fromhex(text[8:12])[::-1]
    return id_part + manufacturer + bytes.fromhex(text[12:16])


def _telegram(reply: bytes | None) -> Telegram:
    assert reply is not None
    reader = FrameReader()
    reader.feed(reply)
    frame = reader.next_frame()
    assert isinstance(frame, LongFrame)
    assert reader.next_frame() is None
    return parse_rsp_ud(frame)


def _garbled(reply: bytes | None) -> bool:
    assert reply is not None
    reader = FrameReader()
    reader.feed(reply)
    try:
        reader.next_frame()
    except GarbledFrame:
        return True
    return False


def _read_all(bus: SimulatedBus, address: int) -> list[Telegram]:
    """SND_NKE, then REQ_UD2 with alternating FCB until no more follow."""
    if address != ADDRESS_SECONDARY:
        assert bus.handle(snd_nke(address)) == ACK_BYTES
    telegrams: list[Telegram] = []
    fcb = True
    while True:
        telegrams.append(_telegram(bus.handle(req_ud2(address, fcb))))
        fcb = not fcb
        if not telegrams[-1].more_follows:
            return telegrams


def _records(bus: SimulatedBus, address: int) -> list[MBusRecord]:
    return [r for t in _read_all(bus, address) for r in t.records]


def _value(records: list[MBusRecord], quantity: Quantity) -> Decimal | int:
    found = [r for r in records if r.key is not None and r.key.quantity == quantity]
    value = found[0].exact_value
    assert value is not None
    return value


def _select(bus: SimulatedBus, pattern: str) -> bytes | None:
    return bus.handle(select_secondary(_pattern(pattern)))


def _read_meter(bus: SimulatedBus, meter: SimulatedMeter) -> list[Telegram]:
    if meter.primary:
        return _read_all(bus, meter.primary)
    assert bus.handle(snd_nke(ADDRESS_SECONDARY)) is None  # nothing selected
    assert _select(bus, meter.id_digits) == ACK_BYTES
    telegrams = _read_all(bus, ADDRESS_SECONDARY)
    assert bus.handle(snd_nke(ADDRESS_SECONDARY)) == ACK_BYTES
    return telegrams


# The documented meters


def test_documented_tables_list_the_five_meters() -> None:
    meters = documented_meters()
    assert [(m.primary, m.id_digits) for m in meters] == [
        (1, "12345678"),
        (2, "12399999"),
        (3, "22223333"),
        (0, "87654321"),
        (5, "55667788"),
    ]
    assert {m.manufacturer for m in meters} == {"PDC"}
    assert [m.status for m in meters] == [0, 0, 0, 0, 0x04]
    assert [m.telegrams for m in meters] == [2, 1, 1, 1, 1]
    records = documented_records()
    assert {r.meter for r in records} == {1, 2, 3, 4, 5}
    assert [r.quantity for r in records if r.meter == 1].count("date") == 1


def test_frames_match_the_documented_tables() -> None:
    """At time 0 every meter sends exactly what its docstring rows say."""
    bus = _bus()
    meters = documented_meters()
    assert len(bus.meters) == len(meters)
    for documented, meter in zip(meters, bus.meters, strict=True):
        telegrams = _read_meter(bus, meter)
        assert len(telegrams) == documented.telegrams
        for telegram in telegrams:
            header = telegram.header
            assert header.id_digits == documented.id_digits
            assert header.manufacturer == documented.manufacturer
            assert header.version == documented.version
            assert header.medium == documented.medium
            assert header.status == documented.status
        rows = [r for r in documented_records() if r.meter == documented.number]
        sent = [
            (number, record)
            for number, telegram in enumerate(telegrams, start=1)
            for record in telegram.records
        ]
        assert len(sent) == len(rows), documented.number
        for row, (number, record) in zip(rows, sent, strict=True):
            where = (documented.number, row.quantity, row.storage, row.tariff)
            assert number == row.telegram, where
            if row.quantity == "date":
                assert record.key is None, where
                assert record.unsupported_reason is not None
                assert "date" in record.unsupported_reason
                continue
            assert record.key is not None, (where, record.unsupported_reason)
            assert record.key.quantity == row.quantity, where
            assert record.unit == row.unit, where
            assert record.key.storage == row.storage, where
            assert record.key.tariff == row.tariff, where
            assert record.key.subunit == row.subunit, where
            assert record.key.function == row.function, where
            assert record.key.qualifiers == row.qualifiers, where
            assert record.exact_value == Decimal(row.initial), where


def test_only_the_date_record_is_unsupported() -> None:
    bus = _bus()
    unsupported = [
        (meter.id_digits, record.description)
        for meter in bus.meters
        for telegram in _read_meter(bus, meter)
        for record in telegram.records
        if record.key is None or record.exact_value is None
    ]
    assert len(unsupported) == 1
    assert unsupported[0][0] == "12345678"
    assert "date" in unsupported[0][1]


@pytest.mark.parametrize("elapsed", [0.0, 1.0, 61.0, 3600.0, 86_400.5])
def test_every_frame_passes_the_pymeterbus_oracle(elapsed: float) -> None:
    clock = FakeClock()
    bus = _bus(clock)
    clock.advance(elapsed)
    compared = 0
    for meter in bus.meters:
        if meter.primary:
            assert bus.handle(snd_nke(meter.primary)) == ACK_BYTES
            address = meter.primary
        else:
            assert _select(bus, meter.id_digits) == ACK_BYTES
            address = ADDRESS_SECONDARY
        fcb = True
        while True:
            reply = bus.handle(req_ud2(address, fcb))
            assert reply is not None
            count, _ = oracle_compare(reply, f"{meter.id_digits} at {elapsed}")
            compared += count
            fcb = not fcb
            if not _telegram(reply).more_follows:
                break
        if not meter.primary:
            assert bus.handle(snd_nke(ADDRESS_SECONDARY)) == ACK_BYTES
    assert compared == len(documented_records()) - 1  # all but the date


# Link layer


def test_meter_one_spans_two_telegrams() -> None:
    bus = _bus()
    assert bus.handle(snd_nke(1)) == ACK_BYTES
    first = _telegram(bus.handle(req_ud2(1, True)))
    second = _telegram(bus.handle(req_ud2(1, False)))
    assert first.more_follows
    assert not second.more_follows
    assert first.header.id_digits == second.header.id_digits == "12345678"
    assert first.records and second.records
    assert first.header.access_no != second.header.access_no


def test_repeated_fcb_repeats_the_last_telegram() -> None:
    bus = _bus()
    assert bus.handle(snd_nke(1)) == ACK_BYTES
    first = bus.handle(req_ud2(1, True))
    assert bus.handle(req_ud2(1, True)) == first  # the master lost the reply
    second = bus.handle(req_ud2(1, False))
    assert second != first
    assert bus.handle(req_ud2(1, False)) == second
    # After the last telegram a toggled FCB starts over with the first one.
    again = _telegram(bus.handle(req_ud2(1, True)))
    assert again.more_follows
    assert again.records[0].key == _telegram(first).records[0].key


def test_snd_nke_resets_the_fcb_state() -> None:
    bus = _bus()
    assert bus.handle(snd_nke(1)) == ACK_BYTES
    assert _telegram(bus.handle(req_ud2(1, True))).more_follows
    assert bus.handle(snd_nke(1)) == ACK_BYTES
    # The same FCB after a reset is a new request: the first telegram again.
    assert _telegram(bus.handle(req_ud2(1, True))).more_follows


def test_primary_addresses_answer() -> None:
    bus = _bus()
    for primary, medium in ((1, 0x04), (2, 0x07), (3, 0x02), (5, 0x06)):
        assert bus.handle(snd_nke(primary)) == ACK_BYTES
        reply = bus.handle(req_ud2(primary, True))
        assert reply is not None
        frame = FrameReader()
        frame.feed(reply)
        long_frame = frame.next_frame()
        assert isinstance(long_frame, LongFrame)
        assert long_frame.address == primary
        assert parse_rsp_ud(long_frame).header.medium == medium
    for silent in (4, 6, 250):
        assert bus.handle(snd_nke(silent)) is None
        assert bus.handle(req_ud2(silent, True)) is None


def test_broadcast_resets_without_reply() -> None:
    bus = _bus()
    assert bus.handle(snd_nke(1)) == ACK_BYTES
    assert _telegram(bus.handle(req_ud2(1, True))).more_follows
    assert bus.handle(snd_nke(ADDRESS_BROADCAST)) is None
    assert bus.handle(req_ud2(ADDRESS_BROADCAST, True)) is None
    assert _telegram(bus.handle(req_ud2(1, True))).more_follows


def test_primary_zero_meter_answers_only_via_secondary() -> None:
    bus = _bus()
    assert bus.handle(snd_nke(0)) is None
    assert bus.handle(req_ud2(0, True)) is None
    assert _select(bus, "87654321") == ACK_BYTES
    telegram = _telegram(bus.handle(req_ud2(ADDRESS_SECONDARY, True)))
    assert telegram.header.id_digits == "87654321"
    assert telegram.header.medium_name == "heat"
    energy = [r for r in telegram.records if r.key and r.key.quantity == "energy"]
    assert energy[0].unit == "GJ"


def test_select_one_meter_then_read_and_deselect() -> None:
    bus = _bus()
    assert _select(bus, "12345678") == ACK_BYTES
    telegrams = _read_all(bus, ADDRESS_SECONDARY)
    assert [t.header.id_digits for t in telegrams] == ["12345678"] * 2
    assert bus.handle(snd_nke(ADDRESS_SECONDARY)) == ACK_BYTES
    assert bus.handle(req_ud2(ADDRESS_SECONDARY, True)) is None
    assert bus.handle(snd_nke(ADDRESS_SECONDARY)) is None


def test_select_with_shared_digits_collides() -> None:
    bus = _bus()
    assert _garbled(_select(bus, "12FFFFFF"))  # meters 1 and 2
    assert _garbled(bus.handle(req_ud2(ADDRESS_SECONDARY, True)))
    assert _garbled(bus.handle(snd_nke(ADDRESS_SECONDARY)))  # both deselect
    assert bus.handle(req_ud2(ADDRESS_SECONDARY, True)) is None


def test_select_narrows_the_selection() -> None:
    bus = _bus()
    assert _garbled(_select(bus, "123FFFFF"))
    # A new selection replaces the old one: non-matching meters deselect.
    assert _select(bus, "1239FFFF") == ACK_BYTES
    telegram = _telegram(bus.handle(req_ud2(ADDRESS_SECONDARY, True)))
    assert telegram.header.id_digits == "12399999"
    assert _select(bus, "9FFFFFFF") is None
    assert bus.handle(req_ud2(ADDRESS_SECONDARY, True)) is None


def test_select_wildcards_manufacturer_version_medium() -> None:
    bus = _bus()
    assert _select(bus, "1234567840830104") == ACK_BYTES  # PDC, v1, heat
    assert _select(bus, "12345678FFFFFF04") == ACK_BYTES
    assert _select(bus, "12345678FFFFFF07") is None  # not water
    assert _select(bus, "1234567841830104") is None  # another manufacturer
    assert _select(bus, "12345678408302FF") is None  # another version
    assert _select(bus, "FFFFFFFFFFFFFF02") == ACK_BYTES  # the one electricity
    assert _garbled(_select(bus, "FFFFFFFF40830104"))  # two heat meters


def test_garbled_or_unknown_requests_get_no_reply() -> None:
    bus = _bus()
    bad_checksum = bytearray(snd_nke(1))
    bad_checksum[3] ^= 0xFF
    assert bus.handle(bytes(bad_checksum)) is None
    assert bus.handle(b"") is None
    assert bus.handle(snd_nke(1)[:3]) is None  # incomplete
    assert bus.handle(ACK_BYTES) is None
    assert bus.handle(bytes((0x10, 0x5A, 1, 0x5B, 0x16))) is None  # REQ_UD1


def test_two_meters_on_one_primary_collide() -> None:
    meters = simulator.demo_meters()
    meters[1].primary = 1
    bus = SimulatedBus(meters, clock=FakeClock())
    assert _garbled(bus.handle(snd_nke(1)))
    assert _garbled(bus.handle(req_ud2(1, True)))


def test_status_is_settable() -> None:
    meters = simulator.demo_meters()
    meters[0].status = 0x10  # temporary error
    bus = SimulatedBus(meters, clock=FakeClock())
    assert bus.handle(snd_nke(1)) == ACK_BYTES
    assert _telegram(bus.handle(req_ud2(1, True))).header.status == 0x10
    assert _telegram(bus.handle(req_ud2(5, True))).header.status == 0x04


def test_records_per_telegram_splits_any_meter() -> None:
    meters = simulator.demo_meters()
    meters[2].records_per_telegram = 1
    bus = SimulatedBus(meters, clock=FakeClock())
    telegrams = _read_all(bus, 3)
    assert len(telegrams) == len(meters[2].records)
    assert all(len(t.records) == 1 for t in telegrams)


def test_demo_meters_are_fresh_copies() -> None:
    first = simulator.demo_meters()
    first[0].status = 0x08
    assert simulator.demo_meters()[0].status == 0


# Values over time


def test_values_change_with_the_clock() -> None:
    clock = FakeClock()
    bus = _bus(clock)
    before = {p: _records(bus, p) for p in (1, 2, 3)}
    clock.advance(3700.0)  # not a whole number of any period
    after = {p: _records(bus, p) for p in (1, 2, 3)}
    for primary in (1, 2, 3):
        old = before[primary]
        new = after[primary]
        for quantity in (Quantity.ENERGY, Quantity.VOLUME):
            matching = [
                (o, n)
                for o, n in zip(old, new, strict=True)
                if o.key is not None and o.key.quantity == quantity
            ]
            for o, n in matching:
                assert o.exact_value is not None and n.exact_value is not None
                assert n.exact_value >= o.exact_value, (primary, o.key)
                if o.key is not None and o.key.storage == 0 and primary != 3:
                    assert n.exact_value > o.exact_value, (primary, o.key)
    assert _value(after[1], Quantity.ENERGY) > _value(before[1], Quantity.ENERGY)
    assert _value(after[1], Quantity.FLOW_TEMPERATURE) != _value(
        before[1], Quantity.FLOW_TEMPERATURE
    )
    assert _value(after[1], Quantity.OPERATING_TIME) == 21_001


def test_counters_never_decrease_and_temperatures_stay_in_range() -> None:
    clock = FakeClock()
    bus = _bus(clock)
    last = Decimal(0)
    for _ in range(200):
        records = _records(bus, 1)
        energy = Decimal(_value(records, Quantity.ENERGY))
        assert energy >= last
        last = energy
        flow = _value(records, Quantity.FLOW_TEMPERATURE)
        assert Decimal("67.5") <= flow <= Decimal("72.5")
        clock.advance(37.3)


def test_values_are_deterministic_under_a_fake_clock() -> None:
    def frames() -> list[bytes | None]:
        clock = FakeClock(1000.0)
        bus = _bus(clock)
        clock.advance(1234.5)
        bus.handle(snd_nke(3))
        return [bus.handle(req_ud2(3, True)), bus.handle(req_ud2(1, True))]

    assert frames() == frames()


# Transports


async def _exchange(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: bytes
) -> bytes:
    for byte in request:  # one byte at a time: the server assembles frames
        writer.write(bytes((byte,)))
        await writer.drain()
    first = await asyncio.wait_for(reader.readexactly(1), 2.0)
    if first == ACK_BYTES:
        return first
    head = first + await asyncio.wait_for(reader.readexactly(3), 2.0)
    return head + await asyncio.wait_for(reader.readexactly(head[1] + 2), 2.0)


async def test_tcp_serves_the_bus(mbus_sim: MBusSimServer) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", mbus_sim.port)
    try:
        assert await _exchange(reader, writer, snd_nke(1)) == ACK_BYTES
        # A garbled request gets no reply; the connection stays usable.
        writer.write(b"\x10\x40\x01\x00\x16")
        await writer.drain()
        await asyncio.sleep(0.05)  # dropped alone, like a garbled frame on a bus
        reply = await _exchange(reader, writer, req_ud2(1, True))
        assert _telegram(reply).more_follows
        select = select_secondary(_pattern("87654321"))
        assert await _exchange(reader, writer, select) == ACK_BYTES
        reply = await _exchange(reader, writer, req_ud2(ADDRESS_SECONDARY, True))
        assert _telegram(reply).header.id_digits == "87654321"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_tcp_clients_share_one_bus(mbus_sim: MBusSimServer) -> None:
    first = await asyncio.open_connection("127.0.0.1", mbus_sim.port)
    second = await asyncio.open_connection("127.0.0.1", mbus_sim.port)
    try:
        select = select_secondary(_pattern("22223333"))
        assert await _exchange(*first, select) == ACK_BYTES
        reply = await _exchange(*second, req_ud2(ADDRESS_SECONDARY, True))
        assert _telegram(reply).header.id_digits == "22223333"
    finally:
        for _, writer in (first, second):
            writer.close()
            await writer.wait_closed()


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are POSIX")
async def test_serial_serves_the_bus_over_a_pty() -> None:
    bus = _bus()
    with pty_pair() as (master, slave, _path):
        task = asyncio.create_task(simulator.serve_serial(bus, master))
        loop = asyncio.get_running_loop()
        try:

            async def exchange(request: bytes, size: int) -> bytes:
                await loop.run_in_executor(None, os.write, slave, request)
                received = b""
                while len(received) < size:
                    chunk = await asyncio.wait_for(
                        loop.run_in_executor(None, os.read, slave, 512), 2.0
                    )
                    received += chunk
                return received

            assert await exchange(snd_nke(5), 1) == ACK_BYTES
            reply = await exchange(req_ud2(5, True), 4)
            while len(reply) < reply[1] + 6:
                reply += await asyncio.wait_for(
                    loop.run_in_executor(None, os.read, slave, 512), 2.0
                )
            assert _telegram(reply).header.status == 0x04
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are POSIX")
async def test_serial_ends_when_the_pty_closes() -> None:
    import pty

    master, slave = pty.openpty()
    try:
        task = asyncio.create_task(simulator.serve_serial(_bus(), master))
        await asyncio.sleep(0.05)
        assert not task.done()
        os.close(slave)
        await asyncio.wait_for(task, 2.0)
        os.fstat(master)  # the caller's fd is left open
    finally:
        os.close(master)


def test_command_line_defaults() -> None:
    args = simulator.build_parser().parse_args([])
    assert args.port == 10001
    assert args.host == "0.0.0.0"
    args = simulator.build_parser().parse_args(["--host", "127.0.0.1", "--port", "1"])
    assert (args.host, args.port) == ("127.0.0.1", 1)
