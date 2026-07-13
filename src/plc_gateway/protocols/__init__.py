"""Protocol driver contracts and registry helpers."""

from plc_gateway.protocols.driver import (
    CommunicationDriver,
    DriverCapabilities,
    DriverFactory,
)
from plc_gateway.protocols.mock import MOCK_PROTOCOL, MockDriver, create_mock_driver
from plc_gateway.protocols.registry import DriverRegistry, build_driver_registry

__all__ = [
    "MOCK_PROTOCOL",
    "CommunicationDriver",
    "DriverCapabilities",
    "DriverFactory",
    "DriverRegistry",
    "MockDriver",
    "build_driver_registry",
    "create_mock_driver",
]
