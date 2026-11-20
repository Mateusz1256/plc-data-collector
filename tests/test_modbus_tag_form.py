"""Tests for the Panel's Tag form adapters (generic and Modbus)."""

from __future__ import annotations

import pytest

from plc_data_collector.domain import ConnectionConfig, TagConfig, ValueType
from plc_data_collector.panel.modbus_tag_form import ModbusTagForm, parse_read_from
from plc_data_collector.panel.protocol_forms import FormErrors
from plc_data_collector.panel.tag_adapters import tag_adapter_for
from plc_data_collector.panel.tag_forms import GenericTagForm
from plc_data_collector.protocols.modbus_tags import Area

GROUPS = [("press_fast", "press_fast (500 ms)"), ("press_slow", "press_slow (10 s)")]


def _connection(numbering: str = "modicon", **options: object) -> ConnectionConfig:
    return ConnectionConfig(
        id="press2",
        name="Press 2",
        protocol="modbus",
        endpoint="tcp://press2-plc:502",
        protocol_options={"address_numbering": numbering, **options},
    )


def _data(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Oil temperature",
        "tag_group": "press_fast",
        "read_from": "hr",
        "address": "40121",
        "data_type": "float32",
        "unit": "C",
        "enabled": "on",
    }
    data.update(overrides)
    return data


def _build(data: dict[str, str], numbering: str = "modicon") -> TagConfig:
    return ModbusTagForm().build(
        data,
        connection=_connection(numbering),
        tag_id="t1",
        allowed_group_ids={"press_fast", "press_slow"},
        existing=None,
    )


def test_adapter_for_protocol() -> None:
    assert isinstance(tag_adapter_for("modbus"), ModbusTagForm)
    assert isinstance(tag_adapter_for("opcua"), GenericTagForm)
    assert isinstance(tag_adapter_for("mock"), GenericTagForm)


def test_columns() -> None:
    assert ModbusTagForm.columns == (
        "name",
        "tag_group",
        "read_from",
        "address",
        "data_type",
        "length",
        "bit",
        "byte_order",
        "word_order",
        "unit_id",
        "scale",
        "offset",
        "unit",
    )
    assert ModbusTagForm.required_columns == (
        "name",
        "read_from",
        "address",
        "data_type",
    )
    assert GenericTagForm.columns == (
        "name",
        "tag_group",
        "address",
        "value_type",
        "unit",
    )
    assert GenericTagForm.required_columns == ("name", "address", "value_type")


@pytest.mark.parametrize(
    ("text", "area"),
    [
        ("hr", Area.HOLDING_REGISTERS),
        ("HR", Area.HOLDING_REGISTERS),
        (" co ", Area.COILS),
        ("FC02", Area.DISCRETE_INPUTS),
        ("fc04", Area.INPUT_REGISTERS),
        ("Holding registers (FC03)", Area.HOLDING_REGISTERS),
        ("coils (fc01)", Area.COILS),
    ],
)
def test_parse_read_from(text: str, area: Area) -> None:
    assert parse_read_from(text) is area


@pytest.mark.parametrize("text", ["", "FC05", "FC3", "holding", "x"])
def test_parse_read_from_refuses(text: str) -> None:
    assert parse_read_from(text) is None


def test_build_with_overrides() -> None:
    tag = _build(
        _data(
            read_from="FC04",
            address="30011",
            data_type="uint32",
            byte_order="ba",
            word_order="lo",
            unit_id="7",
        )
    )

    assert tag.address == "ir:10"
    assert dict(tag.protocol_options) == {
        "data_type": "uint32",
        "byte_order": "ba",
        "word_order": "lo",
        "unit_id": 7,
    }
    assert tag.value_type is ValueType.INTEGER
    assert tag.enabled is True


def test_build_string_with_length() -> None:
    tag = _build(_data(address="10", data_type="string", length="8"), "zero")

    assert tag.address == "hr:10"
    assert dict(tag.protocol_options) == {"data_type": "string", "length": 8}
    assert tag.value_type is ValueType.TEXT


def test_build_refuses_unknown_group() -> None:
    with pytest.raises(FormErrors) as raised:
        _build(_data(tag_group="elsewhere"))
    assert "tag_group" in raised.value.errors


def test_build_scale_on_bool_is_a_scale_error() -> None:
    with pytest.raises(FormErrors) as raised:
        _build(_data(read_from="co", address="00001", data_type="bool", scale="2"))
    assert raised.value.errors == {"scale": "Scale isn't used with bool."}


def test_build_offset_on_bool_is_an_offset_error() -> None:
    with pytest.raises(FormErrors) as raised:
        _build(_data(read_from="co", address="00001", data_type="bool", offset="1"))
    assert raised.value.errors == {"offset": "Offset isn't used with bool."}


def test_build_address_past_the_end_is_an_address_error() -> None:
    with pytest.raises(FormErrors) as raised:
        _build(_data(address="65535", data_type="float32"), "zero")
    assert set(raised.value.errors) == {"address"}


def test_values_round_trip() -> None:
    form = ModbusTagForm()
    connection = _connection("one")
    tag = _build(
        _data(
            address="121", data_type="int16", scale="0.1", offset="-3.5", unit_id="0"
        ),
        "one",
    )

    values = form.values(tag, connection)

    assert values["read_from"] == "hr"
    assert values["address"] == "121"
    assert values["scale"] == "0.1"
    assert values["offset"] == "-3.5"
    assert values["unit_id"] == "0"
    data = {k: str(v) for k, v in values.items() if k != "enabled"}
    data["enabled"] = "on"
    again = form.build(
        data,
        connection=connection,
        tag_id="t1",
        allowed_group_ids={"press_fast"},
        existing=tag,
    )
    assert again == tag


def test_next_values_steps_by_span() -> None:
    form = ModbusTagForm()
    connection = _connection("zero")
    tag = _build(_data(address="10", data_type="float64", word_order="lo"), "zero")

    values = form.next_values(tag, connection)

    assert values is not None
    assert values["address"] == "14"
    assert values["name"] == ""
    assert values["data_type"] == "float64"
    assert values["word_order"] == "lo"
    assert values["tag_group"] == "press_fast"
    assert values["unit"] == "C"


def test_next_values_for_bit_steps_the_bit() -> None:
    form = ModbusTagForm()
    connection = _connection("zero")
    tag = _build(_data(address="100", data_type="bit", bit="15"), "zero")

    values = form.next_values(tag, connection)

    assert values is not None
    assert (values["address"], values["bit"]) == ("101", "0")


def test_next_values_at_the_end_leaves_address_empty() -> None:
    form = ModbusTagForm()
    tag = _build(_data(address="65535", data_type="int16"), "zero")

    values = form.next_values(tag, _connection("zero"))

    assert values is not None
    assert values["address"] == ""


def test_table_cells() -> None:
    form = ModbusTagForm()
    tag = _build(_data(read_from="di", address="10008", data_type="bool"))

    assert form.table_cells(tag, _connection("modicon")) == (
        "Discrete inputs (FC02)",
        "10008",
        "bool",
    )
    assert form.table_cells(tag, _connection("zero")) == (
        "Discrete inputs (FC02)",
        "7",
        "bool",
    )


def test_generic_adapter_matches_old_form() -> None:
    form = GenericTagForm()
    connection = ConnectionConfig(
        id="line1", name="Line 1", protocol="opcua", endpoint="opc.tcp://x:4840"
    )
    defaults = form.defaults(connection, GROUPS)
    assert defaults["tag_group"] == "press_fast"
    assert defaults["value_type"] == "numeric"
    tag = form.build(
        {
            "name": "T",
            "tag_group": "press_fast",
            "address": "ns=2;s=T",
            "value_type": "numeric",
            "enabled": "on",
        },
        connection=connection,
        tag_id="t1",
        allowed_group_ids={"press_fast"},
        existing=None,
    )
    assert form.table_cells(tag, connection) == ("", "ns=2;s=T", "numeric")
    assert form.next_values(tag, connection) is None
    assert form.values(tag, connection)["tag_group"] == "press_fast"


def test_build_from_columns_only_is_enabled() -> None:
    modbus = ModbusTagForm()
    row = dict.fromkeys(modbus.columns, "")
    row.update(_data())
    del row["enabled"]
    assert set(row) == set(modbus.columns)
    tag = modbus.build(
        row,
        connection=_connection(),
        tag_id="t1",
        allowed_group_ids={"press_fast"},
        existing=None,
    )
    assert tag.enabled is True

    generic = GenericTagForm()
    opcua_row = {
        "name": "T",
        "tag_group": "press_fast",
        "address": "ns=2;s=T",
        "value_type": "numeric",
        "unit": "",
    }
    assert set(opcua_row) == set(generic.columns)
    opcua = ConnectionConfig(
        id="line1", name="Line 1", protocol="opcua", endpoint="opc.tcp://x:4840"
    )
    tag = generic.build(
        opcua_row,
        connection=opcua,
        tag_id="t1",
        allowed_group_ids={"press_fast"},
        existing=None,
    )
    assert tag.enabled is True


def test_build_enabled_false() -> None:
    tag = ModbusTagForm().build(
        _data(),
        connection=_connection(),
        tag_id="t1",
        allowed_group_ids={"press_fast"},
        existing=None,
        enabled=False,
    )
    assert tag.enabled is False


def test_next_values_clear_unused_bit_and_length() -> None:
    form = ModbusTagForm()
    tag = _build(_data(address="10", data_type="string", length="8"), "zero")

    values = form.next_values(tag, _connection("zero"))

    assert values is not None
    assert (values["address"], values["length"], values["bit"]) == ("18", "8", "")
