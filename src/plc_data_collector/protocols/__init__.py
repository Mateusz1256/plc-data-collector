"""Protocol driver contracts and registry helpers."""

from plc_data_collector.protocols.driver import (
    CommunicationDriver,
    DriverCapabilities,
    DriverFactory,
)
from plc_data_collector.protocols.mock import (
    MOCK_PROTOCOL,
    MockDriver,
    create_mock_driver,
)
from plc_data_collector.protocols.opcua import (
    OPCUA_PROTOCOL,
    OpcUaDriver,
    create_opcua_driver,
)
from plc_data_collector.protocols.registry import DriverRegistry, build_driver_registry


def default_driver_registry() -> DriverRegistry:
    """Return a registry with every protocol driver shipped in this package."""
    return build_driver_registry(
        [
            (MOCK_PROTOCOL, create_mock_driver),
            (OPCUA_PROTOCOL, create_opcua_driver),
        ]
    )


__all__ = [
    "MOCK_PROTOCOL",
    "OPCUA_PROTOCOL",
    "CommunicationDriver",
    "DriverCapabilities",
    "DriverFactory",
    "DriverRegistry",
    "MockDriver",
    "OpcUaDriver",
    "build_driver_registry",
    "create_mock_driver",
    "create_opcua_driver",
    "default_driver_registry",
]
