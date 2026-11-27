"""M-Bus driver tests against the simulator (over TCP, and in memory)."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from mbus_support import MBusSimServer, documented_meters, documented_records

from plc_data_collector.domain import (
    ConnectionConfig,
    Quality,
    TagConfig,
    TagGroupConfig,
    TagRequest,
    TagResult,
    TransientCommunicationError,
    ValueType,
)
from plc_data_collector.protocols import (
    build_driver_registry,
    create_mbus_driver,
    default_driver_registry,
)
from plc_data_collector.protocols.driver import CommunicationDriver, DescribesConnection
from plc_data_collector.protocols.mbus import MBusDriver
from plc_data_collector.protocols.mbus_bus import (
    BusTransportError,
    MBusBus,
    TcpTransport,
)
from plc_data_collector.protocols.mbus_codec import Frame, FrameReader, ShortFrame
from plc_data_collector.runtime import ConnectionWorker
from plc_data_collector.simulators import mbus as simulator

LOGGER_NAME = "plc_data_collector.protocols.mbus"
TIMEOUT_MS = 200
REQ_UD2 = 0x4B  # FCB and FCV masked out


def _connection(endpoint: str, **options: object) -> ConnectionConfig:
    return ConnectionConfig(
        id="heat",
        protocol="mbus",
        endpoint=endpoint,
        timeout_ms=TIMEOUT_MS,
        protocol_options=options,
    )


def _tag(
    tag_id: str,
    address: str,
    quantity: str,
    unit: str | None = None,
    value_type: ValueType = ValueType.NUMERIC,
    **options: object,
) -> TagRequest:
    protocol_options: dict[str, object] = {"quantity": quantity, **options}
    if unit is not None:
        protocol_options["unit"] = unit
    return TagRequest(
        tag_id=tag_id,
        address=address,
        value_type=value_type,
        protocol_options=protocol_options,
    )


def _initial(meter: int, quantity: str, storage: int = 0, **match: object) -> Decimal:
    """The documented initial value (the simulator's clock stays at 0)."""
    rows = [
        r
        for r in documented_records()
        if r.meter == meter
        and r.quantity == quantity
        and r.storage == storage
        and all(getattr(r, k) == v for k, v in match.items())
    ]
    assert len(rows) == 1
    return Decimal(rows[0].initial)


def _by_id(results: list[TagResult]) -> dict[str, TagResult]:
    return {result.tag_id: result for result in results}


def _raw(result: TagResult) -> object:
    assert result.value is not None, (result.error_code, result.error_message)
    return result.value.raw


def _simulated(sim: MBusSimServer, primary: int) -> simulator.SimulatedMeter:
    return next(m for m in sim.bus.meters if m.primary == primary)


class _Counted:
    """Counts the bus's turns and records the request frames sent."""

    def __init__(self, driver: MBusDriver) -> None:
        self.turns = 0
        self.requests: list[tuple[str, int]] = []
        bus = driver.bus
        original = bus.turn

        def turn() -> object:
            self.turns += 1
            return original()

        bus.turn = turn  # type: ignore[method-assign,assignment]


def _recording_factory(
    port: int, requests: list[tuple[str, int]]
) -> Callable[[], TcpTransport]:
    class Recording(TcpTransport):
        async def write(self, data: bytes) -> None:
            reader = FrameReader()
            reader.feed(data)
            frame = reader.next_frame()
            if isinstance(frame, ShortFrame):
                kind = "req_ud2" if frame.control & 0x4F == REQ_UD2 else "snd_nke"
                requests.append((kind, frame.address))
            else:
                requests.append(("select", -1))
            await super().write(data)

    return lambda: Recording("127.0.0.1", port, connect_timeout_s=TIMEOUT_MS / 1000)


@asynccontextmanager
async def _driver(
    sim: MBusSimServer, **options: object
) -> AsyncIterator[tuple[MBusDriver, _Counted]]:
    requests: list[tuple[str, int]] = []
    driver = MBusDriver(
        _connection(sim.endpoint, **options),
        transport_factory=_recording_factory(sim.port, requests),
    )
    counted = _Counted(driver)
    counted.requests = requests
    await driver.connect()
    try:
        yield driver, counted
    finally:
        await driver.disconnect()


# Registration and the connection


def test_registered_as_mbus() -> None:
    registry = default_driver_registry()
    driver = registry.create_driver(_connection("tcp://127.0.0.1:10001"))
    assert isinstance(driver, MBusDriver)
    assert isinstance(driver, CommunicationDriver)
    assert isinstance(driver, DescribesConnection)
    assert driver.capabilities.protocol == "mbus"
    assert isinstance(create_mbus_driver(_connection("serial:///dev/null")), MBusDriver)


async def test_connection_details_name_the_transport_not_the_endpoint(
    mbus_sim: MBusSimServer,
) -> None:
    driver = create_mbus_driver(_connection(mbus_sim.endpoint))
    assert driver.connection_details() is None
    await driver.connect()
    try:
        assert await driver.health_check()
        assert driver.connection_details() == "TCP gateway"
    finally:
        await driver.disconnect()
    assert not await driver.health_check()
    assert driver.connection_details() is None


def test_serial_connection_details_give_the_baud_rate() -> None:
    driver = MBusDriver(_connection("serial:///dev/ttyUSB0", baud_rate=9600))
    driver._bus._transport = object()  # type: ignore[assignment]
    assert driver.connection_details() == "Serial 9600 baud"


# Reading


async def test_each_meter_is_read_once_per_poll_in_address_order(
    mbus_sim: MBusSimServer,
) -> None:
    tags = [
        _tag("m3_v", "3", "voltage", "V"),
        _tag("m1_e", "1", "energy", "MWh"),
        _tag("m2_v", "2", "volume", "m3"),
        _tag("m1_v", "001", "volume", "L"),
        _tag("m3_i", "3", "current", "A"),
        _tag("m2_f", "2", "volume_flow", "L/h"),
    ]
    async with _driver(mbus_sim) as (driver, counted):
        results = await driver.read(tags)
        assert counted.turns == 3
        meters = [a for kind, a in counted.requests if kind == "req_ud2"]
        # Meter 1 sends two telegrams.
        assert meters == [1, 1, 2, 3]
        counted.requests.clear()
        await driver.read(tags)
        assert counted.turns == 6
        assert [a for _, a in counted.requests] == [1, 1, 2, 3]
    assert [r.tag_id for r in results] == [t.tag_id for t in tags]
    values = _by_id(results)
    assert all(r.quality is Quality.GOOD for r in results)
    assert _raw(values["m1_e"]) == float(_initial(1, "energy"))
    assert _raw(values["m1_v"]) == float(_initial(1, "volume"))
    assert _raw(values["m2_v"]) == float(_initial(2, "volume") / 1000)
    assert _raw(values["m2_f"]) == float(_initial(2, "volume_flow"))
    assert _raw(values["m3_v"]) == float(_initial(3, "voltage"))
    assert _raw(values["m3_i"]) == float(_initial(3, "current"))


async def test_billing_value_is_exact_in_the_tag_unit(mbus_sim: MBusSimServer) -> None:
    billing = _initial(1, "energy", storage=1)
    tags = [
        _tag("mwh", "1", "energy", "MWh", storage=1),
        _tag("kwh", "1", "energy", "kWh", storage=1),
        _tag("now", "1", "energy", "MWh"),
        _tag("hours", "1", "operating_time", "s", ValueType.INTEGER),
        _tag("t1_fwd", "3", "energy", "kWh", tariff=1, qualifiers=["forward"]),
        _tag("t2_bwd", "3", "energy", "Wh", tariff=2, qualifiers=["backward"]),
    ]
    async with _driver(mbus_sim) as (driver, _):
        values = _by_id(await driver.read(tags))
    assert values["mwh"].quality is Quality.GOOD
    assert _raw(values["mwh"]) == float(billing)
    assert _raw(values["kwh"]) == float(billing * 1000)
    assert _raw(values["now"]) == float(_initial(1, "energy"))
    assert _raw(values["hours"]) == int(_initial(1, "operating_time")) * 3600
    assert isinstance(_raw(values["hours"]), int)
    fwd = _initial(3, "energy", tariff=1, qualifiers=("forward",))
    assert _raw(values["t1_fwd"]) == float(fwd / 1000)
    bwd = _initial(3, "energy", tariff=2, qualifiers=("backward",))
    assert _raw(values["t2_bwd"]) == float(bwd)


async def test_a_meter_in_gj_never_fills_a_kwh_tag(mbus_sim: MBusSimServer) -> None:
    tags = [
        _tag("kwh", "87654321", "energy", "kWh"),
        _tag("gj", "87654321", "energy", "GJ"),
        _tag("mj", "87654321", "energy", "MJ"),
    ]
    async with _driver(mbus_sim) as (driver, counted):
        values = _by_id(await driver.read(tags))
        assert counted.turns == 1
    assert values["kwh"].quality is Quality.BAD
    assert values["kwh"].error_code == "mbus_unit_mismatch"
    assert values["kwh"].value is None
    message = values["kwh"].error_message or ""
    assert "GJ" in message and "kWh" in message
    assert _raw(values["gj"]) == float(_initial(4, "energy"))
    assert _raw(values["mj"]) == float(_initial(4, "energy") * 1000)


async def test_missing_record_is_bad_and_the_others_good(
    mbus_sim: MBusSimServer,
) -> None:
    tags = [
        _tag("missing", "2", "energy", "kWh"),
        _tag("storage2", "2", "volume", "L", storage=2),
        _tag("second", "2", "volume", "L", occurrence=2),
        _tag("volume", "2", "volume", "L"),
    ]
    async with _driver(mbus_sim) as (driver, _):
        values = _by_id(await driver.read(tags))
    for tag_id in ("missing", "storage2", "second"):
        assert values[tag_id].quality is Quality.BAD
        assert values[tag_id].error_code == "mbus_record_not_found"
        assert "2" in (values[tag_id].error_message or "")
    assert _raw(values["volume"]) == float(_initial(2, "volume"))


async def test_occurrence_picks_the_nth_matching_record() -> None:
    first, second = simulator.demo_meters()[1].records[:2]
    assert isinstance(second, simulator.SimulatedRecord)
    # Two records with the same key: the storage-1 one moved to storage 0.
    repeated = dataclasses.replace(second, storage=0)
    meter = simulator.SimulatedMeter(
        primary=7, id_digits="77777777", medium=0x07, records=(first, repeated)
    )
    tags = [
        _tag("one", "7", "volume", "L"),
        _tag("two", "7", "volume", "L", occurrence=2),
        _tag("three", "7", "volume", "L", occurrence=3),
    ]
    async with _memory_driver([meter]) as (driver, _):
        values = _by_id(await driver.read(tags))
    assert _raw(values["one"]) == float(_initial(2, "volume"))
    assert _raw(values["two"]) == float(_initial(2, "volume", storage=1))
    assert values["three"].error_code == "mbus_record_not_found"


async def test_absent_meter_marks_only_its_tags(mbus_sim: MBusSimServer) -> None:
    tags = [
        _tag("absent", "9", "volume", "L"),
        _tag("absent2", "9", "energy", "kWh"),
        _tag("present", "2", "volume", "L"),
    ]
    async with _driver(mbus_sim) as (driver, _):
        values = _by_id(await driver.read(tags))
        assert await driver.health_check()
        again = _by_id(await driver.read(tags))
    for result in (values, again):
        assert result["absent"].error_code == "mbus_no_reply"
        assert result["absent2"].error_code == "mbus_no_reply"
        assert result["absent"].quality is Quality.BAD
        assert _raw(result["present"]) == float(_initial(2, "volume"))


async def test_meter_id_check_marks_only_the_tags_that_fail_it(
    mbus_sim: MBusSimServer,
) -> None:
    tags = [
        _tag("wrong", "1", "energy", "MWh", meter_id="99999999"),
        _tag("right", "1", "energy", "MWh", meter_id="12345678"),
        _tag("unchecked", "1", "volume", "L"),
    ]
    async with _driver(mbus_sim) as (driver, counted):
        values = _by_id(await driver.read(tags))
        assert counted.turns == 1
    assert values["wrong"].quality is Quality.BAD
    assert values["wrong"].error_code == "mbus_wrong_meter"
    message = values["wrong"].error_message or ""
    assert "99999999" in message and "12345678" in message
    assert _raw(values["right"]) == float(_initial(1, "energy"))
    assert _raw(values["unchecked"]) == float(_initial(1, "volume"))


async def test_invalid_tags_are_bad_and_do_not_stop_the_others(
    mbus_sim: MBusSimServer,
) -> None:
    tags = [
        _tag("address", "P5", "volume", "L"),
        _tag("options", "2", "volume", "kWh"),
        _tag("ok", "2", "volume", "L"),
    ]
    async with _driver(mbus_sim) as (driver, counted):
        values = _by_id(await driver.read(tags))
        assert counted.turns == 1
    assert values["address"].error_code == "mbus_invalid_address"
    assert values["options"].error_code == "mbus_invalid_options"
    assert values["ok"].quality is Quality.GOOD


async def test_plans_are_cached_per_tag_key(mbus_sim: MBusSimServer) -> None:
    tag = _tag("t", "3", "energy", "Wh", tariff=1, qualifiers=["forward"])
    edited = _tag("t", "3", "energy", "Wh", tariff=1, qualifiers=["backward"])
    async with _driver(mbus_sim) as (driver, _):
        await driver.read([tag])
        await driver.read([tag])
        assert len(driver._plans) == 1
        values = _by_id(await driver.read([edited]))
        assert len(driver._plans) == 2
    backward = _initial(3, "energy", tariff=1, qualifiers=("backward",))
    assert _raw(values["t"]) == float(backward)


# Meter status


async def test_meter_status_error_makes_its_readings_uncertain(
    mbus_sim: MBusSimServer,
) -> None:
    _simulated(mbus_sim, 2).status = 0x08 | 0x10
    tags = [
        _tag("volume", "2", "volume", "L"),
        _tag("missing", "2", "energy", "kWh"),
        _tag("other", "3", "voltage", "V"),
    ]
    async with _driver(mbus_sim) as (driver, _):
        values = _by_id(await driver.read(tags))
    volume = values["volume"]
    assert volume.quality is Quality.UNCERTAIN
    assert _raw(volume) == float(_initial(2, "volume"))
    assert volume.error_code == "mbus_meter_status"
    message = volume.error_message or ""
    assert "permanent error" in message and "temporary error" in message
    assert values["missing"].quality is Quality.BAD
    assert values["missing"].error_code == "mbus_record_not_found"
    assert values["other"].quality is Quality.GOOD


async def test_power_low_is_good_and_warned_once_until_it_clears(
    mbus_sim: MBusSimServer, caplog: pytest.LogCaptureFixture
) -> None:
    meter5 = next(m for m in documented_meters() if m.number == 5)
    assert meter5.status == 0x04
    tags = [_tag("volume", "5", "volume", "L"), _tag("m2", "2", "volume", "L")]

    def warnings() -> int:
        return sum(
            r.getMessage() == "mbus_meter_power_low" and r.levelno == logging.WARNING
            for r in caplog.records
        )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        async with _driver(mbus_sim) as (driver, _):
            for _ in range(3):
                values = _by_id(await driver.read(tags))
                assert values["volume"].quality is Quality.GOOD
                assert values["volume"].error_code is None
                assert _raw(values["volume"]) == float(_initial(5, "volume"))
            assert warnings() == 1
            _simulated(mbus_sim, 5).status = 0
            await driver.read(tags)
            assert warnings() == 1
            _simulated(mbus_sim, 5).status = 0x04
            await driver.read(tags)
            await driver.read(tags)
            assert warnings() == 2


# Transport errors and reconnecting


async def test_transport_loss_raises_and_repeats_until_connect(
    mbus_sim: MBusSimServer,
) -> None:
    tags = [_tag("volume", "2", "volume", "L")]
    async with _driver(mbus_sim) as (driver, _):
        bus = driver.bus
        await driver.read(tags)
        await mbus_sim.stop()
        with pytest.raises(TransientCommunicationError) as lost:
            await driver.read(tags)
        assert lost.value.code == "mbus_connection_lost"
        assert not await driver.health_check()
        with pytest.raises(TransientCommunicationError) as repeated:
            await driver.read(tags)
        assert repeated.value.code == "mbus_connection_lost"
        with pytest.raises(TransientCommunicationError) as failed:
            await driver.connect()
        assert failed.value.code == "mbus_connect_failed"
        await mbus_sim.start()
        await driver.connect()
        values = _by_id(await driver.read(tags))
        assert driver.bus is bus
    assert _raw(values["volume"]) == float(_initial(2, "volume"))


async def test_read_before_connect_is_not_connected() -> None:
    driver = MBusDriver(_connection("tcp://127.0.0.1:1"))
    with pytest.raises(TransientCommunicationError) as error:
        await driver.read([_tag("v", "2", "volume", "L")])
    assert error.value.code == "mbus_not_connected"


async def test_worker_reconnects_after_transport_loss(
    mbus_sim: MBusSimServer,
) -> None:
    drivers: list[MBusDriver] = []

    def factory(connection: ConnectionConfig) -> MBusDriver:
        driver = MBusDriver(connection)
        drivers.append(driver)
        return driver

    registry = build_driver_registry([("mbus", factory)])
    worker = ConnectionWorker(
        _connection(mbus_sim.endpoint), registry, retry_sleep=_no_sleep
    )
    group = TagGroupConfig(id="g", connection_id="heat", interval_ms=1000)
    tags = (
        TagConfig(
            id="v",
            tag_group_id="g",
            name="V",
            address="2",
            value_type=ValueType.NUMERIC,
            protocol_options={"quantity": "volume", "unit": "L"},
        ),
    )
    await worker.connect()
    try:
        poll = await worker.poll_group(group, tags)
        assert poll.results[0].quality is Quality.GOOD
        bus = drivers[0].bus
        await mbus_sim.stop()
        await mbus_sim.start()
        poll = await worker.poll_group(group, tags)
        assert poll.results[0].error_code == "mbus_connection_lost"
        poll = await worker.poll_group(group, tags)
        assert poll.results[0].quality is Quality.GOOD
    finally:
        await worker.disconnect()
    assert len(drivers) == 1
    assert drivers[0].bus is bus


async def _no_sleep(_: float) -> None:
    return None


# In memory: the simulator without a socket


class _MemoryTransport:
    """``MBusTransport`` straight onto a ``SimulatedBus``."""

    def __init__(self, bus: simulator.SimulatedBus) -> None:
        self._bus = bus
        self._pending = b""
        self.lose = False
        self.requests: list[int] = []

    async def open(self) -> None:
        self._pending = b""

    async def write(self, data: bytes) -> None:
        if self.lose:
            raise BusTransportError("mbus_connection_lost")
        reader = FrameReader()
        reader.feed(data)
        frame = reader.next_frame()
        if isinstance(frame, ShortFrame) and frame.control & 0x4F == REQ_UD2:
            self.requests.append(frame.address)
        self._pending += self._bus.handle(data) or b""

    async def read_frame(self, timeout_s: float) -> Frame | None:
        reader = FrameReader()
        reader.feed(self._pending)
        self._pending = b""
        return reader.next_frame()

    async def flush_input(self) -> None:
        self._pending = b""

    async def close(self) -> None:
        return None

    def describe(self) -> str:
        return "Memory"


@asynccontextmanager
async def _memory_driver(
    meters: list[simulator.SimulatedMeter],
) -> AsyncIterator[tuple[MBusDriver, _MemoryTransport]]:
    transport = _MemoryTransport(simulator.SimulatedBus(meters, clock=lambda: 0.0))
    driver = MBusDriver(
        _connection("tcp://127.0.0.1:10001"), transport_factory=lambda: transport
    )
    await driver.connect()
    try:
        yield driver, transport
    finally:
        await driver.disconnect()


class _Impostor(simulator.SimulatedMeter):
    """Answers a selection of ``11111111`` while its ID is another."""

    def matches(self, pattern: bytes) -> bool:
        return pattern[:4] == bytes.fromhex("11111111")[::-1]


async def test_secondary_reply_from_another_meter_is_wrong_meter() -> None:
    demo = simulator.demo_meters()[1]
    impostor = _Impostor(
        primary=0, id_digits="12399999", medium=0x07, records=demo.records
    )
    tags = [
        _tag("wrong", "11111111", "volume", "L"),
        _tag("other", "3", "voltage", "V"),
    ]
    async with _memory_driver([impostor, simulator.demo_meters()[2]]) as (driver, _):
        values = _by_id(await driver.read(tags))
    assert values["wrong"].quality is Quality.BAD
    assert values["wrong"].error_code == "mbus_wrong_meter"
    message = values["wrong"].error_message or ""
    assert "11111111" in message and "12399999" in message
    assert values["other"].quality is Quality.GOOD


async def test_primary_addresses_are_read_in_numeric_order() -> None:
    demo = simulator.demo_meters()
    meters = [
        dataclasses.replace(demo[1], primary=10, id_digits="10101010"),
        demo[1],
        demo[2],
    ]
    tags = [
        _tag("ten", "10", "volume", "L"),
        _tag("three", "3", "voltage", "V"),
        _tag("two", "2", "volume", "L"),
    ]
    async with _memory_driver(meters) as (driver, transport):
        values = _by_id(await driver.read(tags))
    assert transport.requests == [2, 3, 10]
    assert all(result.quality is Quality.GOOD for result in values.values())


async def test_wildcard_secondary_address_accepts_a_matching_meter() -> None:
    tags = [_tag("v", "1239FFFF", "volume", "L")]
    async with _memory_driver(simulator.demo_meters()) as (driver, _):
        values = _by_id(await driver.read(tags))
    assert _raw(values["v"]) == float(_initial(2, "volume"))


async def test_memory_transport_loss_propagates_and_bus_stays_the_same() -> None:
    async with _memory_driver(simulator.demo_meters()) as (driver, transport):
        bus: MBusBus = driver.bus
        transport.lose = True
        with pytest.raises(BusTransportError) as error:
            await driver.read([_tag("v", "2", "volume", "L")])
        assert error.value.code == "mbus_connection_lost"
        assert not bus.is_open
        transport.lose = False
        await driver.connect()
        assert driver.bus is bus and bus.is_open
        values = _by_id(await driver.read([_tag("v", "2", "volume", "L")]))
    assert values["v"].quality is Quality.GOOD
