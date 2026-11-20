from __future__ import annotations

import pytest

from plc_data_collector.domain import (
    ConfigurationError,
    PermanentProtocolError,
    TagValue,
    ValueType,
)
from plc_data_collector.protocols.modbus_tags import (
    AddressNumbering,
    Area,
    ByteOrder,
    DataType,
    ModbusConnectionOptions,
    ModbusTagOptions,
    WordOrder,
    decode_bits,
    decode_registers,
    display_address,
    format_endpoint,
    parse_address,
    parse_display_address,
    parse_endpoint,
    resolve_tag,
)

CONN = ModbusConnectionOptions()


def plan(address: str, **options: object):  # type: ignore[no-untyped-def]
    return resolve_tag(address, options, CONN)


@pytest.mark.parametrize("text", ["co:0", "hr:120", "hr:100.3", "ir:65535"])
def test_parse_address_round_trip(text: str) -> None:
    assert str(parse_address(text)) == text


@pytest.mark.parametrize(
    "text", ["hr", "xx:1", "hr:-1", "hr:65536", "hr:1.16", "co:1.0", "hr: 1"]
)
def test_parse_address_rejects(text: str) -> None:
    with pytest.raises(ConfigurationError) as info:
        parse_address(text)
    assert info.value.code == "invalid_modbus_address"


@pytest.mark.parametrize(
    ("area", "address", "numbering", "text"),
    [
        (Area.HOLDING_REGISTERS, 120, AddressNumbering.ZERO, "120"),
        (Area.HOLDING_REGISTERS, 120, AddressNumbering.ONE, "121"),
        (Area.HOLDING_REGISTERS, 120, AddressNumbering.MODICON, "40121"),
        (Area.HOLDING_REGISTERS, 9998, AddressNumbering.MODICON, "49999"),
        (Area.HOLDING_REGISTERS, 9999, AddressNumbering.MODICON, "410000"),
        (Area.INPUT_REGISTERS, 9999, AddressNumbering.MODICON, "310000"),
        (Area.COILS, 0, AddressNumbering.MODICON, "00001"),
    ],
)
def test_display_address(
    area: Area, address: int, numbering: AddressNumbering, text: str
) -> None:
    assert display_address(area, address, numbering) == text
    assert parse_display_address(area, text, numbering) == address


def test_parse_display_address_errors() -> None:
    with pytest.raises(ConfigurationError, match="Input registers"):
        parse_display_address(Area.HOLDING_REGISTERS, "30121", AddressNumbering.MODICON)
    with pytest.raises(ConfigurationError, match="start at 1"):
        parse_display_address(Area.HOLDING_REGISTERS, "0", AddressNumbering.ONE)
    with pytest.raises(ConfigurationError, match="start at 1"):
        parse_display_address(Area.HOLDING_REGISTERS, "40000", AddressNumbering.MODICON)
    for bad in ("abc", "", "65536"):
        with pytest.raises(ConfigurationError) as info:
            parse_display_address(Area.HOLDING_REGISTERS, bad, AddressNumbering.ZERO)
        assert info.value.code == "invalid_modbus_address"


def test_wrong_modicon_message_is_exact() -> None:
    with pytest.raises(ConfigurationError) as info:
        parse_display_address(Area.HOLDING_REGISTERS, "30121", AddressNumbering.MODICON)
    assert info.value.message == (
        "Modicon address 30121 is an input register; "
        "choose Input registers (FC04) or use 4xxxx."
    )


def test_resolve_tag_spans_and_value_types() -> None:
    p = plan("hr:120", data_type="float32")
    assert (p.span, p.value_type) == (2, ValueType.NUMERIC)
    assert (p.unit_id, p.byte_order, p.word_order) == (1, ByteOrder.AB, WordOrder.HI)
    assert plan("ir:10", data_type="uint32", word_order="lo").word_order == WordOrder.LO
    assert plan("hr:5", data_type="int16", scale=0.1).value_type == ValueType.NUMERIC
    assert plan("hr:5", data_type="int16").value_type == ValueType.INTEGER
    p = plan("di:7", data_type="bool")
    assert (p.value_type, p.span) == (ValueType.BOOLEAN, 1)
    p = plan("ir:200", data_type="string", length=10, unit_id=3)
    assert (p.span, p.unit_id, p.value_type) == (10, 3, ValueType.TEXT)
    p = plan("hr:100.3", data_type="bit")
    assert (p.bit, p.span, p.value_type) == (3, 1, ValueType.BOOLEAN)
    conn = ModbusConnectionOptions(
        unit_id=9, byte_order=ByteOrder.BA, word_order=WordOrder.LO
    )
    p = resolve_tag("hr:1", {"data_type": "int32"}, conn)
    assert (p.unit_id, p.byte_order, p.word_order) == (9, ByteOrder.BA, WordOrder.LO)


@pytest.mark.parametrize(
    ("address", "options", "ok"),
    [
        ("hr:65535", {"data_type": "uint16"}, True),
        ("hr:65535", {"data_type": "float32"}, False),
        ("hr:65532", {"data_type": "float64"}, True),
        ("hr:65533", {"data_type": "float64"}, False),
        ("hr:0", {"data_type": "string", "length": 125}, True),
        ("hr:0", {"data_type": "string", "length": 126}, False),
    ],
)
def test_resolve_tag_boundaries(
    address: str, options: dict[str, object], ok: bool
) -> None:
    if ok:
        resolve_tag(address, options, CONN)
    else:
        with pytest.raises(ConfigurationError) as info:
            resolve_tag(address, options, CONN)
        assert info.value.code == "invalid_modbus_tag"


@pytest.mark.parametrize(
    ("address", "options"),
    [
        ("hr:1", {"data_type": "bool"}),
        ("co:1", {"data_type": "int16"}),
        ("hr:1", {"data_type": "bit"}),
        ("hr:1.2", {"data_type": "int16"}),
        ("hr:1", {"data_type": "string"}),
        ("hr:1", {"data_type": "int16", "length": 2}),
        ("hr:1.2", {"data_type": "bit", "scale": 2}),
        ("hr:1", {"data_type": "int16", "scale": 0}),
        ("hr:1", {"data_type": "int16", "scale": float("nan")}),
        ("hr:1", {"data_type": "int16", "offset": float("inf")}),
        ("co:1", {"data_type": "bool", "offset": 1}),
        ("hr:1", {"data_type": "string", "length": 2, "scale": 2}),
        ("hr:1", {"data_type": "int16", "bogus": 1}),
        ("hr:1", {"data_type": "int16", "unit_id": 256}),
        ("hr:1", {}),
        ("zz:1", {"data_type": "int16"}),
    ],
)
def test_resolve_tag_rejections(address: str, options: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError) as info:
        resolve_tag(address, options, CONN)
    assert info.value.code == "invalid_modbus_tag"
    assert "ValidationError" not in info.value.message
    assert "pydantic" not in info.value.message


def test_resolve_tag_validation_message_names_field() -> None:
    with pytest.raises(ConfigurationError) as info:
        resolve_tag("hr:1", {"data_type": "int16", "unit_id": 256}, CONN)
    assert "unit_id" in info.value.message


def test_tag_options_to_options() -> None:
    opts = ModbusTagOptions(
        data_type=DataType.FLOAT32, word_order=WordOrder.LO, scale=0.5
    )
    assert opts.to_options() == {
        "data_type": "float32",
        "word_order": "lo",
        "scale": 0.5,
    }


@pytest.mark.parametrize(
    ("address", "options", "registers", "expected"),
    [
        ("hr:0", {"data_type": "float32"}, [0x3FC0, 0x0000], 1.5),
        ("hr:0", {"data_type": "float32", "word_order": "lo"}, [0x0000, 0x3FC0], 1.5),
        ("hr:0", {"data_type": "float32", "byte_order": "ba"}, [0xC03F, 0x0000], 1.5),
        ("hr:0", {"data_type": "int16"}, [0xFFFE], -2),
        ("hr:0", {"data_type": "uint16"}, [0xFFFE], 65534),
        ("hr:0", {"data_type": "int32"}, [0xFFFF, 0xFFFE], -2),
        ("hr:0", {"data_type": "uint32"}, [0x0001, 0x1170], 70000),
        ("hr:0", {"data_type": "int64"}, [0xFFFF] * 4, -1),
        ("hr:0", {"data_type": "uint64", "word_order": "lo"}, [0x0001, 0, 0, 0], 1),
        ("hr:0", {"data_type": "float64"}, [0x3FF0, 0, 0, 0], 1.0),
        ("hr:0.3", {"data_type": "bit"}, [0x0008], True),
        ("hr:0.3", {"data_type": "bit"}, [0xFFF7], False),
        ("hr:0", {"data_type": "string", "length": 3}, [0x4142, 0x4300, 0x2020], "ABC"),
        (
            "hr:0",
            {"data_type": "string", "length": 1, "byte_order": "ba"},
            [0x4241],
            "AB",
        ),
        ("hr:0", {"data_type": "string", "length": 2}, [0x4142, 0x2020], "AB"),
    ],
)
def test_decode_registers(
    address: str, options: dict[str, object], registers: list[int], expected: object
) -> None:
    value = decode_registers(resolve_tag(address, options, CONN), registers)
    assert value.raw == expected
    if isinstance(expected, bool):
        assert value.value_type == ValueType.BOOLEAN
    elif isinstance(expected, int):
        assert value.value_type == ValueType.INTEGER
    elif isinstance(expected, float):
        assert value.value_type == ValueType.NUMERIC


def test_decode_scale_and_offset() -> None:
    p = plan("hr:0", data_type="int16", scale=0.1, offset=0)
    value = decode_registers(p, [2345])
    assert value.value_type == ValueType.NUMERIC
    assert value.numeric_value == pytest.approx(234.5)
    p = plan("hr:0", data_type="uint16", offset=-10)
    assert decode_registers(p, [15]).numeric_value == pytest.approx(5.0)


def test_decode_rejects_non_ascii_string() -> None:
    p = plan("hr:0", data_type="string", length=1)
    with pytest.raises(PermanentProtocolError) as info:
        decode_registers(p, [0xC3A9])
    assert info.value.code == "modbus_string_not_ascii"


@pytest.mark.parametrize("registers", [[0x7FC0, 0], [0x7F80, 0], [0xFF80, 0]])
def test_decode_non_finite_float(registers: list[int]) -> None:
    p = plan("hr:0", data_type="float32")
    with pytest.raises(PermanentProtocolError) as info:
        decode_registers(p, registers)
    assert info.value.code == "modbus_value_not_finite"


def test_decode_non_finite_after_scaling() -> None:
    p = plan("hr:0", data_type="float32", scale=1e300)
    with pytest.raises(PermanentProtocolError) as info:
        decode_registers(p, [0x7F00, 0])
    assert info.value.code == "modbus_value_not_finite"


def test_decode_bits() -> None:
    assert decode_bits(plan("di:0", data_type="bool"), [True]) == TagValue.boolean(True)
    assert decode_bits(plan("co:0", data_type="bool"), [False]) == TagValue.boolean(
        False
    )


def test_parse_endpoint() -> None:
    assert parse_endpoint("tcp://plc:502") == ("plc", 502)
    assert parse_endpoint("tcp://[fe80::1]:5020") == ("fe80::1", 5020)
    assert format_endpoint("fe80::1", 502) == "tcp://[fe80::1]:502"
    assert format_endpoint("plc", 502) == "tcp://plc:502"


@pytest.mark.parametrize(
    "endpoint",
    [
        "opc.tcp://plc:502",
        "tcp://plc",
        "tcp://u:p@plc:502",
        "tcp://plc:502/x",
        "tcp://plc:0",
        "tcp://plc:70000",
        "tcp://plc:502?a=1",
        "tcp://:502",
        "",
    ],
)
def test_parse_endpoint_rejects(endpoint: str) -> None:
    with pytest.raises(ConfigurationError) as info:
        parse_endpoint(endpoint)
    assert info.value.code == "invalid_modbus_endpoint"


def test_connection_options_defaults_and_rejections() -> None:
    o = ModbusConnectionOptions.from_options({})
    assert (o.unit_id, o.byte_order, o.word_order, o.address_numbering) == (
        1,
        ByteOrder.AB,
        WordOrder.HI,
        AddressNumbering.ZERO,
    )
    assert (o.max_registers_per_read, o.max_bits_per_read, o.max_gap) == (125, 2000, 0)
    for bad in ({"max_registers_per_read": 126}, {"foo": 1}):
        with pytest.raises(ConfigurationError) as info:
            ModbusConnectionOptions.from_options(bad)
        assert info.value.code == "invalid_modbus_connection"


@pytest.mark.parametrize(("register", "expected"), [(0x0800, True), (0x0008, False)])
def test_decode_bit_applies_byte_order(register: int, expected: bool) -> None:
    p = plan("hr:0.3", data_type="bit", byte_order="ba")
    assert decode_registers(p, [register]).boolean_value is expected


@pytest.mark.parametrize("numbering", list(AddressNumbering))
def test_parse_display_address_huge_input(numbering: AddressNumbering) -> None:
    with pytest.raises(ConfigurationError) as info:
        parse_display_address(Area.HOLDING_REGISTERS, "9" * 5000, numbering)
    assert info.value.code == "invalid_modbus_address"
