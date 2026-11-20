"""Tests for pasting Tags from CSV (parsing only; no store, no routes)."""

from __future__ import annotations

import pytest

from plc_data_collector.domain import ConnectionConfig
from plc_data_collector.panel.tag_adapters import tag_adapter_for
from plc_data_collector.panel.tag_paste import (
    MAX_PASTE_ROWS,
    PasteResult,
    detect_delimiter,
    parse_paste,
)

MODBUS = ConnectionConfig(
    id="press2",
    name="Press 2",
    protocol="modbus",
    endpoint="tcp://press2-plc:502",
    protocol_options={"address_numbering": "modicon"},
)
OPCUA = ConnectionConfig(
    id="line1", name="Line 1", protocol="opcua", endpoint="opc.tcp://plc:4840"
)


def _parse(
    text: str,
    connection: ConnectionConfig = MODBUS,
    taken: frozenset[str] = frozenset(),
) -> PasteResult:
    return parse_paste(
        text,
        adapter=tag_adapter_for(connection.protocol),
        connection=connection,
        default_group_id="fast",
        allowed_group_ids={"fast", "slow"},
        taken_ids=taken,
    )


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
