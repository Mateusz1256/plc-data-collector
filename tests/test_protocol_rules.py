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


def _opcua_tag(options: dict[str, object]) -> TagConfig:
    return TagConfig(
        id="t",
        tag_group_id="g",
        name="t",
        address="junk",
        value_type=ValueType.TEXT,
        protocol_options=options,
    )


def test_permissive_rules_accept_any_address() -> None:
    connection = ConnectionConfig(id="c", protocol="opcua", endpoint="whatever")
    rules = rules_for("opcua")
    rules.validate_connection(connection)
    rules.validate_tag(_opcua_tag({}), connection)
    rules_for("mock").validate_tag(_opcua_tag({"anything": 1}), connection)


def test_opcua_rules_refuse_tag_protocol_options() -> None:
    connection = ConnectionConfig(id="c", protocol="opcua", endpoint="whatever")
    with pytest.raises(ConfigurationError) as info:
        rules_for("opcua").validate_tag(_opcua_tag({"anything": 1}), connection)
    assert info.value.code == "opcua_tag_options_not_supported"
    assert info.value.message == (
        "OPC UA Tags take no protocol_options; remove 'anything'."
    )


def test_validate_config_refuses_opcua_tag_options() -> None:
    config = CollectorConfig(
        connections=(ConnectionConfig(id="o1", protocol="opcua", endpoint="x"),),
        tag_groups=(TagGroupConfig(id="g", connection_id="o1", interval_ms=1000),),
        tags=(_opcua_tag({"data_type": "int16"}),),
    )
    with pytest.raises(ConfigurationError) as info:
        validate_config(config)
    assert info.value.message.startswith("Tag 't': OPC UA Tags take no")
    assert info.value.details["tag_id"] == "t"


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


@pytest.mark.parametrize(
    "options",
    [
        {"data_type": "int16", "unit_id": True},
        {"data_type": "int16", "unit_id": "2"},
        {"data_type": "int16", "unit_id": 2.0},
        {"data_type": "string", "length": True},
        {"data_type": "int16", "scale": True},
        {"data_type": "int16", "offset": "1"},
    ],
)
def test_modbus_tag_options_are_not_coerced(options: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError) as info:
        validate_config(_config(options=options, value_type=ValueType.NUMERIC))
    assert info.value.code == "invalid_modbus_tag"


def test_modbus_validate_tag_returns_normalised_options() -> None:
    config = _config(
        options={
            "data_type": "int16",
            "unit_id": 2,
            "scale": 2,
            "byte_order": None,
            "word_order": None,
        },
        value_type=ValueType.NUMERIC,
    )
    connection = config.connections[0]

    options = rules_for("modbus").validate_tag(config.tags[0], connection)

    assert options == {"data_type": "int16", "unit_id": 2, "scale": 2.0}
    assert isinstance(options["scale"], float)


class _FakeRules:
    def validate_connection(self, connection: ConnectionConfig) -> None:
        return None

    def validate_tag(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> dict[str, object]:
        return {}


def test_rules_for_returns_registered_rules() -> None:
    from plc_data_collector.protocols.rules import register_rules

    fake = _FakeRules()
    register_rules("fake", fake)
    assert rules_for("fake") is fake


def test_unknown_protocol_is_permissive() -> None:
    tag = TagConfig(
        id="t",
        tag_group_id="g",
        name="t",
        address="x",
        value_type=ValueType.INTEGER,
        protocol_options={"a": 1},
    )
    connection = ConnectionConfig(id="c", protocol="nope", endpoint="x")
    rules = rules_for("nope")
    rules.validate_connection(connection)
    assert rules.validate_tag(tag, connection) == {"a": 1}


def test_builtin_rules_registered() -> None:
    from plc_data_collector.protocols.modbus_tags import ModbusRules

    assert isinstance(rules_for("modbus"), ModbusRules)
    tag = TagConfig(
        id="t",
        tag_group_id="g",
        name="t",
        address="x",
        value_type=ValueType.INTEGER,
        protocol_options={"a": 1},
    )
    connection = ConnectionConfig(id="c", protocol="opcua", endpoint="x")
    with pytest.raises(ConfigurationError) as info:
        rules_for("opcua").validate_tag(tag, connection)
    assert info.value.code == "opcua_tag_options_not_supported"
