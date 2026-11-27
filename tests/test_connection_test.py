"""Tests for Test connection: a one-off connect and probe read with a new driver."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import ClassVar

import pytest
from mbus_support import (
    BusHost,
    mbus_connection,
    memory_host,
    memory_transport,
    running_host,
)
from s7_support import AREA_DB, FakeTransport

from plc_data_collector.app.connection_test import (
    ConnectionTestResult,
    probe_connection,
)
from plc_data_collector.domain import (
    ConnectionConfig,
    TagConfig,
    TagRequest,
    TagResult,
    TransientCommunicationError,
    ValueType,
)
from plc_data_collector.protocols import (
    MOCK_PROTOCOL,
    DriverRegistry,
    MockDriver,
    build_driver_registry,
    default_driver_registry,
)
from plc_data_collector.protocols.s7 import PUT_GET_MESSAGE, S7Driver
from plc_data_collector.simulators import mbus as simulator

AWAIT_LIMIT_S = 10


def _connection(**options: object) -> ConnectionConfig:
    return ConnectionConfig(
        id="line1",
        protocol=MOCK_PROTOCOL,
        endpoint="mock://line1",
        timeout_ms=200,
        protocol_options=options,
    )


def _probe_tag(address: str = "ns=2;s=Line1.Temperature") -> TagConfig:
    return TagConfig(
        id="line1_temperature",
        tag_group_id="fast",
        name="Temperature",
        address=address,
        value_type=ValueType.NUMERIC,
    )


class SpyMockDriver(MockDriver):
    """The mock driver, recording disconnects; ``hang`` makes connect never end."""

    instances: ClassVar[list[SpyMockDriver]] = []

    def __init__(self, connection: ConnectionConfig, *, hang: bool = False) -> None:
        super().__init__(connection)
        self.hang = hang
        self.connect_calls = 0
        self.disconnect_calls = 0
        SpyMockDriver.instances.append(self)

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.hang:
            await asyncio.Event().wait()
        await super().connect()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        await super().disconnect()


@pytest.fixture(autouse=True)
def _reset_spies() -> None:
    SpyMockDriver.instances.clear()


def _spy_registry(*, hang: bool = False) -> DriverRegistry:
    return build_driver_registry(
        [(MOCK_PROTOCOL, lambda connection: SpyMockDriver(connection, hang=hang))]
    )


async def _probe(
    connection: ConnectionConfig,
    registry: DriverRegistry,
    probe_tag: TagConfig | None,
) -> ConnectionTestResult:
    return await asyncio.wait_for(
        probe_connection(connection, registry, probe_tag=probe_tag),
        timeout=AWAIT_LIMIT_S,
    )


async def test_success_reports_elapsed_and_probe() -> None:
    registry = _spy_registry()

    result = await _probe(
        _connection(tags={"line1_temperature": {"value": 21.5}}),
        registry,
        _probe_tag(),
    )

    assert result.ok is True
    assert result.elapsed_ms >= 0
    assert result.message == (
        f"Connected in {result.elapsed_ms} ms. "
        "Read ns=2;s=Line1.Temperature without errors."
    )
    (driver,) = SpyMockDriver.instances
    assert driver.disconnect_calls == 1


async def test_success_without_probe_tag_only_connects() -> None:
    result = await _probe(_connection(), default_driver_registry(), None)

    assert result.ok is True
    assert result.message == f"Connected in {result.elapsed_ms} ms."


async def test_failed_probe_read_names_code_and_address() -> None:
    result = await _probe(
        _connection(
            tags={
                "line1_temperature": {
                    "error_code": "bad_node",
                    "error_message": "No such node.",
                }
            }
        ),
        _spy_registry(),
        _probe_tag(),
    )

    assert result.ok is False
    assert result.message == (
        f"Connected in {result.elapsed_ms} ms, but reading "
        "ns=2;s=Line1.Temperature failed (bad_node). No such node. "
        "Check the Tag address and that the Tag exists on the server."
    )
    assert SpyMockDriver.instances[0].disconnect_calls == 1


async def test_s7_put_get_refusal_is_explained() -> None:
    transport = FakeTransport(memory={(AREA_DB, 1): bytes(10)}, header_error=0x8104)
    connection = ConnectionConfig(
        id="press", protocol="s7", endpoint="tcp://press-plc:102", timeout_ms=200
    )
    registry = build_driver_registry(
        [("s7", lambda c: S7Driver(c, transport_factory=lambda: transport))]
    )
    tag = TagConfig(
        id="press_speed",
        tag_group_id="fast",
        name="Speed",
        address="DB1.0",
        value_type=ValueType.NUMERIC,
        protocol_options={"data_type": "int"},
    )

    result = await _probe(connection, registry, tag)

    assert result.ok is False
    assert "failed (s7_access_denied)" in result.message
    assert PUT_GET_MESSAGE in result.message


async def test_connect_failure_reports_code_and_hint() -> None:
    result = await _probe(
        _connection(connect_failures_before_success=1),
        _spy_registry(),
        None,
    )

    assert result.ok is False
    assert "mock_connection_failed" in result.message
    assert "Check" in result.message
    assert SpyMockDriver.instances[0].disconnect_calls == 1


async def test_timeout_reports_failure_and_disconnects() -> None:
    connection = _connection()  # timeout_ms=200, so the deadline is 1.2 s.
    loop = asyncio.get_running_loop()
    started = loop.time()

    result = await _probe(connection, _spy_registry(hang=True), _probe_tag())

    elapsed_s = loop.time() - started
    assert result.ok is False
    assert "connection_test_timeout" in result.message
    assert "Check" in result.message
    assert 1.1 <= elapsed_s < 5
    (driver,) = SpyMockDriver.instances
    assert driver.connect_calls == 1
    assert driver.disconnect_calls == 1


async def test_message_hides_credentials() -> None:
    connection = ConnectionConfig(
        id="line1",
        protocol=MOCK_PROTOCOL,
        endpoint="mock://operator:hunter2-secret@plc-sim:4840",
        timeout_ms=200,
        protocol_options={
            "connect_failures_before_success": 1,
            "password": "opt-secret-pw",
        },
    )

    failed = await _probe(connection, _spy_registry(), None)
    passed = await _probe(connection, _spy_registry(), None)

    for result in (failed, passed):
        assert "hunter2-secret" not in result.message
        assert "operator" not in result.message
        assert "opt-secret-pw" not in result.message


async def test_unknown_protocol_reports_code_without_driver() -> None:
    connection = ConnectionConfig(id="x", protocol="profinet", endpoint="tcp://h:502")

    result = await _probe(connection, default_driver_registry(), None)

    assert result.ok is False
    assert "unknown_driver_protocol" in result.message


async def test_unexpected_error_reports_type_not_text() -> None:
    class Exploding(MockDriver):
        async def connect(self) -> None:
            raise RuntimeError("opc.tcp://user:leaked-pw@host")

    registry = build_driver_registry([(MOCK_PROTOCOL, Exploding)])

    result = await _probe(_connection(), registry, None)

    assert result.ok is False
    assert "RuntimeError" in result.message
    assert "leaked-pw" not in result.message


async def test_transient_error_message_text_is_not_shown() -> None:
    class Failing(MockDriver):
        async def connect(self) -> None:
            raise TransientCommunicationError(
                "connect to opc.tcp://u:pw-in-text@h failed",
                code="opcua_connect_failed",
            )

    registry = build_driver_registry([(MOCK_PROTOCOL, Failing)])

    result = await _probe(_connection(), registry, None)

    assert "opcua_connect_failed" in result.message
    assert "pw-in-text" not in result.message


async def test_driver_construction_error_reports_type_not_text() -> None:
    def explode(connection: ConnectionConfig) -> MockDriver:
        raise RuntimeError("opc.tcp://user:ctor-leak@host")

    registry = build_driver_registry([(MOCK_PROTOCOL, explode)])

    result = await _probe(_connection(), registry, None)

    assert result.ok is False
    assert "RuntimeError" in result.message
    assert "ctor-leak" not in result.message


async def test_probe_passes_protocol_options() -> None:
    seen: list[TagRequest] = []

    class RecordingDriver(SpyMockDriver):
        async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]:
            seen.extend(tags)
            return await super().read(tags)

    registry = build_driver_registry(
        [(MOCK_PROTOCOL, lambda connection: RecordingDriver(connection))]
    )
    tag = replace(_probe_tag(), protocol_options={"data_type": "int16"})

    await _probe(
        _connection(tags={"line1_temperature": {"value": 21.5}}), registry, tag
    )

    assert [dict(request.protocol_options) for request in seen] == [
        {"data_type": "int16"}
    ]


class DescribingDriver(SpyMockDriver):
    """The mock driver, describing its connection like the S7 driver."""

    def __init__(self, connection: ConnectionConfig, details: str | None) -> None:
        super().__init__(connection)
        self.details = details

    def connection_details(self) -> str | None:
        return self.details


def _describing_registry(details: str | None) -> DriverRegistry:
    return build_driver_registry(
        [(MOCK_PROTOCOL, lambda connection: DescribingDriver(connection, details))]
    )


async def test_success_message_includes_connection_details() -> None:
    registry = _describing_registry("PDU 480 bytes, CPU 6ES7 211-1AE40-0XB0")

    result = await _probe(
        _connection(tags={"line1_temperature": {"value": 21.5}}),
        registry,
        _probe_tag(),
    )

    assert result.ok is True
    assert result.message == (
        f"Connected in {result.elapsed_ms} ms (PDU 480 bytes, CPU "
        "6ES7 211-1AE40-0XB0). Read ns=2;s=Line1.Temperature without errors."
    )


async def test_connection_details_also_shown_when_probe_fails() -> None:
    registry = _describing_registry("PDU 240 bytes")

    result = await _probe(
        _connection(
            tags={
                "line1_temperature": {
                    "error_code": "bad_node",
                    "error_message": "No such node.",
                }
            }
        ),
        registry,
        _probe_tag(),
    )

    assert result.ok is False
    assert result.message.startswith(
        f"Connected in {result.elapsed_ms} ms (PDU 240 bytes), but reading "
    )


async def test_no_connection_details_keeps_plain_message() -> None:
    result = await _probe(_connection(), _describing_registry(None), None)

    assert result.message == f"Connected in {result.elapsed_ms} ms."


# M-Bus: the running worker's bus, or a temporary one


def _meter_tag(
    address: str = "2", quantity: str = "volume", unit: str = "m3"
) -> TagConfig:
    return TagConfig(
        id="heat_volume",
        tag_group_id="meters",
        name="Volume",
        address=address,
        value_type=ValueType.NUMERIC,
        protocol_options={"quantity": quantity, "unit": unit},
    )


async def _mbus_probe(
    host: BusHost, connection: ConnectionConfig, probe_tag: TagConfig | None
) -> ConnectionTestResult:
    return await asyncio.wait_for(
        probe_connection(connection, host.registry, probe_tag=probe_tag, host=host),
        timeout=AWAIT_LIMIT_S,
    )


async def test_mbus_running_connection_is_tested_on_its_bus() -> None:
    transport = memory_transport(simulator.demo_meters())
    async with running_host(transport, mbus_connection()) as (host, _):
        result = await _mbus_probe(host, mbus_connection(), _meter_tag())

        assert result.ok is True, result.message
        assert result.message == (
            f"Connected in {result.elapsed_ms} ms (Memory). Read 2 without errors."
        )
        # No second link: the worker's transport was opened once, and is open.
        assert transport.opens == 1
        assert transport.is_open


async def test_mbus_running_with_other_settings_fails_with_the_panel_message() -> None:
    transport = memory_transport(simulator.demo_meters())
    running = mbus_connection("serial:///dev/ttyUSB0", baud_rate=2400)
    async with running_host(transport, running) as (host, _):
        form = mbus_connection("serial:///dev/ttyUSB0", baud_rate=4800)
        result = await _mbus_probe(host, form, None)

    assert result.ok is False
    assert result.message == (
        "This Connection is running with other settings. Apply your changes "
        "first, or disable the Connection."
    )
    assert transport.opens == 1


async def test_mbus_running_but_disconnected_fails() -> None:
    transport = memory_transport(simulator.demo_meters())
    async with running_host(transport, mbus_connection()) as (host, driver):
        await driver.disconnect()
        result = await _mbus_probe(host, mbus_connection(), None)

    assert result.ok is False
    assert "(mbus_not_connected)" in result.message


async def test_mbus_stopped_connection_is_tested_on_a_temporary_bus() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    result = await _mbus_probe(host, mbus_connection(), None)

    assert result.ok is True
    assert result.message == f"Connected in {result.elapsed_ms} ms (Memory)."
    assert transport.opens == 1
    assert not transport.is_open


async def test_mbus_probe_tag_of_a_silent_meter_fails() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    result = await _mbus_probe(host, mbus_connection(), _meter_tag(address="9"))

    assert result.ok is False
    assert result.message.startswith(
        f"Connected in {result.elapsed_ms} ms (Memory), but reading 9 failed "
        "(mbus_no_reply). Meter 9: No reply to SND_NKE to 9."
    )


async def test_mbus_probe_tag_uses_the_drivers_record_matching() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    result = await _mbus_probe(
        host, mbus_connection(), _meter_tag(quantity="mass", unit="kg")
    )

    assert result.ok is False
    assert "(mbus_record_not_found)" in result.message


async def test_mbus_without_a_host_uses_a_new_driver() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    result = await _probe(mbus_connection(), host.registry, _meter_tag())

    assert result.ok is True, result.message
    assert not transport.is_open
