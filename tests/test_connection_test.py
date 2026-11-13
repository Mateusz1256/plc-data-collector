"""Tests for Test connection: a one-off connect and probe read with a new driver."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from plc_data_collector.app.connection_test import (
    ConnectionTestResult,
    probe_connection,
)
from plc_data_collector.domain import (
    ConnectionConfig,
    TagConfig,
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
    assert "bad_node" in result.message
    assert "ns=2;s=Line1.Temperature" in result.message
    assert "Check" in result.message
    assert SpyMockDriver.instances[0].disconnect_calls == 1


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
    connection = ConnectionConfig(id="x", protocol="modbus", endpoint="tcp://h:502")

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
