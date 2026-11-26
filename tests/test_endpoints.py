from __future__ import annotations

import pytest

from plc_data_collector.domain import ConfigurationError
from plc_data_collector.protocols.endpoints import parse_endpoint


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
