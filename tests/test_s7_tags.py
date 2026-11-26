from __future__ import annotations

import pytest

from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    PermanentProtocolError,
    TagConfig,
    TagValue,
    ValueType,
)
from plc_data_collector.protocols.rules import rules_for
from plc_data_collector.protocols.s7_tags import (
    Area,
    DataType,
    S7Address,
    S7ConnectionOptions,
    S7Rules,
    S7TagPlan,
    byte_span,
    decode,
    display_address,
    parse_address,
    parse_step7,
    resolve_tag,
)

DB10_4 = S7Address(Area.DB, 10, 4, None)


def test_area_wire_codes_and_labels() -> None:
    assert [a.wire_code for a in (Area.DB, Area.M, Area.I, Area.Q)] == [
        0x84,
        0x83,
        0x81,
        0x82,
    ]
    assert Area.M.label == "Merkers"


def test_parse_address_canonical() -> None:
    assert parse_address("DB10.4") == DB10_4
    assert parse_address("DB10.4.0") == S7Address(Area.DB, 10, 4, 0)
    assert parse_address("M20.3") == S7Address(Area.M, None, 20, 3)
    assert str(parse_address("Q0.7")) == "Q0.7"
    assert str(DB10_4) == "DB10.4"


@pytest.mark.parametrize(
    "text", ["DB0.1", "DB10.4.8", "M65536", "%MW20", "DB65536.0", "DB10", "M01"]
)
def test_parse_address_refuses(text: str) -> None:
    with pytest.raises(ConfigurationError) as info:
        parse_address(text)
    assert info.value.code == "s7_invalid_address"


@pytest.mark.parametrize(
    ("text", "address", "size"),
    [
        ("%DB10.DBD4", DB10_4, 4),
        ("DB10.DBX4.0", S7Address(Area.DB, 10, 4, 0), 0),
        ("DB10.DBB4", DB10_4, 1),
        ("MW20", S7Address(Area.M, None, 20, None), 2),
        ("%MD20", S7Address(Area.M, None, 20, None), 4),
        ("E0.3", S7Address(Area.I, None, 0, 3), 0),
        ("%I0.3", S7Address(Area.I, None, 0, 3), 0),
        ("AW8", S7Address(Area.Q, None, 8, None), 2),
        ("QB4", S7Address(Area.Q, None, 4, None), 1),
        ("DB10.4", DB10_4, None),
        ("M20", S7Address(Area.M, None, 20, None), None),
    ],
)
def test_parse_step7(text: str, address: S7Address, size: int | None) -> None:
    assert parse_step7(text) == (address, size)


@pytest.mark.parametrize(
    "text", ["DB10.DBX4", "DB10.DBW4.1", "MW20.1", "DB10.DBD", "x"]
)
def test_parse_step7_refuses(text: str) -> None:
    with pytest.raises(ConfigurationError) as info:
        parse_step7(text)
    assert info.value.code == "s7_invalid_address"


def test_display_address() -> None:
    assert display_address(DB10_4, DataType.REAL) == "DB10.DBD4"
    assert display_address(S7Address(Area.DB, 10, 4, 0), DataType.BOOL) == "DB10.DBX4.0"
    assert display_address(DB10_4, DataType.LREAL) == "DB10.DBB4"
    assert display_address(DB10_4, DataType.STRING) == "DB10.DBB4"
    assert display_address(S7Address(Area.M, None, 20, None), DataType.INT) == "MW20"
    assert display_address(S7Address(Area.M, None, 20, 3), DataType.BOOL) == "M20.3"
    assert display_address(S7Address(Area.Q, None, 4, None), DataType.BYTE) == "QB4"


def test_byte_span() -> None:
    assert byte_span(DataType.BOOL, None) == 1
    assert byte_span(DataType.TIME, None) == 4
    assert byte_span(DataType.LWORD, None) == 8
    assert byte_span(DataType.STRING, 10) == 12


def test_resolve_tag_plan() -> None:
    plan = resolve_tag("DB10.4", {"data_type": "real", "scale": 2})
    assert plan == S7TagPlan(
        Area.DB, 10, 4, None, DataType.REAL, 4, None, 2.0, None, ValueType.NUMERIC
    )
    string = resolve_tag("M0", {"data_type": "string", "length": 10})
    assert string.span == 12
    assert string.value_type is ValueType.TEXT


@pytest.mark.parametrize(
    ("address", "options", "code"),
    [
        ("DB10.4", {"data_type": "bool"}, "s7_invalid_address"),
        ("DB10.4.0", {"data_type": "int"}, "s7_invalid_address"),
        ("DB10.4", {"data_type": "string"}, "s7_invalid_options"),
        ("DB10.4", {"data_type": "int", "length": 4}, "s7_invalid_options"),
        ("DB10.4", {"data_type": "int", "scale": 0}, "s7_invalid_options"),
        ("DB10.4.0", {"data_type": "bool", "scale": 2}, "s7_invalid_options"),
        ("DB10.4", {"data_type": "char", "offset": 1}, "s7_invalid_options"),
        (
            "DB10.4",
            {"data_type": "string", "length": 5, "scale": 2},
            "s7_invalid_options",
        ),
        ("M65535", {"data_type": "int"}, "s7_invalid_address"),
        ("M65533", {"data_type": "lreal"}, "s7_invalid_address"),
        ("DB10.4", {}, "s7_invalid_options"),
        ("DB10.4", {"data_type": "int", "unit_id": 1}, "s7_invalid_options"),
        ("DB10.4", {"data_type": "string", "length": True}, "s7_invalid_options"),
        ("DB10.4", {"data_type": "string", "length": 255}, "s7_invalid_options"),
        ("%MW20", {"data_type": "int"}, "s7_invalid_address"),
    ],
)
def test_resolve_tag_refuses(
    address: str, options: dict[str, object], code: str
) -> None:
    with pytest.raises(ConfigurationError) as info:
        resolve_tag(address, options)
    assert info.value.code == code


def test_resolve_tag_edge_fits() -> None:
    assert resolve_tag("M65534", {"data_type": "int"}).span == 2


def _plan(data_type: str, **options: object) -> S7TagPlan:
    address = "DB1.0.3" if data_type == "bool" else "DB1.0"
    return resolve_tag(address, {"data_type": data_type, **options})


INT64_MAX_BYTES = b"\x7f" + b"\xff" * 7


@pytest.mark.parametrize(
    ("data_type", "data", "expected"),
    [
        ("int", b"\xff\xfe", -2),
        ("uint", b"\xff\xfe", 65534),
        ("word", b"\xff\xfe", 65534),
        ("byte", b"\xff", 255),
        ("sint", b"\xff", -1),
        ("usint", b"\xff", 255),
        ("dint", b"\xff\xff\xff\xfe", -2),
        ("udint", b"\xff\xff\xff\xfe", 4294967294),
        ("dword", b"\x00\x00\x01\x00", 256),
        ("lint", INT64_MAX_BYTES, 2**63 - 1),
        ("ulint", INT64_MAX_BYTES, 2**63 - 1),
        ("lword", INT64_MAX_BYTES, 2**63 - 1),
        ("time", b"\xff\xff\xff\xff", -1),
    ],
)
def test_decode_integers(data_type: str, data: bytes, expected: int) -> None:
    assert decode(_plan(data_type), data) == TagValue.integer(expected)


@pytest.mark.parametrize("data_type", ["ulint", "lword"])
def test_decode_unsigned_64_out_of_range(data_type: str) -> None:
    with pytest.raises(PermanentProtocolError) as info:
        decode(_plan(data_type), b"\xff" * 8)
    assert info.value.code == "s7_value_out_of_range"


def test_decode_floats() -> None:
    assert decode(_plan("real"), b"\x3f\xc0\x00\x00") == TagValue.numeric(1.5)
    assert decode(_plan("lreal"), b"\x3f\xf8" + b"\x00" * 6) == TagValue.numeric(1.5)
    with pytest.raises(PermanentProtocolError) as info:
        decode(_plan("real"), b"\x7f\xc0\x00\x00")
    assert info.value.code == "s7_value_not_finite"
    with pytest.raises(PermanentProtocolError) as info:
        decode(_plan("lreal"), b"\x7f\xf0" + b"\x00" * 6)
    assert info.value.code == "s7_value_not_finite"


def test_decode_scale_and_offset() -> None:
    value = decode(_plan("int", scale=0.1), b"\x00\x64")
    assert value.value_type is ValueType.NUMERIC
    assert value.numeric_value == pytest.approx(10.0)
    assert decode(_plan("int", offset=1), b"\x00\x64") == TagValue.numeric(101.0)
    with pytest.raises(PermanentProtocolError) as info:
        decode(_plan("lreal", scale=1e308), b"\x40\x59" + b"\x00" * 6)
    assert info.value.code == "s7_value_not_finite"


def test_decode_bool_and_char() -> None:
    assert decode(_plan("bool"), b"\x08") == TagValue.boolean(True)
    assert decode(_plan("bool"), b"\xf7") == TagValue.boolean(False)
    assert decode(_plan("char"), b"A") == TagValue.text("A")


def test_decode_string() -> None:
    plan = _plan("string", length=10)
    assert decode(plan, b"\x0a\x03abc" + b"\x00" * 7) == TagValue.text("abc")
    assert decode(_plan("string", length=1), b"\x0a\x01\xe9") == TagValue.text("é")
    with pytest.raises(PermanentProtocolError) as info:
        decode(plan, b"\x0a\x0b" + b"a" * 10)
    assert info.value.code == "s7_string_invalid"
    with pytest.raises(PermanentProtocolError) as info:
        decode(_plan("string", length=2), b"\xff\x05ab")
    assert info.value.code == "s7_string_invalid"


def test_decode_wrong_size_is_refused() -> None:
    with pytest.raises(PermanentProtocolError):
        decode(_plan("int"), b"\x00")


@pytest.mark.parametrize(
    ("data_type", "options", "expected"),
    [
        ("bool", {}, ValueType.BOOLEAN),
        ("byte", {}, ValueType.INTEGER),
        ("lword", {}, ValueType.INTEGER),
        ("time", {}, ValueType.INTEGER),
        ("real", {}, ValueType.NUMERIC),
        ("lreal", {}, ValueType.NUMERIC),
        ("char", {}, ValueType.TEXT),
        ("string", {"length": 4}, ValueType.TEXT),
        ("int", {"scale": 2}, ValueType.NUMERIC),
        ("int", {"offset": 1}, ValueType.NUMERIC),
    ],
)
def test_value_types(
    data_type: str, options: dict[str, object], expected: ValueType
) -> None:
    assert _plan(data_type, **options).value_type is expected


def test_connection_options_defaults_and_limits() -> None:
    options = S7ConnectionOptions.from_options({})
    assert (options.rack, options.slot, options.max_gap) == (0, 1, 16)
    for bad in ({"rack": 8}, {"slot": 32}, {"max_gap": 257}, {"x": 1}):
        with pytest.raises(ConfigurationError) as info:
            S7ConnectionOptions.from_options(bad)
        assert info.value.code == "invalid_s7_connection"


def _connection(endpoint: str = "tcp://plc:102") -> ConnectionConfig:
    return ConnectionConfig(id="c", protocol="s7", endpoint=endpoint)


def _tag(address: str, options: dict[str, object], value_type: ValueType) -> TagConfig:
    return TagConfig(
        id="t",
        tag_group_id="g",
        name="t",
        address=address,
        value_type=value_type,
        protocol_options=options,
    )


def test_rules_are_registered_for_s7() -> None:
    assert isinstance(rules_for("s7"), S7Rules)


def test_rules_validate_connection_endpoint() -> None:
    S7Rules().validate_connection(_connection())
    with pytest.raises(ConfigurationError) as info:
        S7Rules().validate_connection(_connection("plc"))
    assert info.value.code == "invalid_s7_endpoint"
    assert info.value.message.startswith("Invalid S7 endpoint 'plc'")
    with pytest.raises(ConfigurationError) as info:
        S7Rules().validate_connection(
            ConnectionConfig(
                id="c",
                protocol="s7",
                endpoint="tcp://plc:102",
                protocol_options={"rack": 9},
            )
        )
    assert info.value.code == "invalid_s7_connection"


def test_rules_validate_tag_normalises_options() -> None:
    tag = _tag("DB1.0", {"data_type": "int", "scale": 2}, ValueType.NUMERIC)
    options = S7Rules().validate_tag(tag, _connection())
    assert options == {"data_type": "int", "scale": 2.0}
    assert isinstance(options["scale"], float)


def test_rules_refuse_value_type_mismatch() -> None:
    tag = _tag("DB1.0", {"data_type": "real"}, ValueType.INTEGER)
    with pytest.raises(ConfigurationError) as info:
        S7Rules().validate_tag(tag, _connection())
    assert info.value.code == "s7_value_type_mismatch"
