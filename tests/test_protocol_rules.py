"""Tests for per-protocol validation rules."""

from __future__ import annotations

import pytest

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.protocols.rules import rules_for, validate_config


def _config(
    *,
    endpoint: str = "tcp://plc:502",
    address: str = "hr:1",
    options: dict[str, object] | None = None,
    value_type: ValueType = ValueType.INTEGER,
    tag_id: str = "p2_temp",
) -> CollectorConfig:
    return CollectorConfig(
        connections=(ConnectionConfig(id="m1", protocol="modbus", endpoint=endpoint),),
        tag_groups=(TagGroupConfig(id="g1", connection_id="m1", interval_ms=1000),),
        tags=(
            TagConfig(
                id=tag_id,
                tag_group_id="g1",
                name=tag_id,
                address=address,
                value_type=value_type,
                protocol_options=options or {"data_type": "int16"},
            ),
        ),
    )


def test_rules_for_unknown_protocol_accepts_anything() -> None:
    connection = ConnectionConfig(id="c", protocol="opcua", endpoint="whatever")
    tag = TagConfig(
        id="t",
        tag_group_id="g",
        name="t",
        address="junk",
        value_type=ValueType.TEXT,
        protocol_options={"anything": 1},
    )
    rules = rules_for("opcua")
    rules.validate_connection(connection)
    rules.validate_tag(tag, connection)
    rules_for("mock").validate_tag(tag, connection)


def test_valid_modbus_config_passes() -> None:
    validate_config(_config())


def test_validate_config_names_bad_modbus_tag() -> None:
    with pytest.raises(ConfigurationError) as info:
        validate_config(_config(options={"data_type": "bool"}))

    assert info.value.message.startswith("Tag 'p2_temp': ")
    assert info.value.code == "invalid_modbus_tag"
    assert info.value.details["tag_id"] == "p2_temp"


def test_validate_config_rejects_value_type_mismatch() -> None:
    with pytest.raises(ConfigurationError) as info:
        validate_config(_config(options={"data_type": "int16", "scale": 0.1}))

    assert info.value.code == "modbus_value_type_mismatch"
    assert "value_type is integer but int16 with scale gives numeric" in str(info.value)


def test_validate_config_rejects_bad_modbus_endpoint() -> None:
    with pytest.raises(ConfigurationError) as info:
        validate_config(_config(endpoint="plc:502"))

    assert info.value.message.startswith("Connection 'm1': ")
    assert info.value.code == "invalid_modbus_endpoint"
    assert info.value.details["connection_id"] == "m1"
