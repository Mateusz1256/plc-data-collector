from __future__ import annotations

import pytest

from plc_data_collector.domain import ConfigurationError
from plc_data_collector.protocols.endpoints import (
    is_serial_endpoint,
    parse_endpoint,
    parse_serial_endpoint,
)


def test_modbus_message_and_code_are_the_default() -> None:
    with pytest.raises(ConfigurationError) as info:
        parse_endpoint("plc")
    assert info.value.code == "invalid_modbus_endpoint"
    assert info.value.message.startswith("Invalid Modbus endpoint 'plc'")


def test_parse_endpoint_label_names_the_protocol() -> None:
    with pytest.raises(ConfigurationError) as info:
        parse_endpoint("plc", protocol_label="S7")
    assert info.value.code == "invalid_s7_endpoint"
    assert info.value.message.startswith("Invalid S7 endpoint 'plc'")


def test_parse_endpoint_with_mbus_label() -> None:
    assert parse_endpoint("tcp://gw:10001", protocol_label="M-Bus") == ("gw", 10001)
    with pytest.raises(ConfigurationError) as info:
        parse_endpoint("gw", protocol_label="M-Bus")
    assert info.value.code == "invalid_mbus_endpoint"
    assert info.value.message.startswith("Invalid M-Bus endpoint 'gw'")


@pytest.mark.parametrize(
    ("endpoint", "device"),
    [
        ("serial:///dev/ttyUSB0", "/dev/ttyUSB0"),
        ("serial://COM3", "COM3"),
        ("SERIAL://COM3", "COM3"),
        (
            "serial:///dev/serial/by-id/usb-FTDI_FT232R-if00-port0",
            "/dev/serial/by-id/usb-FTDI_FT232R-if00-port0",
        ),
    ],
)
def test_parse_serial_endpoint(endpoint: str, device: str) -> None:
    assert is_serial_endpoint(endpoint)
    assert parse_serial_endpoint(endpoint) == device


@pytest.mark.parametrize(
    "endpoint",
    [
        "serial://",
        "serial:// COM3",
        "serial://COM 3",
        "serial://COM3?x=1",
        "serial://COM3#a",
        "COM3",
        "tcp://gw:1",
    ],
)
def test_parse_serial_endpoint_refuses(endpoint: str) -> None:
    with pytest.raises(ConfigurationError) as info:
        parse_serial_endpoint(endpoint)
    assert info.value.code == "invalid_serial_endpoint"


def test_is_serial_endpoint() -> None:
    assert not is_serial_endpoint("tcp://gw:10001")
    assert not is_serial_endpoint("COM3")
