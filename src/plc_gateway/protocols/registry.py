"""Registry for protocol driver factories."""

from __future__ import annotations

from collections.abc import Iterable

from plc_gateway.domain import ConfigurationError, ConnectionConfig
from plc_gateway.protocols.driver import CommunicationDriver, DriverFactory


class DriverRegistry:
    """Map protocol names to driver factories."""

    def __init__(self) -> None:
        """Create an empty driver registry."""
        self._factories: dict[str, DriverFactory] = {}

    def register(self, protocol: str, factory: DriverFactory) -> None:
        """Register a factory for a protocol name."""
        normalized = _normalize_protocol(protocol)
        if normalized in self._factories:
            raise ConfigurationError(
                f"Driver for protocol '{normalized}' is already registered.",
                code="duplicate_driver_protocol",
                details={"protocol": normalized},
            )
        self._factories[normalized] = factory

    def create_driver(self, connection: ConnectionConfig) -> CommunicationDriver:
        """Create a driver for a validated connection configuration."""
        protocol = _normalize_protocol(connection.protocol)
        factory = self._factories.get(protocol)
        if factory is None:
            raise ConfigurationError(
                f"No communication driver registered for protocol '{protocol}'.",
                code="unknown_driver_protocol",
                details={"protocol": protocol},
            )
        return factory(connection)

    def registered_protocols(self) -> tuple[str, ...]:
        """Return registered protocol names in deterministic order."""
        return tuple(sorted(self._factories))


def build_driver_registry(
    registrations: Iterable[tuple[str, DriverFactory]],
) -> DriverRegistry:
    """Build a registry from protocol/factory pairs."""
    registry = DriverRegistry()
    for protocol, factory in registrations:
        registry.register(protocol, factory)
    return registry


def _normalize_protocol(protocol: str) -> str:
    normalized = protocol.strip().lower()
    if not normalized:
        raise ConfigurationError(
            "Driver protocol cannot be empty.",
            code="invalid_driver_protocol",
        )
    return normalized
