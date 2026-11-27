"""Paste Tags from CSV: parse a pasted table into Tags, row by row.

The text is what a person copies from a spreadsheet or a device manual: tab,
semicolon or comma separated, with a header row naming the Tag form's columns.
Every row goes through the protocol's Tag form adapter, so a pasted Tag follows
exactly the rules of one entered by hand.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Collection, Hashable, Sequence
from dataclasses import dataclass, field

from plc_data_collector.domain import ConfigurationError, ConnectionConfig, TagConfig
from plc_data_collector.panel.protocol_forms import FormErrors
from plc_data_collector.panel.tag_adapters import TagFormAdapter
from plc_data_collector.panel.tag_forms import generate_tag_id
from plc_data_collector.protocols.modbus_tags import (
    MODBUS_PROTOCOL,
    ModbusConnectionOptions,
)
from plc_data_collector.protocols.s7_tags import S7_PROTOCOL

MAX_PASTE_ROWS = 1000
GROUP_COLUMN = "tag_group"

EMPTY_ERROR = "Paste a header row and at least one tag."
ROW_LIMIT_ERROR = f"Paste at most {MAX_PASTE_ROWS} tags at a time."
MAX_PASTE_CHARS = 1_000_000
TEXT_LIMIT_ERROR = (
    f"The pasted text is too long (over 1 MB). Paste at most {MAX_PASTE_ROWS} rows."
)
UNNAMED_VALUE_ERROR = "This value has no column name."
EXTRA_CELLS_ERROR = "This line has more values than the header has columns."


@dataclass(frozen=True)
class PasteRow:
    """One pasted line: its values and either the Tag it makes or its errors."""

    line: int
    values: dict[str, str]
    tag: TagConfig | None
    errors: dict[str, str]


@dataclass(frozen=True)
class PasteResult:
    """The parsed paste; ``error`` is a problem with the paste as a whole."""

    rows: list[PasteRow]
    error: str | None = None
    # Header names as typed, by normalised column name.
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether every row made a Tag and there is at least one row."""
        return (
            self.error is None
            and bool(self.rows)
            and all(not row.errors and row.tag is not None for row in self.rows)
        )


def detect_delimiter(header: str) -> str:
    """Return the delimiter of a header line: tab, else semicolon, else comma."""
    if "\t" in header:
        return "\t"
    if ";" in header:
        return ";"
    return ","


def _normalise(name: str) -> str:
    key = "_".join(name.strip().lower().split())
    return GROUP_COLUMN if key == "tag_group_id" else key


def parse_paste(
    text: str,
    *,
    adapter: TagFormAdapter,
    connection: ConnectionConfig,
    default_group_id: str,
    allowed_group_ids: Collection[str],
    taken_ids: Collection[str],
    existing_tags: Sequence[TagConfig] = (),
) -> PasteResult:
    """Parse pasted ``text`` into Tags of ``connection``.

    Rows that fail keep their errors, keyed by column; nothing is raised for a
    bad row. A row that reads the same place as an earlier row, or as one of
    ``existing_tags`` (the Connection's Tags), is an error. Problems with the
    paste as a whole come back as ``error``.
    """
    if len(text) > MAX_PASTE_CHARS:
        return PasteResult([], TEXT_LIMIT_ERROR)
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    first = next((line for line in text.split("\n") if line.strip()), None)
    if first is None:
        return PasteResult([], EMPTY_ERROR)
    reader = csv.reader(
        io.StringIO(text, newline=""), delimiter=detect_delimiter(first)
    )

    header: list[str] | None = None
    typed: dict[str, str] = {}
    raw_rows: list[tuple[int, list[str]]] = []
    try:
        while True:
            start = reader.line_num + 1
            cells = next(reader, None)
            if cells is None:
                break
            if not any(cell.strip() for cell in cells):
                continue
            if header is None:
                header = []
                for cell in cells:
                    column = _normalise(cell)
                    header.append(column)
                    if column:
                        typed.setdefault(column, cell.strip())
                continue
            raw_rows.append((start, cells))
    except csv.Error as error:
        return PasteResult([], f"This text can't be read as a table: {error}.")
    if header is None:
        return PasteResult([], EMPTY_ERROR)

    allowed_columns = adapter.columns
    for column in header:
        if column and column not in allowed_columns:
            return PasteResult(
                [],
                f"Unknown column '{column}'. Use: {', '.join(allowed_columns)}.",
            )
    missing = [name for name in adapter.required_columns if name not in header]
    if missing:
        return PasteResult([], f"Missing required column: {', '.join(missing)}.")
    named = [column for column in header if column]
    if len(set(named)) != len(named):
        return PasteResult([], "Each column can appear only once.")
    if not raw_rows:
        return PasteResult([], EMPTY_ERROR)
    if len(raw_rows) > MAX_PASTE_ROWS:
        return PasteResult([], ROW_LIMIT_ERROR)

    taken = set(taken_ids)
    noun = adapter.duplicate_noun
    holders: dict[Hashable, str] = {}
    for existing in existing_tags:
        holders.setdefault(adapter.register_key(existing, connection), existing.id)
    seen: dict[Hashable, int] = {}
    rows: list[PasteRow] = []
    for line, cells in raw_rows:
        values = {column: "" for column in named}
        unnamed_value = False
        for column, cell in zip(header, cells, strict=False):
            if column:
                values[column] = cell.strip()
            elif cell.strip():
                unnamed_value = True
        if not values.get(GROUP_COLUMN):
            values[GROUP_COLUMN] = default_group_id
        errors: dict[str, str] = {}
        tag: TagConfig | None = None
        if unnamed_value:
            errors["form"] = UNNAMED_VALUE_ERROR
        elif any(cell.strip() for cell in cells[len(header) :]):
            errors["form"] = EXTRA_CELLS_ERROR
        else:
            name = values.get("name", "")
            tag_id = generate_tag_id(connection.id, name, taken)
            try:
                tag = adapter.build(
                    values,
                    connection=connection,
                    tag_id=tag_id,
                    allowed_group_ids=allowed_group_ids,
                    existing=None,
                )
            except FormErrors as error:
                errors = dict(error.errors)
            else:
                key = adapter.register_key(tag, connection)
                holder = holders.get(key)
                earlier = seen.get(key)
                if holder is not None:
                    errors["address"] = f"Same {noun} as Tag {holder}."
                    tag = None
                elif earlier is not None:
                    errors["address"] = f"Same {noun} as line {earlier}."
                    tag = None
                else:
                    seen[key] = line
                    taken.add(tag_id)
        rows.append(PasteRow(line, values, tag, errors))
    return PasteResult(rows, headers=typed)


def example_paste(connection: ConnectionConfig) -> str:
    """Return a sample paste for the Connection's protocol, in its numbering."""
    if connection.protocol == S7_PROTOCOL:
        return (
            "name;address;data_type;unit\n"
            "Line speed;%DB10.DBD4;real;m/min\n"
            "Motor running;%DB10.DBX8.0;bool;"
        )
    if connection.protocol != MODBUS_PROTOCOL:
        return "name,address,value_type\nTemperature,ns=2;s=Line1.Temperature,numeric"
    try:
        numbering = ModbusConnectionOptions.from_options(
            connection.protocol_options
        ).address_numbering.value
    except ConfigurationError:
        numbering = "zero"
    address = {"zero": "120", "one": "121", "modicon": "40121"}.get(numbering, "120")
    header = "name;read_from;address;data_type;scale"
    return f"{header}\nOil temperature;hr;{address};float32;0,1"
