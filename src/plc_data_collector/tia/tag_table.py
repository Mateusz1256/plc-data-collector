"""Candidates from a TIA Portal PLC tag table exported as ``.xlsx``.

Standard library only (``zipfile`` + ``xml.etree``). The file is untrusted, so:

* the compressed size, the bytes really inflated (counted while streaming; the
  zip header's declared sizes are not trusted) and the row count are bounded;
* an XML part with a DOCTYPE or ENTITY declaration, or in an encoding other
  than UTF-8/ASCII/Latin-1, is refused before it reaches the parser, so no
  entity is ever defined or resolved;
* every failure is a ``TiaSourceError``.

A row that cannot become a Tag still yields a ``Candidate``, with ``problem``
set. When its address is unusable, ``address`` is None; the Logical Address
text as TIA wrote it is always in ``source_address``.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
import zlib
from collections.abc import Iterator
from xml.etree import ElementTree

from plc_data_collector.domain import ConfigurationError
from plc_data_collector.protocols.s7_tags import (
    DataType,
    parse_step7,
    size_letter_error,
)
from plc_data_collector.tia.layout import UNSUPPORTED_PROBLEM, Candidate
from plc_data_collector.tia.source import TiaSourceError

MAX_COMPRESSED_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_ROWS = 100_000
# Elements in one XML part; bounds the parse time of a flood of empty elements.
MAX_ELEMENTS = 2_000_000
SHEET_NAME = "PLC Tags"

_MAX_COLUMN_LETTERS = 3
_CHUNK = 64 * 1024
_NOT_XLSX = "This is not an .xlsx file."
_NAME, _TYPE, _ADDRESS, _COMMENT = "name", "data type", "logical address", "comment"
_ALLOWED_ENCODINGS = {"utf-8", "us-ascii", "ascii", "iso-8859-1", "latin-1"}
_ENCODING = re.compile(rb"""^\s*<\?xml[^>]*?encoding\s*=\s*["']([^"']+)["']""")
_STRING_LENGTH = re.compile(r"string\[([0-9]{1,3})\]")
_MAX_DEPTH = 16
_MAX_LIVE = 100_000
_ADDRESS_SPACE = 65536
_STORED_OR_DEFLATED = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_TYPES = {data_type.value: data_type for data_type in DataType}
_MAX_STRING = 254


class _Reader:
    """The open workbook and what is left of the inflate budget."""

    def __init__(self, data: bytes, file: str) -> None:
        self.file = file
        self.left = MAX_UNCOMPRESSED_BYTES
        try:
            self.archive = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, EOFError, OSError, ValueError) as error:
            raise TiaSourceError(file, 0, _NOT_XLSX) from error

    def fail(self, message: str, line: int = 0) -> TiaSourceError:
        return TiaSourceError(self.file, line, message)

    def read(self, name: str, *, required: bool = True) -> bytes | None:
        """Inflate one member, counting the bytes as they come out."""
        try:
            info = self.archive.getinfo(name)
        except KeyError:
            if required:
                raise self.fail(f"The workbook has no {name}.") from None
            return None
        if info.compress_type not in _STORED_OR_DEFLATED:
            # Other methods inflate without a per-read bound; Excel/TIA only
            # write deflate.
            raise self.fail(f"{name} uses an unsupported compression method.")
        chunks: list[bytes] = []
        try:
            with self.archive.open(info) as stream:
                while chunk := stream.read(_CHUNK):
                    self.left -= len(chunk)
                    if self.left < 0:
                        raise self.fail(
                            "The file is too large when unpacked "
                            f"(limit {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB)."
                        )
                    chunks.append(chunk)
        except TiaSourceError:
            raise
        except (
            zipfile.BadZipFile,
            zlib.error,
            EOFError,
            OSError,
            NotImplementedError,
            RuntimeError,
            ValueError,
        ) as error:
            raise self.fail(f"{name} cannot be read: the file is damaged.") from error
        return b"".join(chunks)

    def xml(self, name: str, *, required: bool = True) -> bytes | None:
        """Inflate an XML member and refuse DOCTYPE/ENTITY and odd encodings."""
        data = self.read(name, required=required)
        if data is None:
            return None
        if b"\x00" in data or data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            raise self.fail(f"{name} is not UTF-8 XML.")
        match = _ENCODING.match(data[:512])
        if match and match.group(1).decode("ascii", "replace").lower() not in (
            _ALLOWED_ENCODINGS
        ):
            raise self.fail(f"{name} uses an unsupported encoding.")
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            raise self.fail(f"{name} has a DOCTYPE or ENTITY declaration; refused.")
        return data

    def events(
        self, name: str, data: bytes, drop_depth: int = 2
    ) -> Iterator[tuple[str, ElementTree.Element]]:
        """End events of the XML in ``data``, with bounded memory.

        Nesting deeper than ``_MAX_DEPTH``, too many elements held at once and
        more than ``MAX_ELEMENTS`` elements in the part are refused. Elements
        down to ``drop_depth`` (root = 1) are dropped once the caller has seen
        them, so a huge document never builds a full tree.
        """
        stack: list[ElementTree.Element] = []
        live = 0
        total = 0
        try:
            for event, element in ElementTree.iterparse(
                io.BytesIO(data), events=("start", "end")
            ):
                if event == "start":
                    stack.append(element)
                    live += 1
                    total += 1
                    if total > MAX_ELEMENTS:
                        raise self.fail(f"{name} has too many elements.")
                    if len(stack) > _MAX_DEPTH or live > _MAX_LIVE:
                        raise self.fail(f"{name} is nested or large beyond limits.")
                    continue
                depth = len(stack)
                stack.pop()
                yield event, element
                if depth <= drop_depth:
                    element.clear()
                    live = 0 if depth == drop_depth else live
                    if stack:
                        stack[-1].remove(element)
        except ElementTree.ParseError as error:
            raise self.fail(f"{name} is not valid XML ({error}).") from error


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ElementTree.Element) -> str:
    """Text of a shared/inline string: every ``t`` except phonetic hints."""
    parts: list[str] = []
    for child in element:
        name = _local(child.tag)
        if name == "t":
            parts.append(child.text or "")
        elif name == "r":
            parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
    return "".join(parts)


def _sheet_path(reader: _Reader) -> str:
    book = reader.xml("xl/workbook.xml")
    rels = reader.xml("xl/_rels/workbook.xml.rels")
    assert book is not None and rels is not None
    sheets = [
        (element.get("name", ""), element.get(_ID, ""))
        for _, element in reader.events("xl/workbook.xml", book, 3)
        if _local(element.tag) == "sheet"
    ]
    if not sheets:
        raise reader.fail("The workbook has no sheets.")
    targets = {
        element.get("Id", ""): element.get("Target", "")
        for _, element in reader.events("xl/_rels/workbook.xml.rels", rels)
        if _local(element.tag) == "Relationship"
    }
    chosen = next(
        (s for s in sheets if s[0].strip().lower() == SHEET_NAME.lower()), sheets[0]
    )
    target = targets.get(chosen[1])
    if not target:
        raise reader.fail(f'The sheet "{chosen[0]}" has no data.')
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("xl", target))


def _shared_strings(reader: _Reader) -> list[str]:
    data = reader.xml("xl/sharedStrings.xml", required=False)
    if data is None:
        return []
    strings: list[str] = []
    for _, element in reader.events("xl/sharedStrings.xml", data):
        if _local(element.tag) == "si":
            strings.append(_text(element))
            element.clear()
    return strings


def _column(reference: str) -> int | None:
    """Zero-based column of a cell reference like ``AB12``; None if unusable."""
    letters = re.match(r"[A-Za-z]+", reference)
    if letters is None or len(letters.group()) > _MAX_COLUMN_LETTERS:
        return None
    index = 0
    for letter in letters.group().upper():
        index = index * 26 + ord(letter) - 64
    return index - 1


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = next((c for c in cell if _local(c.tag) == "is"), None)
        return "" if inline is None else _text(inline)
    value = next((c.text or "" for c in cell if _local(c.tag) == "v"), "")
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    return value


def _rows(
    reader: _Reader, name: str, data: bytes, shared: list[str]
) -> Iterator[tuple[int, dict[int, str]]]:
    count = 0
    for _, element in reader.events(name, data, 3):
        if _local(element.tag) != "row":
            continue
        count += 1
        if count > MAX_ROWS:
            raise reader.fail(f"The sheet has more than {MAX_ROWS} rows.", count)
        cells: dict[int, str] = {}
        for position, cell in enumerate(element):
            if _local(cell.tag) != "c":
                continue
            index = _column(cell.get("r", ""))
            if index is None:
                index = position
            text = _cell_text(cell, shared).strip()
            if text:
                cells[index] = text
        element.clear()
        yield count, cells


def _candidate(name: str, kind: str, address: str, comment: str) -> Candidate:
    def bad(problem: str, data_type: DataType | None = None) -> Candidate:
        return Candidate(name, None, data_type, None, comment, problem, address)

    lowered = kind.lower()
    if not address:
        return bad("The row has no Logical Address.")
    if not kind:
        return bad("The row has no Data Type.")
    upper = address.upper()
    if upper.startswith("%T"):
        return bad("Timers are not supported.")
    if upper.startswith("%C"):
        return bad("Counters are not supported.")
    if upper.endswith(":P"):
        return bad("Peripheral I/O is not supported.")
    try:
        parsed, size = parse_step7(address)
    except ConfigurationError as error:
        return bad(error.message)
    length: int | None = None
    string = _STRING_LENGTH.fullmatch(lowered)
    data_type = DataType.STRING if string else _TYPES.get(lowered)
    problem: str | None = None
    if data_type is None:
        problem = f"{UNSUPPORTED_PROBLEM}: data type {kind}."
    elif string:
        length = int(string.group(1))
        if not 1 <= length <= _MAX_STRING:
            problem = f"String length {length} is outside 1-{_MAX_STRING}."
    elif data_type is DataType.STRING:
        length = _MAX_STRING  # TIA's plain String is String[254]
    if problem is None and data_type is not None:
        problem = size_letter_error(address, size, data_type)
    if (
        problem is None
        and length is not None
        and parsed.byte + length + 2 > _ADDRESS_SPACE
    ):
        problem = "The String does not fit in the address space."
    return Candidate(name, parsed, data_type, length, comment, problem, address)


def parse_tag_table(data: bytes, file_name: str) -> tuple[Candidate, ...]:
    """Read the tag table in the ``.xlsx`` ``data``; refuse anything hostile."""
    if len(data) > MAX_COMPRESSED_BYTES:
        raise TiaSourceError(
            file_name,
            0,
            "The file is too large "
            f"(limit {MAX_COMPRESSED_BYTES // (1024 * 1024)} MB).",
        )
    reader = _Reader(data, file_name)
    try:
        return _parse(reader)
    finally:
        reader.archive.close()


def _parse(reader: _Reader) -> tuple[Candidate, ...]:
    path = _sheet_path(reader)
    shared = _shared_strings(reader)
    sheet = reader.xml(path)
    assert sheet is not None
    columns: dict[str, int] | None = None
    candidates: list[Candidate] = []
    for number, cells in _rows(reader, path, sheet, shared):
        if columns is None:
            found = {text.lower(): index for index, text in cells.items()}
            if _NAME in found:
                missing = [
                    label
                    for label, key in (
                        ("Data Type", _TYPE),
                        ("Logical Address", _ADDRESS),
                    )
                    if key not in found
                ]
                if missing:
                    raise reader.fail(
                        f"The sheet has no {' or '.join(missing)} column.", number
                    )
                columns = found
            continue
        name = cells.get(columns[_NAME], "")
        kind = cells.get(columns[_TYPE], "")
        address = cells.get(columns[_ADDRESS], "")
        if not name and not address:
            continue
        comment = cells.get(columns[_COMMENT], "") if _COMMENT in columns else ""
        candidates.append(_candidate(name or address, kind, address, comment))
    if columns is None:
        raise reader.fail(
            "The sheet has no Name, Data Type and Logical Address header."
        )
    return tuple(candidates)
