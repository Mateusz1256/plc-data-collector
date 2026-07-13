from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from plc_gateway.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagRequest,
    TagResult,
    TagValue,
    ValueType,
)
from plc_gateway.protocols import (
    CommunicationDriver,
    DriverCapabilities,
    DriverRegistry,
    build_driver_registry,
)


class FakeDriver:
    """Minimal test driver implementing the communication contract."""

    def __init__(self, connection: ConnectionConfig) -> None:
        """Create a fake driver for one connection."""
        self.connection = connection
        self.connected = False

    @property
    def capabilities(self) -> DriverCapabilities:
        """Return fake driver capabilities."""
        return DriverCapabilities(protocol=self.connection.protocol)

    async def connect(self) -> None:
        """Mark the fake connection as open."""
        self.connected = True

    async def disconnect(self) -> None:
        """Mark the fake connection as closed."""
        self.connected = False

    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]:
        """Return deterministic values for requested tags."""
        received_at = _utc_now()
        return [
            TagResult.success(
                tag_id=tag.tag_id,
                value=TagValue.numeric(1.0),
                source_timestamp=received_at,
                received_at=received_at,
            )
            for tag in tags
        ]

    async def health_check(self) -> bool:
        """Return whether the fake driver is connected."""
        return self.connected


class CancellingDriver(FakeDriver):
    """Fake driver that exposes cancellation propagation."""

    async def connect(self) -> None:
        """Raise cancellation exactly as an async protocol client might."""
        raise asyncio.CancelledError


def _utc_now() -> datetime:
    return datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


def make_connection(protocol: str = "mock") -> ConnectionConfig:
    return ConnectionConfig(
        id="mock_connection",
        protocol=protocol,
        endpoint="mock://local",
        timeout_ms=1000,
    )


def test_registry_creates_driver_by_protocol_name() -> None:
    registry = DriverRegistry()
    registry.register("mock", FakeDriver)

    driver = registry.create_driver(make_connection())

    assert isinstance(driver, CommunicationDriver)
    assert isinstance(driver, FakeDriver)
    assert driver.connection.id == "mock_connection"
    assert driver.capabilities.protocol == "mock"


def test_registry_normalizes_protocol_names() -> None:
    registry = build_driver_registry([(" Mock ", FakeDriver)])

    driver = registry.create_driver(make_connection("MOCK"))

    assert isinstance(driver, FakeDriver)
    assert registry.registered_protocols() == ("mock",)


def test_unknown_protocol_raises_configuration_error() -> None:
    registry = DriverRegistry()

    with pytest.raises(ConfigurationError, match="protocol 'opcua'"):
        registry.create_driver(make_connection("opcua"))


def test_duplicate_protocol_registration_is_rejected() -> None:
    registry = DriverRegistry()
    registry.register("mock", FakeDriver)

    with pytest.raises(ConfigurationError, match="already registered"):
        registry.register(" MOCK ", FakeDriver)


@pytest.mark.asyncio
async def test_driver_contract_supports_lifecycle_batch_read_and_health() -> None:
    driver = FakeDriver(make_connection())

    assert await driver.health_check() is False
    await driver.connect()
    assert await driver.health_check() is True

    results = await driver.read(
        [
            TagRequest(
                tag_id="temperature",
                address="ns=2;s=Machine.Temperature",
                value_type=ValueType.NUMERIC,
                timeout_ms=500,
            )
        ]
    )

    assert len(results) == 1
    assert results[0].is_success is True
    await driver.disconnect()
    assert await driver.health_check() is False


@pytest.mark.asyncio
async def test_driver_contract_does_not_swallow_cancellation() -> None:
    driver = CancellingDriver(make_connection())

    with pytest.raises(asyncio.CancelledError):
        await driver.connect()
