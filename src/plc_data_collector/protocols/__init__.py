"""Protocol driver contracts and registry helpers."""

from plc_data_collector.protocols.driver import (
    CommunicationDriver,
    DescribesConnection,
    DriverCapabilities,
    DriverFactory,
)
from plc_data_collector.protocols.mock import (
    MOCK_PROTOCOL,
    MockDriver,
    create_mock_driver,
)
from plc_data_collector.protocols.modbus import ModbusDriver, create_modbus_driver
from plc_data_collector.protocols.modbus_tags import MODBUS_PROTOCOL
from plc_data_collector.protocols.opcua import (
    OPCUA_PROTOCOL,
    OpcUaDriver,
    create_opcua_driver,
)
from plc_data_collector.protocols.registry import DriverRegistry, build_driver_registry
from plc_data_collector.protocols.s7 import S7Driver, create_s7_driver
from plc_data_collector.protocols.s7_tags import S7_PROTOCOL


def default_driver_registry() -> DriverRegistry:
    """Return a registry with every protocol driver shipped in this package."""
    return build_driver_registry(
        [
            (MOCK_PROTOCOL, create_mock_driver),
            (OPCUA_PROTOCOL, create_opcua_driver),
            (MODBUS_PROTOCOL, create_modbus_driver),
            (S7_PROTOCOL, create_s7_driver),
        ]
    )


__all__ = [
    "MOCK_PROTOCOL",
    "MODBUS_PROTOCOL",
    "OPCUA_PROTOCOL",
    "S7_PROTOCOL",
    "CommunicationDriver",
    "DescribesConnection",
    "DriverCapabilities",
    "DriverFactory",
    "DriverRegistry",
    "MockDriver",
    "ModbusDriver",
    "OpcUaDriver",
    "S7Driver",
    "build_driver_registry",
    "create_mock_driver",
    "create_modbus_driver",
    "create_opcua_driver",
    "create_s7_driver",
    "default_driver_registry",
]
