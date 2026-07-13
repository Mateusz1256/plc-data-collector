from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from plc_gateway.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagRequest,
    TransientCommunicationError,
    ValueType,
)
from plc_gateway.protocols import (
    MOCK_PROTOCOL,
    CommunicationDriver,
    DriverRegistry,
    MockDriver,
    create_mock_driver,
)


class FakeSleeper:
    """Deterministic async sleeper for mock driver tests."""

    def __init__(self) -> None:
        """Create a fake sleeper with recorded delays."""
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        """Record requested delay without waiting for real time."""
        self.delays.append(delay)


def fixed_clock() -> datetime:
    return datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def make_connection(
    protocol_options: dict[str, object] | None = None,
    *,
    timeout_ms: int = 1000,
) -> ConnectionConfig:
    return ConnectionConfig(
        id="mock_connection",
        protocol=MOCK_PROTOCOL,
        endpoint="mock://local",
        timeout_ms=timeout_ms,
        protocol_options=protocol_options or {},
    )


def make_tag(
    tag_id: str,
    value_type: ValueType = ValueType.NUMERIC,
    *,
    timeout_ms: int | None = None,
) -> TagRequest:
    return TagRequest(
        tag_id=tag_id,
        address=f"mock://{tag_id}",
        value_type=value_type,
        timeout_ms=timeout_ms,
    )


@pytest.mark.asyncio
async def test_mock_driver_returns_configured_constant_values() -> None:
    driver = MockDriver(
        make_connection(
            {
                "tags": {
                    "temperature": {"value": 20.5},
                    "running": {"value": True},
                }
            }
        ),
        clock=fixed_clock,
    )

    assert isinstance(driver, CommunicationDriver)
    await driver.connect()
    results = await driver.read(
        [
            make_tag("temperature"),
            make_tag("running", ValueType.BOOLEAN),
            make_tag("unknown", ValueType.INTEGER),
        ]
    )

    assert [result.value.raw for result in results if result.value is not None] == [
        20.5,
        True,
        0,
    ]
    assert all(result.received_at == fixed_clock() for result in results)


@pytest.mark.asyncio
async def test_mock_driver_returns_deterministic_value_sequences() -> None:
    driver = MockDriver(
        make_connection({"tags": {"counter": {"sequence": [1, 2]}}}),
        clock=fixed_clock,
    )

    await driver.connect()

    first = await driver.read([make_tag("counter", ValueType.INTEGER)])
    second = await driver.read([make_tag("counter", ValueType.INTEGER)])
    third = await driver.read([make_tag("counter", ValueType.INTEGER)])

    assert first[0].value is not None
    assert second[0].value is not None
    assert third[0].value is not None
    assert first[0].value.raw == 1
    assert second[0].value.raw == 2
    assert third[0].value.raw == 1


@pytest.mark.asyncio
async def test_mock_driver_uses_injected_sleeper_for_artificial_delay() -> None:
    sleeper = FakeSleeper()
    driver = MockDriver(
        make_connection({"delay_ms": 250}),
        clock=fixed_clock,
        sleeper=sleeper,
    )

    await driver.connect()
    await driver.read([make_tag("temperature")])

    assert sleeper.delays == [0.25, 0.25]


@pytest.mark.asyncio
async def test_mock_driver_reports_timeout_without_waiting() -> None:
    sleeper = FakeSleeper()
    driver = MockDriver(
        make_connection({"delay_ms": 250}, timeout_ms=100),
        clock=fixed_clock,
        sleeper=sleeper,
    )

    with pytest.raises(TransientCommunicationError, match="timed out"):
        await driver.connect()

    assert sleeper.delays == []


@pytest.mark.asyncio
async def test_mock_driver_can_fail_then_recover_connection() -> None:
    driver = MockDriver(
        make_connection({"connect_failures_before_success": 1}),
        clock=fixed_clock,
    )

    with pytest.raises(TransientCommunicationError, match="connection failed"):
        await driver.connect()
    assert await driver.health_check() is False

    await driver.connect()
    assert await driver.health_check() is True


@pytest.mark.asyncio
async def test_mock_driver_returns_single_tag_errors() -> None:
    driver = MockDriver(
        make_connection(
            {
                "tags": {
                    "broken": {
                        "error_code": "mock_bad_tag",
                        "error_message": "Configured tag failure",
                    }
                }
            }
        ),
        clock=fixed_clock,
    )

    await driver.connect()
    results = await driver.read([make_tag("broken")])

    assert results[0].is_success is False
    assert results[0].error_code == "mock_bad_tag"
    assert results[0].error_message == "Configured tag failure"


@pytest.mark.asyncio
async def test_mock_driver_rejects_reads_before_connect() -> None:
    driver = MockDriver(make_connection(), clock=fixed_clock)

    with pytest.raises(TransientCommunicationError, match="not connected"):
        await driver.read([make_tag("temperature")])


def test_create_mock_driver_can_be_registered() -> None:
    registry = DriverRegistry()
    registry.register(MOCK_PROTOCOL, create_mock_driver)

    driver = registry.create_driver(make_connection())

    assert isinstance(driver, MockDriver)
    assert driver.capabilities.protocol == MOCK_PROTOCOL


def test_mock_driver_rejects_invalid_behavior_config() -> None:
    with pytest.raises(ConfigurationError, match="only one"):
        MockDriver(
            make_connection(
                {"tags": {"bad": {"value": 1, "sequence": [1, 2]}}},
            )
        )


@pytest.mark.asyncio
async def test_mock_driver_does_not_swallow_cancellation() -> None:
    async def cancelling_sleeper(_: float) -> None:
        raise asyncio.CancelledError

    driver = MockDriver(
        make_connection({"delay_ms": 1}),
        clock=fixed_clock,
        sleeper=cancelling_sleeper,
    )

    with pytest.raises(asyncio.CancelledError):
        await driver.connect()
