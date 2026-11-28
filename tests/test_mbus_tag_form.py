"""Tests for the Panel's M-Bus Tag form adapter and the battery warning."""

from __future__ import annotations

import pytest

from plc_data_collector.domain import (
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.panel.mbus_tag_form import MBusTagForm, battery_warning
from plc_data_collector.panel.protocol_forms import FormErrors
from plc_data_collector.panel.tag_adapters import tag_adapter_for

GATEWAY = ConnectionConfig(
    id="heat", name="Heat meters", protocol="mbus", endpoint="tcp://gw:10001"
)
GROUPS = [("meters", "meters (15 min)")]
BATTERY_TEXT = (
    "This group polls M-Bus meters every 10 s. Battery-powered meters may drain "
    "or stop answering when read more often than once a minute."
)


def _data(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Heat energy",
        "tag_group": "meters",
        "meter": "12345678",
        "quantity": "energy",
        "unit": "kWh",
        "storage": "1",
        "tariff": "0",
        "subunit": "0",
        "function": "instantaneous",
        "occurrence": "1",
        "meter_id": "",
        "enabled": "on",
    }
    data.update(overrides)
    return data


def _build(data: dict[str, str], existing: TagConfig | None = None) -> TagConfig:
    return MBusTagForm().build(
        data,
        connection=GATEWAY,
        tag_id="t1",
        allowed_group_ids={"meters"},
        existing=existing,
        enabled="enabled" in data,
    )


def _errors(data: dict[str, str]) -> dict[str, str]:
    with pytest.raises(FormErrors) as raised:
        _build(data)
    return raised.value.errors


def _group(interval_ms: int, connection_id: str = "heat") -> TagGroupConfig:
    return TagGroupConfig(
        id="meters",
        connection_id=connection_id,
        interval_ms=interval_ms,
        timeout_ms=min(interval_ms, 800),
    )


def test_adapter_for_mbus() -> None:
    assert isinstance(tag_adapter_for("mbus"), MBusTagForm)
    assert MBusTagForm.columns == (
        "name",
        "meter",
        "quantity",
        "unit",
        "storage",
        "tariff",
        "subunit",
        "function",
        "qualifiers",
        "occurrence",
        "meter_id",
        "tag_group",
        "enabled",
    )
    assert MBusTagForm.required_columns == ("name", "meter", "quantity", "unit")
    assert MBusTagForm.duplicate_noun == "record"


def test_build_energy_kwh_storage_1() -> None:
    tag = _build(_data())

    assert tag.address == "12345678"
    assert dict(tag.protocol_options) == {
        "quantity": "energy",
        "unit": "kWh",
        "storage": 1,
    }
    assert tag.value_type is ValueType.NUMERIC
    assert tag.metadata == {"unit": "kWh"}
    assert tag.enabled is True


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("005", "5"),
        (" 12345678 ", "12345678"),
        ("123456782c2d0104", "123456782C2D0104"),
        ("12345678ffffffff", "12345678"),
    ],
)
def test_build_stores_canonical_meter_address(typed: str, stored: str) -> None:
    assert _build(_data(meter=typed)).address == stored


def test_build_advanced_options_and_qualifier_boxes() -> None:
    tag = _build(
        _data(
            quantity="volume",
            unit="m3",
            storage="",
            tariff="2",
            subunit="1",
            function="maximum",
            occurrence="2",
            meter_id="87654321",
            qualifier_per_hour="on",
            qualifier_forward="on",
        )
    )

    assert dict(tag.protocol_options) == {
        "quantity": "volume",
        "unit": "m3",
        "tariff": 2,
        "subunit": 1,
        "function": "maximum",
        "qualifiers": ["forward", "per_hour"],
        "occurrence": 2,
        "meter_id": "87654321",
    }


def test_build_qualifiers_column_joined_with_plus() -> None:
    tag = _build(_data(qualifiers="per_hour+Forward"))

    assert tag.protocol_options["qualifiers"] == ["forward", "per_hour"]


def test_duration_is_integer_and_unit_case_is_fixed() -> None:
    hours = _build(_data(quantity="operating_time", unit="S", storage="0"))
    energy = _build(_data(unit="mwh"))

    assert hours.value_type is ValueType.INTEGER
    assert hours.metadata == {"unit": "s"}
    assert energy.protocol_options["unit"] == "MWh"


def test_hca_units_has_no_unit() -> None:
    tag = _build(_data(quantity="hca_units", unit="", storage="0"))

    assert "unit" not in tag.protocol_options
    assert "unit" not in tag.metadata
    assert _errors(_data(quantity="hca_units", unit="kWh")) == {
        "unit": "hca_units has no unit; leave Unit empty."
    }


def test_unit_must_belong_to_the_quantity() -> None:
    assert _errors(_data(unit="m3")) == {
        "unit": ("Choose a unit of energy: Wh, kWh, MWh, GWh, J, kJ, MJ, GJ.")
    }
    assert "unit" in _errors(_data(unit=""))


def test_field_errors() -> None:
    errors = _errors(
        _data(
            name="",
            meter="251",
            quantity="heat",
            storage="-1",
            tariff="x",
            subunit="1025",
            function="average",
            occurrence="33",
            meter_id="1234",
            qualifier_backward="on",
            qualifiers="sideways",
        )
    )

    assert set(errors) == {
        "name",
        "meter",
        "quantity",
        "storage",
        "tariff",
        "subunit",
        "function",
        "occurrence",
        "meter_id",
        "qualifiers",
    }
    assert "'251'" in errors["meter"]
    assert errors["storage"] == "Enter a whole number from 0 to 1099511627776."
    assert errors["occurrence"] == "Enter a whole number from 1 to 32."
    assert errors["meter_id"] == "Enter the meter's 8-digit identification number."
    assert "sideways" in errors["qualifiers"]


def test_meter_required() -> None:
    assert _errors(_data(meter=" ")) == {"meter": "Enter the meter address."}


def test_enabled_words_from_a_pasted_row() -> None:
    form = MBusTagForm()
    data = _data(enabled="no")
    tag = form.build(
        data,
        connection=GATEWAY,
        tag_id="t1",
        allowed_group_ids={"meters"},
        existing=None,
    )
    assert tag.enabled is False
    with pytest.raises(FormErrors) as raised:
        form.build(
            _data(enabled="maybe"),
            connection=GATEWAY,
            tag_id="t1",
            allowed_group_ids={"meters"},
            existing=None,
        )
    assert raised.value.errors == {"enabled": "Enter yes or no."}


def test_values_round_trip() -> None:
    form = MBusTagForm()
    saved = _build(
        _data(meter="123456782C2D0104", qualifier_backward="on", meter_id="12345678")
    )

    values = form.values(saved, GATEWAY)

    assert values["meter"] == "123456782C2D0104"
    assert values["quantity"] == "energy"
    assert values["unit"] == "kWh"
    assert values["storage"] == "1"
    assert values["tariff"] == "0"
    assert values["function"] == "instantaneous"
    assert values["occurrence"] == "1"
    assert values["meter_id"] == "12345678"
    assert values["qualifier_backward"] is True
    assert values["qualifier_forward"] is False
    rebuilt = _build({k: v for k, v in _str_values(values).items() if v != ""})
    assert rebuilt.protocol_options == saved.protocol_options
    assert rebuilt.address == saved.address


def _str_values(values: dict[str, object]) -> dict[str, str]:
    data: dict[str, str] = {}
    for key, value in values.items():
        if isinstance(value, bool):
            if value:
                data[key] = "on"
        else:
            data[key] = str(value)
    return data


def test_defaults_and_context() -> None:
    form = MBusTagForm()
    values = form.defaults(GATEWAY, GROUPS)

    assert values["tag_group"] == "meters"
    assert values["quantity"] == ""
    assert values["storage"] == "0"
    assert values["occurrence"] == "1"
    assert values["enabled"] is True
    assert form.template_context(values, GATEWAY)["units"] == ()
    energy = form.template_context({**values, "quantity": "energy"}, GATEWAY)
    units = energy["units"]
    assert isinstance(units, tuple)
    assert "GJ" in units
    assert "m3" not in units


def test_table_cells_and_register_key() -> None:
    form = MBusTagForm()
    tag = _build(_data(meter="1234567840830104", tariff="2"))

    assert form.table_cells(tag, GATEWAY) == (
        "12345678 PDC heat",
        "energy, storage 1, tariff 2",
        "kWh",
    )
    other_unit = _build(_data(meter="1234567840830104", tariff="2", unit="MWh"))
    assert form.register_key(tag, GATEWAY) == form.register_key(other_unit, GATEWAY)
    other_storage = _build(_data(meter="1234567840830104", tariff="2", storage="2"))
    assert form.register_key(tag, GATEWAY) != form.register_key(other_storage, GATEWAY)


def test_next_values_keep_the_meter() -> None:
    form = MBusTagForm()
    tag = _build(_data(meter_id="12345678"))

    values = form.next_values(tag, GATEWAY)

    assert values is not None
    assert values["meter"] == "12345678"
    assert values["meter_id"] == "12345678"
    assert values["tag_group"] == "meters"
    assert values["name"] == ""
    assert values["quantity"] == ""


def test_battery_warning() -> None:
    assert battery_warning(_group(10_000), GATEWAY) == BATTERY_TEXT
    assert battery_warning(_group(60_000), GATEWAY) is None
    assert "every 1.5 s." in (battery_warning(_group(1_500), GATEWAY) or "")
    opcua = ConnectionConfig(id="heat", protocol="opcua", endpoint="opc.tcp://plc:4840")
    assert battery_warning(_group(10_000), opcua) is None
