"""Tests for the M-Bus tag model: addresses, options, units and rules."""

from __future__ import annotations

from decimal import Decimal

import pytest

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.protocols.mbus_tags import (
    MBUS_PROTOCOL,
    QUALIFIERS,
    UNIT_FAMILIES,
    Function,
    MBusConnectionOptions,
    MBusRules,
    MBusTagOptions,
    MeterAddress,
    Quantity,
    RecordKey,
    SecondaryAddress,
    canonical_meter_address,
    convert,
    display_meter_address,
    family_of,
    parse_meter_address,
    register_key,
    resolve_tag,
    units_for,
)
from plc_data_collector.protocols.rules import rules_for, validate_config

GATEWAY = ConnectionConfig(id="mb", protocol="mbus", endpoint="tcp://gw:10001")
SERIAL = ConnectionConfig(id="ms", protocol="mbus", endpoint="serial:///dev/ttyUSB0")


def _tag(
    options: dict[str, object] | None = None,
    *,
    address: str = "5",
    value_type: ValueType = ValueType.NUMERIC,
) -> TagConfig:
    return TagConfig(
        id="heat_energy",
        tag_group_id="g",
        name="heat_energy",
        address=address,
        value_type=value_type,
        protocol_options=(
            {"quantity": "energy", "unit": "kWh"} if options is None else options
        ),
    )


# Addresses


@pytest.mark.parametrize(
    ("text", "primary"), [("5", 5), ("0", 0), ("250", 250), (" 7 ", 7)]
)
def test_primary_addresses_parse(text: str, primary: int) -> None:
    assert parse_meter_address(text) == MeterAddress(primary=primary, secondary=None)


def test_secondary_id_parses_with_wildcards_for_the_rest() -> None:
    address = parse_meter_address("12345678")
    assert address.primary is None
    assert address.secondary == SecondaryAddress(
        id_digits="12345678", manufacturer=None, version=None, medium=None
    )


def test_full_secondary_address_parses() -> None:
    address = parse_meter_address("123456782c2d0104")
    assert address.secondary == SecondaryAddress(
        id_digits="12345678", manufacturer=0x2C2D, version=0x01, medium=0x04
    )


def test_wildcard_id_digits_parse() -> None:
    address = parse_meter_address("1234FFFF")
    assert address.secondary is not None
    assert address.secondary.id_digits == "1234FFFF"


@pytest.mark.parametrize(
    "text",
    [
        "251",
        "1234567",
        "P5",
        "S12345678",
        "123456789",
        "",
        "-1",
        "12A45678",
        "12345678 2C2D0104",
        "1234567G2C2D0104",
        "123456782C2D01",
    ],
)
def test_bad_addresses_are_refused(text: str) -> None:
    with pytest.raises(ConfigurationError) as info:
        parse_meter_address(text)
    assert info.value.code == "mbus_invalid_address"


def test_pattern_is_lsb_first_with_wildcards() -> None:
    address = parse_meter_address("12345678")
    assert address.secondary is not None
    assert address.secondary.pattern() == bytes.fromhex("78563412FFFFFFFF")


def test_pattern_of_full_address() -> None:
    address = parse_meter_address("1234FFFF2C2D0104")
    assert address.secondary is not None
    assert address.secondary.pattern() == bytes.fromhex("FFFF34122D2C0104")


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("5", "5"),
        ("005", "5"),
        ("12345678", "12345678"),
        ("1234567840830104", "12345678 PDC heat"),
        ("123456782C2D0107", "12345678 KAM water"),
        ("12345678FFFFFF04", "12345678 heat"),
        ("123456782C2D01FF", "12345678 KAM"),
    ],
)
def test_display(text: str, shown: str) -> None:
    assert display_meter_address(parse_meter_address(text)) == shown


# Quantities, units and conversion


def test_units_for_quantities() -> None:
    assert units_for(Quantity.ENERGY) == (
        "Wh",
        "kWh",
        "MWh",
        "GWh",
        "J",
        "kJ",
        "MJ",
        "GJ",
    )
    assert units_for(Quantity.ON_TIME) == ("s",)
    assert units_for(Quantity.OPERATING_TIME) == ("s",)
    assert units_for(Quantity.VOLUME_FLOW) == ("m3/h", "L/h", "m3/min", "m3/s")
    assert units_for(Quantity.HCA_UNITS) == ()


def test_quantity_names_are_exactly_the_spec_table() -> None:
    assert {q.value for q in Quantity} == {
        "energy",
        "volume",
        "mass",
        "on_time",
        "operating_time",
        "power",
        "volume_flow",
        "mass_flow",
        "flow_temperature",
        "return_temperature",
        "temperature_difference",
        "external_temperature",
        "pressure",
        "hca_units",
        "voltage",
        "current",
    }
    assert {f.value for f in Function} == {
        "instantaneous",
        "maximum",
        "minimum",
        "error",
    }
    assert (
        frozenset({"backward", "forward", "per_hour", "per_minute", "per_second"})
        == QUALIFIERS
    )


def test_energy_families_are_separate() -> None:
    wh = family_of(Quantity.ENERGY, "kWh")
    joule = family_of(Quantity.ENERGY, "GJ")
    assert wh.base_unit == "Wh"
    assert joule.base_unit == "J"
    assert wh != joule
    assert wh.factors["GWh"] == Decimal(10) ** 9
    assert "GJ" not in wh.factors
    assert family_of(Quantity.POWER, "kJ/h").base_unit == "J/h"
    assert family_of(Quantity.POWER, "MW").base_unit == "W"


def test_duration_family_carries_internal_factors() -> None:
    family = family_of(Quantity.ON_TIME, "h")
    assert family.base_unit == "s"
    assert dict(family.factors) == {
        "s": Decimal(1),
        "min": Decimal(60),
        "h": Decimal(3600),
        "d": Decimal(86400),
    }


def test_family_of_unknown_unit_raises() -> None:
    with pytest.raises(KeyError):
        family_of(Quantity.ENERGY, "m3")


def test_every_unit_has_a_family() -> None:
    for quantity in Quantity:
        for unit in units_for(quantity):
            assert family_of(quantity, unit) in UNIT_FAMILIES.values()


def test_convert_is_exact() -> None:
    wh = family_of(Quantity.ENERGY, "Wh")
    assert convert(Decimal("1234567"), "Wh", "kWh", wh) == Decimal("1234.567")
    assert str(convert(Decimal("1234567"), "Wh", "kWh", wh)) == "1234.567"
    flow = family_of(Quantity.VOLUME_FLOW, "m3/h")
    assert convert(Decimal("1.5"), "m3/min", "m3/h", flow) == Decimal("90")
    assert convert(Decimal("2"), "m3/s", "m3/h", flow) == Decimal("7200")
    assert convert(Decimal("250"), "L/h", "m3/h", flow) == Decimal("0.25")
    duration = family_of(Quantity.OPERATING_TIME, "d")
    assert convert(Decimal("2"), "d", "s", duration) == Decimal("172800")


def test_convert_refuses_units_outside_the_family() -> None:
    wh = family_of(Quantity.ENERGY, "Wh")
    with pytest.raises(KeyError):
        convert(Decimal(1), "GJ", "kWh", wh)


# Tag options and resolve_tag


def test_resolve_energy_tag() -> None:
    plan = resolve_tag(_tag(), GATEWAY)
    assert plan.meter == MeterAddress(primary=5, secondary=None)
    assert plan.key == RecordKey(
        quantity=Quantity.ENERGY,
        storage=0,
        tariff=0,
        subunit=0,
        function=Function.INSTANTANEOUS,
        qualifiers=(),
    )
    assert plan.occurrence == 1
    assert plan.unit == "kWh"
    assert plan.family.base_unit == "Wh"
    assert plan.value_type is ValueType.NUMERIC
    assert plan.meter_id is None


def test_on_time_is_integer() -> None:
    plan = resolve_tag(
        _tag({"quantity": "on_time", "unit": "s"}, value_type=ValueType.INTEGER),
        GATEWAY,
    )
    assert plan.value_type is ValueType.INTEGER
    assert plan.family.base_unit == "s"


def test_energy_in_gj_uses_the_joule_family() -> None:
    plan = resolve_tag(_tag({"quantity": "energy", "unit": "GJ"}), GATEWAY)
    assert plan.family.base_unit == "J"


def test_hca_units_have_no_unit() -> None:
    plan = resolve_tag(_tag({"quantity": "hca_units"}), GATEWAY)
    assert plan.unit == ""
    assert plan.value_type is ValueType.NUMERIC


def test_full_options_resolve_and_qualifiers_are_sorted() -> None:
    plan = resolve_tag(
        _tag(
            {
                "quantity": "volume",
                "unit": "L",
                "storage": 2**40,
                "tariff": 2**20,
                "subunit": 2**10,
                "function": "maximum",
                "qualifiers": ["per_hour", "backward"],
                "occurrence": 32,
                "meter_id": "87654321",
            },
            address="12345678",
        ),
        SERIAL,
    )
    assert plan.key.qualifiers == ("backward", "per_hour")
    assert plan.key.function is Function.MAXIMUM
    assert plan.key.storage == 2**40
    assert plan.occurrence == 32
    assert plan.meter_id == "87654321"
    assert hash(plan.key) == hash(plan.key)


@pytest.mark.parametrize(
    "options",
    [
        {"quantity": "energy", "unit": "kWh", "colour": "red"},
        {"quantity": "energy", "unit": "kWh", "scale": 0.1},
        {"quantity": "energy", "unit": "kWh", "offset": 1.0},
        {"quantity": "energy", "unit": "kWh", "function": "avg"},
        {"quantity": "energy", "unit": "kWh", "qualifiers": ["sideways"]},
        {"quantity": "energy", "unit": "kWh", "qualifiers": ["forward", "forward"]},
        {"quantity": "energy", "unit": "kWh", "occurrence": 0},
        {"quantity": "energy", "unit": "kWh", "occurrence": 33},
        {"quantity": "energy", "unit": "kWh", "occurrence": True},
        {"quantity": "energy", "unit": "kWh", "storage": -1},
        {"quantity": "energy", "unit": "kWh", "storage": 2**40 + 1},
        {"quantity": "energy", "unit": "kWh", "storage": "1"},
        {"quantity": "energy", "unit": "kWh", "tariff": 2**20 + 1},
        {"quantity": "energy", "unit": "kWh", "subunit": 2**10 + 1},
        {"quantity": "energy", "unit": "kWh", "meter_id": "1234567"},
        {"quantity": "energy", "unit": "kWh", "meter_id": "1234FFFF"},
        {"quantity": "energy", "unit": "kWh", "meter_id": 12345678},
        {"quantity": "energy"},
        {"quantity": "energy", "unit": "m3"},
        {"quantity": "energy", "unit": "kwh"},
        {"quantity": "on_time", "unit": "h"},
        {"quantity": "hca_units", "unit": "kWh"},
        {"quantity": "heat", "unit": "kWh"},
        {"unit": "kWh"},
    ],
)
def test_bad_options_are_refused(options: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError) as info:
        resolve_tag(_tag(options), GATEWAY)
    assert info.value.code == "mbus_invalid_options"


def test_bad_address_in_tag_is_refused() -> None:
    with pytest.raises(ConfigurationError) as info:
        resolve_tag(_tag(address="P5"), GATEWAY)
    assert info.value.code == "mbus_invalid_address"
    assert "'P5'" in info.value.message


def test_register_key_uses_display_form() -> None:
    first = register_key(_tag(address="005"))
    second = register_key(
        _tag(
            {
                "quantity": "energy",
                "unit": "MWh",
                "meter_id": "12345678",
            },
            address="5",
        )
    )
    assert first == second
    assert first[0] == "5"
    assert first[2] == 1
    other = register_key(_tag({"quantity": "energy", "unit": "kWh", "occurrence": 2}))
    assert other != first


def test_tag_options_model_is_strict() -> None:
    options = MBusTagOptions.model_validate({"quantity": "pressure", "unit": "bar"})
    assert options.storage == 0
    assert options.function is Function.INSTANTANEOUS
    assert options.qualifiers == ()
    assert options.occurrence == 1


# Rules


def test_rules_are_registered() -> None:
    assert MBUS_PROTOCOL == "mbus"
    assert isinstance(rules_for("mbus"), MBusRules)


def test_validate_tag_returns_normalised_options() -> None:
    options = rules_for("mbus").validate_tag(
        _tag(
            {
                "quantity": "energy",
                "unit": "kWh",
                "storage": 0,
                "function": "instantaneous",
                "qualifiers": ["per_hour", "backward"],
                "occurrence": 1,
            }
        ),
        GATEWAY,
    )
    assert options == {
        "quantity": "energy",
        "unit": "kWh",
        "qualifiers": ["backward", "per_hour"],
    }


def test_validate_tag_refuses_value_type_mismatch() -> None:
    with pytest.raises(ConfigurationError) as info:
        rules_for("mbus").validate_tag(
            _tag({"quantity": "on_time", "unit": "s"}, value_type=ValueType.NUMERIC),
            GATEWAY,
        )
    assert info.value.code == "mbus_invalid_options"
    assert "integer" in info.value.message


def test_connection_options_defaults_and_ranges() -> None:
    options = MBusConnectionOptions.from_options({})
    assert (options.baud_rate, options.scan_timeout_ms, options.max_telegrams) == (
        2400,
        500,
        8,
    )
    for bad in (
        {"baud_rate": 2401},
        {"baud_rate": "2400"},
        {"scan_timeout_ms": 49},
        {"scan_timeout_ms": 5001},
        {"max_telegrams": 0},
        {"max_telegrams": 17},
        {"parity": "even"},
    ):
        with pytest.raises(ConfigurationError) as info:
            MBusConnectionOptions.from_options(bad)
        assert info.value.code == "invalid_mbus_connection"


@pytest.mark.parametrize("baud_rate", [300, 600, 1200, 2400, 4800, 9600, 19200, 38400])
def test_baud_rate_accepted_on_serial(baud_rate: int) -> None:
    connection = ConnectionConfig(
        id="ms",
        protocol="mbus",
        endpoint="serial:///dev/ttyUSB0",
        protocol_options={"baud_rate": baud_rate},
    )
    rules_for("mbus").validate_connection(connection)


def test_baud_rate_refused_on_tcp() -> None:
    connection = ConnectionConfig(
        id="mb",
        protocol="mbus",
        endpoint="tcp://gw:10001",
        protocol_options={"baud_rate": 2400},
    )
    with pytest.raises(ConfigurationError) as info:
        rules_for("mbus").validate_connection(connection)
    assert info.value.code == "invalid_mbus_connection"


@pytest.mark.parametrize(
    "endpoint", ["tcp://gw:10001", "serial://COM3", "serial:///dev/ttyUSB0"]
)
def test_good_endpoints(endpoint: str) -> None:
    rules_for("mbus").validate_connection(
        ConnectionConfig(id="m", protocol="mbus", endpoint=endpoint)
    )


@pytest.mark.parametrize(
    "endpoint", ["tcp://gw", "gw:10001", "serial://", "COM3", "udp://gw:1"]
)
def test_bad_endpoints(endpoint: str) -> None:
    with pytest.raises(ConfigurationError) as info:
        rules_for("mbus").validate_connection(
            ConnectionConfig(id="m", protocol="mbus", endpoint=endpoint)
        )
    assert info.value.code == "invalid_mbus_endpoint"
    assert "serial://" in info.value.message


def _mbus_config(*endpoints: str) -> CollectorConfig:
    return CollectorConfig(
        connections=tuple(
            ConnectionConfig(id=f"m{index}", protocol="mbus", endpoint=endpoint)
            for index, endpoint in enumerate(endpoints, start=1)
        ),
        tag_groups=(TagGroupConfig(id="g", connection_id="m1", interval_ms=60000),),
        tags=(_tag(),),
    )


def test_valid_mbus_config_passes() -> None:
    validate_config(_mbus_config("tcp://gw:10001", "tcp://gw:10002", "serial://COM3"))


@pytest.mark.parametrize(
    "endpoints",
    [
        ("tcp://GW:10001", "tcp://gw:10001"),
        ("serial:///dev/ttyUSB0", "serial:///dev/ttyUSB0"),
    ],
)
def test_two_mbus_connections_on_one_endpoint_are_refused(
    endpoints: tuple[str, str],
) -> None:
    with pytest.raises(ConfigurationError) as info:
        validate_config(_mbus_config(*endpoints))
    assert info.value.code == "invalid_mbus_connection"
    assert "'m1'" in info.value.message
    assert "'m2'" in info.value.message
    assert info.value.details["connection_id"] == "m2"


def test_same_endpoint_on_other_protocols_is_not_an_mbus_duplicate() -> None:
    config = CollectorConfig(
        connections=(
            ConnectionConfig(id="m1", protocol="mbus", endpoint="tcp://gw:10001"),
            ConnectionConfig(id="x1", protocol="modbus", endpoint="tcp://gw:10001"),
        ),
        tag_groups=(),
        tags=(),
    )
    validate_config(config)


def test_validate_config_names_bad_mbus_tag() -> None:
    config = CollectorConfig(
        connections=(GATEWAY,),
        tag_groups=(TagGroupConfig(id="g", connection_id="mb", interval_ms=60000),),
        tags=(_tag({"quantity": "energy", "unit": "kWh", "function": "avg"}),),
    )
    with pytest.raises(ConfigurationError) as info:
        validate_config(config)
    assert info.value.code == "mbus_invalid_options"
    assert info.value.message.startswith("Tag 'heat_energy': ")


@pytest.mark.parametrize(
    ("text", "stored"),
    [
        ("5", "5"),
        ("005", "5"),
        (" 250 ", "250"),
        ("12345678", "12345678"),
        ("1234ffff", "1234FFFF"),
        ("12345678FFFFFFFF", "12345678"),
        ("123456782c2d0104", "123456782C2D0104"),
        ("12345678FFFFFF04", "12345678FFFFFF04"),
    ],
)
def test_canonical_meter_address(text: str, stored: str) -> None:
    canonical = canonical_meter_address(parse_meter_address(text))
    assert canonical == stored
    assert parse_meter_address(canonical) == parse_meter_address(text)
