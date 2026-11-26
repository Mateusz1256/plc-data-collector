"""Tests for pasting Tags from CSV (parsing only; no store, no routes)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from plc_data_collector.domain import ConnectionConfig, TagConfig, ValueType
from plc_data_collector.panel.tag_adapters import tag_adapter_for
from plc_data_collector.panel.tag_paste import (
    MAX_PASTE_ROWS,
    PasteResult,
    detect_delimiter,
    example_paste,
    parse_paste,
)

MODBUS = ConnectionConfig(
    id="press2",
    name="Press 2",
    protocol="modbus",
    endpoint="tcp://press2-plc:502",
    protocol_options={"address_numbering": "modicon"},
)
S7 = ConnectionConfig(
    id="press3",
    name="Press 3",
    protocol="s7",
    endpoint="tcp://press3-plc:102",
)
OPCUA = ConnectionConfig(
    id="line1", name="Line 1", protocol="opcua", endpoint="opc.tcp://plc:4840"
)


def _parse(
    text: str,
    connection: ConnectionConfig = MODBUS,
    taken: frozenset[str] = frozenset(),
    existing: Sequence[TagConfig] = (),
) -> PasteResult:
    return parse_paste(
        text,
        adapter=tag_adapter_for(connection.protocol),
        connection=connection,
        default_group_id="fast",
        allowed_group_ids={"fast", "slow"},
        taken_ids=taken,
        existing_tags=existing,
    )


def _saved(connection: ConnectionConfig, text: str) -> TagConfig:
    """A Tag the Connection already has, built from a one-row paste."""
    (row,) = _parse(text, connection).rows
    assert row.tag is not None
    return row.tag


def test_detect_delimiter() -> None:
    assert detect_delimiter("a\tb;c,d") == "\t"
    assert detect_delimiter("a;b,c") == ";"
    assert detect_delimiter("a,b") == ","


def test_paste_excel_semicolon_crlf_bom_decimal_comma() -> None:
    text = (
        "﻿Name;Read from;Address;Data type;Scale\r\n"
        "Ram position;hr;40107;int16;0,1\r\n\r\n"
    )

    result = _parse(text)

    assert result.ok
    assert len(result.rows) == 1
    tag = result.rows[0].tag
    assert tag is not None
    assert tag.address == "hr:106"
    assert dict(tag.protocol_options) == {"data_type": "int16", "scale": 0.1}
    assert tag.tag_group_id == "fast"


def test_paste_tab_separated_with_quotes() -> None:
    text = 'name\tread_from\taddress\tdata_type\n"Temp, oil"\tFC03\t40121\tfloat32\n'

    result = _parse(text)

    assert result.ok
    assert result.rows[0].tag is not None
    assert result.rows[0].tag.name == "Temp, oil"


def test_paste_unknown_column() -> None:
    result = _parse("name,read_from,address,data_type,colour\n")

    assert result.error is not None
    assert "Unknown column 'colour'" in result.error
    assert not result.ok


def test_paste_missing_required_column_and_empty() -> None:
    assert "address" in (_parse("name,read_from,data_type\nx,hr,int16\n").error or "")
    assert _parse("  \r\n\n").error is not None
    assert _parse("name,read_from,address,data_type\n").error is not None


def test_paste_header_is_normalised() -> None:
    text = "Name,Read From,Address,Data Type,Tag Group ID\nA,hr,40001,int16,slow\n"

    result = _parse(text)

    assert result.ok
    assert result.rows[0].tag is not None
    assert result.rows[0].tag.tag_group_id == "slow"


def test_paste_row_errors_block_ok() -> None:
    text = "name,read_from,address,data_type\nGood,hr,40001,int16\nBad,hr,40002,bool\n"

    result = _parse(text)

    assert result.ok is False
    good, bad = result.rows
    assert good.tag is not None
    assert not good.errors
    assert bad.tag is None
    assert "data_type" in bad.errors
    assert (good.line, bad.line) == (2, 3)


def test_paste_duplicate_register() -> None:
    text = "name,read_from,address,data_type\nA,hr,40121,int16\nB,hr,40121,uint16\n"

    result = _parse(text)

    assert not result.ok
    assert result.rows[0].errors == {}
    assert result.rows[1].errors["address"] == "Same register as line 2."


def test_paste_same_address_other_unit_is_not_a_duplicate() -> None:
    text = (
        "name,read_from,address,data_type,unit_id\n"
        "A,hr,40121,int16,1\n"
        "B,hr,40121,int16,2\n"
    )

    assert _parse(text).ok


def test_paste_generates_unique_ids() -> None:
    text = (
        "name,read_from,address,data_type\nPump,hr,40001,int16\nPump,hr,40002,int16\n"
    )

    result = _parse(text, taken=frozenset({"press2_pump"}))

    ids = [row.tag.id for row in result.rows if row.tag]
    assert len(set(ids)) == 2
    assert "press2_pump" not in ids


def test_paste_row_limit() -> None:
    lines = ["name,read_from,address,data_type"]
    lines += [f"T{i},hr,{40001 + i},int16" for i in range(MAX_PASTE_ROWS + 1)]

    result = _parse("\n".join(lines))

    assert result.error is not None
    assert "1000" in result.error


def test_paste_opcua() -> None:
    text = "name,address,value_type\nTemp,ns=2;s=Temp,numeric\n"

    result = _parse(text.replace("ns=2;s=Temp", '"ns=2;s=Temp"'), OPCUA)

    assert result.ok
    tag = result.rows[0].tag
    assert tag is not None
    assert dict(tag.protocol_options) == {}
    assert tag.address == "ns=2;s=Temp"


def test_paste_extra_cells_is_a_row_error() -> None:
    result = _parse("name,read_from,address,data_type\nA,hr,40001,int16,oops\n")

    assert not result.ok
    assert "form" in result.rows[0].errors


@pytest.mark.parametrize("group", ["nope"])
def test_paste_unknown_group_is_a_row_error(group: str) -> None:
    text = f"name,read_from,address,data_type,tag_group\nA,hr,40001,int16,{group}\n"

    assert "tag_group" in _parse(text).rows[0].errors


def test_paste_huge_field_is_a_paste_error() -> None:
    text = "name,read_from,address,data_type\n" + "x" * 200_000 + ",hr,40001,int16\n"

    result = _parse(text)

    assert result.error is not None
    assert "can't be read as a table" in result.error


def test_paste_lone_cr_line_endings() -> None:
    result = _parse(
        "name,read_from,address,data_type\rA,hr,40001,int16\rB,hr,40002,int16"
    )

    assert result.ok
    assert [row.line for row in result.rows] == [2, 3]


def test_paste_oversize_text() -> None:
    result = _parse("name\n" + "a" * 1_000_001)

    assert result.error is not None
    assert "over 1 MB" in result.error


def test_paste_trailing_delimiters_are_ignored() -> None:
    text = "name;read_from;address;data_type;;\nA;hr;40001;int16;;\n"

    assert _parse(text).ok


def test_paste_value_under_empty_header_is_a_row_error() -> None:
    result = _parse("name;read_from;address;data_type;\nA;hr;40001;int16;oops\n")

    assert result.rows[0].errors == {"form": "This value has no column name."}


def test_paste_multiline_record_reports_first_line() -> None:
    text = 'name,read_from,address,data_type\n"A\nB",hr,40001,int16\nC,hr,40001,int16\n'

    result = _parse(text)

    assert result.rows[0].line == 2
    assert result.rows[1].errors["address"] == "Same register as line 2."


def test_paste_s7_step7_address_with_semicolons() -> None:
    result = _parse("name;address;data_type\nSpeed;%DB10.DBD4;real\n", S7)

    assert result.ok
    tag = result.rows[0].tag
    assert tag is not None
    assert tag.address == "DB10.4"
    assert dict(tag.protocol_options) == {"data_type": "real"}
    assert tag.value_type is ValueType.NUMERIC
    assert tag.tag_group_id == "fast"
    assert tag.enabled is True


def test_paste_s7_all_columns() -> None:
    text = (
        "name;address;data_type;length;scale;offset;unit;tag_group;enabled\n"
        "Recipe;DB5.DBB10;string;20;;;;slow;no\n"
        "Pressure;MW20;int;;0,1;-5;bar;;yes\n"
        "Running;E0.3;bool;;;;;;\n"
    )

    result = _parse(text, S7)

    assert result.ok, [row.errors for row in result.rows]
    recipe, pressure, running = (row.tag for row in result.rows)
    assert recipe is not None and pressure is not None and running is not None
    assert (recipe.address, recipe.tag_group_id, recipe.enabled) == (
        "DB5.10",
        "slow",
        False,
    )
    assert dict(recipe.protocol_options) == {"data_type": "string", "length": 20}
    assert pressure.address == "M20"
    assert dict(pressure.protocol_options) == {
        "data_type": "int",
        "scale": 0.1,
        "offset": -5.0,
    }
    assert pressure.metadata == {"unit": "bar"}
    assert running.address == "I0.3"
    assert running.value_type is ValueType.BOOLEAN


def test_paste_s7_size_letter_contradicting_type_names_both_sizes() -> None:
    result = _parse("name;address;data_type\nBad;DB10.DBW4;real\n", S7)

    assert not result.ok
    assert result.rows[0].errors == {
        "address": "DB10.DBW4 is a 2-byte address; real needs 4 bytes."
    }


@pytest.mark.parametrize(
    ("address", "data_type", "message"),
    [
        ("DB10.DBX4.0", "real", "DB10.DBX4.0 is a bit address; real needs 4 bytes."),
        ("DB10.4.0", "byte", "DB10.4.0 is a bit address; byte needs 1 byte."),
        (
            "DB10.DBX4.0",
            "string",
            "DB10.DBX4.0 is a bit address; string needs a byte address.",
        ),
        (
            "DB10.DBW4",
            "bool",
            "DB10.DBW4 is a 2-byte address; bool needs a bit address (.0-.7).",
        ),
        ("DB10.4", "bool", "DB10.4 has no bit; bool needs a bit address (.0-.7)."),
        ("MD20", "int", "MD20 is a 4-byte address; int needs 2 bytes."),
    ],
)
def test_paste_s7_bit_and_size_contradictions(
    address: str, data_type: str, message: str
) -> None:
    text = f"name;address;data_type;length\nBad;{address};{data_type};8\n"

    result = _parse(text, S7)

    assert result.rows[0].errors["address"] == message


def test_paste_s7_string_ignores_the_size_letter() -> None:
    text = "name;address;data_type;length\nRecipe;DB10.DBW4;string;8\n"

    result = _parse(text, S7)

    assert result.ok
    assert result.rows[0].tag is not None
    assert result.rows[0].tag.address == "DB10.4"


def test_paste_s7_bad_address_and_bad_enabled() -> None:
    text = "name;address;data_type;enabled\nA;XY12;int;\nB;MW2;int;maybe\n"

    result = _parse(text, S7)

    assert "Invalid S7 address 'XY12'" in result.rows[0].errors["address"]
    assert result.rows[1].errors == {"enabled": "Enter yes or no."}


def test_paste_s7_duplicate_compares_canonical_addresses() -> None:
    text = (
        "name;address;data_type\nA;DB10.DBX4.0;bool\nB;DB10.4.1;bool\nC;DB10.4.0;bool\n"
    )

    result = _parse(text, S7)

    assert not result.ok
    assert result.rows[0].errors == {}
    assert result.rows[1].errors == {}
    assert result.rows[2].errors == {"address": "Same address as line 2."}


def test_paste_s7_flags_an_existing_tag_at_the_same_address() -> None:
    existing = _saved(S7, "name;address;data_type\nSpeed;DB10.4;real\n")
    text = "name;address;data_type\nRate;%DB10.DBD4;real\nOther;DB10.8;int\n"

    result = _parse(text, S7, existing=[existing])

    assert not result.ok
    assert result.rows[0].tag is None
    assert result.rows[0].errors == {"address": f"Same address as Tag {existing.id}."}
    assert result.rows[1].errors == {}


def test_paste_modbus_flags_an_existing_tag_at_the_same_register() -> None:
    existing = _saved(MODBUS, "name,read_from,address,data_type\nA,hr,40121,int16\n")
    text = "name,read_from,address,data_type\nB,hr,40121,uint16\nC,hr,40122,int16\n"

    result = _parse(text, MODBUS, existing=[existing])

    assert not result.ok
    assert result.rows[0].errors == {"address": f"Same register as Tag {existing.id}."}
    assert result.rows[1].errors == {}


def test_paste_opcua_flags_an_existing_tag_at_the_same_address() -> None:
    existing = _saved(OPCUA, "name,address,value_type\nT,ns=2;s=T,numeric\n")

    result = _parse(
        "name,address,value_type\nU,ns=2;s=T,numeric\n", OPCUA, existing=[existing]
    )

    assert result.rows[0].errors == {"address": f"Same address as Tag {existing.id}."}


def test_example_paste_for_s7_parses() -> None:
    example = example_paste(S7)

    assert "%DB10.DBD4" in example
    assert _parse(example, S7).ok
