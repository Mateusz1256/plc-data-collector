"""TIA Portal tag table (.xlsx) reader: candidates, problems, hostile files."""

from __future__ import annotations

import io
import time
import zipfile
from xml.sax.saxutils import escape

import pytest

from plc_data_collector.protocols.s7_tags import Area, DataType, S7Address
from plc_data_collector.tia import tag_table
from plc_data_collector.tia.layout import Candidate
from plc_data_collector.tia.source import TiaSourceError
from plc_data_collector.tia.tag_table import parse_tag_table

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HEADER = ["Name", "Path", "Data Type", "Logical Address", "Comment"]


def column(index: int) -> str:
    return "ABCDEFGHIJ"[index]


def sheet_xml(rows: list[list[str]], shared: list[str] | None = None) -> str:
    """Rows of text cells; with ``shared`` the cells point into shared strings."""
    out = []
    for number, row in enumerate(rows, start=1):
        cells = []
        for index, text in enumerate(row):
            ref = f"{column(index)}{number}"
            if shared is None:
                cells.append(
                    f'<c r="{ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'
                )
            else:
                shared.append(text)
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
        out.append(f'<row r="{number}">{"".join(cells)}</row>')
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{MAIN}">'
        f"<sheetData>{''.join(out)}</sheetData></worksheet>"
    )


def workbook(
    sheets: dict[str, str],
    shared: list[str] | None = None,
    *,
    members: dict[str, bytes | str] | None = None,
) -> bytes:
    """A minimal valid workbook: sheet name -> sheet XML."""
    names = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(sheets, start=1)
    )
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="x" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{MAIN}" xmlns:r="{REL}">'
            f"<sheets>{names}</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            f'relationships">{rels}</Relationships>',
        )
        for i, xml in enumerate(sheets.values(), start=1):
            archive.writestr(f"xl/worksheets/sheet{i}.xml", xml)
        if shared is not None:
            items = "".join(f"<si><t>{escape(text)}</t></si>" for text in shared)
            archive.writestr(
                "xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">{items}</sst>'
            )
        for name, content in (members or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def table(rows: list[list[str]], header: list[str] = HEADER) -> bytes:
    return workbook({"PLC Tags": sheet_xml([header, *rows])})


def refusal(data: bytes) -> TiaSourceError:
    with pytest.raises(TiaSourceError) as caught:
        parse_tag_table(data, "tags.xlsx")
    return caught.value


def refusal_or_none(data: bytes) -> TiaSourceError | None:
    try:
        parse_tag_table(data, "tags.xlsx")
    except TiaSourceError as error:
        return error
    return None


def test_three_rows_become_candidates() -> None:
    data = table(
        [
            ["Level", "Default tag table", "Int", "%MW20", "Tank level"],
            ["Start", "Default tag table", "Bool", "%I0.3", ""],
            ["Setpoint", "Default tag table", "Real", "%QD4", "Speed"],
        ]
    )
    assert parse_tag_table(data, "tags.xlsx") == (
        _candidate("Level", Area.M, 20, None, DataType.INT, "Tank level", "%MW20"),
        _candidate("Start", Area.I, 0, 3, DataType.BOOL, "", "%I0.3"),
        _candidate("Setpoint", Area.Q, 4, None, DataType.REAL, "Speed", "%QD4"),
    )


def _candidate(
    name: str,
    area: Area,
    byte: int,
    bit: int | None,
    data_type: DataType,
    comment: str,
    source: str,
) -> Candidate:
    return Candidate(
        name, S7Address(area, None, byte, bit), data_type, None, comment, None, source
    )


def test_shuffled_header_with_extra_columns_and_shared_strings() -> None:
    shared: list[str] = []
    rows = [
        ["Comment", "Hmi Visible", "Logical Address", "Data Type", "Name"],
        ["c1", "True", "%MD8", "dint", "Counter"],
    ]
    data = workbook({"PLC Tags": sheet_xml(rows, shared)}, shared)
    (candidate,) = parse_tag_table(data, "tags.xlsx")
    assert candidate == _candidate(
        "Counter", Area.M, 8, None, DataType.DINT, "c1", "%MD8"
    )


def test_plc_tags_sheet_is_preferred_to_the_first() -> None:
    other = sheet_xml([HEADER, ["Wrong", "", "Int", "%MW0", ""]])
    wanted = sheet_xml([HEADER, ["Right", "", "Int", "%MW2", ""]])
    data = workbook({"Other": other, "PLC Tags": wanted})
    assert [c.path for c in parse_tag_table(data, "t.xlsx")] == ["Right"]


def test_first_sheet_when_there_is_no_plc_tags_sheet() -> None:
    sheet = sheet_xml([HEADER, ["Only", "", "Int", "%MW2", ""]])
    data = workbook({"Sheet1": sheet})
    assert [c.path for c in parse_tag_table(data, "t.xlsx")] == ["Only"]


@pytest.mark.parametrize(
    ("data_type", "address", "problem"),
    [
        ("Timer", "%T5", "Timers are not supported"),
        ("Counter", "%C3", "Counters are not supported"),
        ("Int", "%IW0:P", "Peripheral I/O is not supported"),
        ("Date", "%MW4", "Not supported in 0.5.0"),
        ("Foo", "%MW4", "Not supported in 0.5.0"),
        ("Real", "%MW20", "2-byte address"),
        ("Int", "%X", "Invalid S7 address"),
        ("Int", "", "no Logical Address"),
        ("", "%MW4", "no Data Type"),
        ("String", "%MB65400", "address space"),
    ],
)
def test_unusable_rows_become_candidates_with_a_problem(
    data_type: str, address: str, problem: str
) -> None:
    (candidate,) = parse_tag_table(table([["Tag", "", data_type, address, ""]]), "t")
    assert candidate.path == "Tag"
    assert candidate.problem is not None
    assert problem in candidate.problem


def test_plain_string_is_string_254() -> None:
    (candidate,) = parse_tag_table(table([["S", "", "String", "%MB10", ""]]), "t")
    assert (candidate.data_type, candidate.length, candidate.problem) == (
        DataType.STRING,
        254,
        None,
    )


def test_unusable_address_is_none_and_the_text_is_kept() -> None:
    rows = [["T", "", "Timer", "%T5", ""], ["B", "", "Int", "%X", ""]]
    timer, bad = parse_tag_table(table(rows), "t")
    assert (timer.address, timer.source_address) == (None, "%T5")
    assert (bad.address, bad.source_address) == (None, "%X")
    (good,) = parse_tag_table(table([["G", "", "Int", "%mw2", ""]]), "t")
    assert good.source_address == "%mw2"


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_non_deflate_compression_refused_without_inflating(method: int) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as archive:
        archive.writestr("xl/workbook.xml", " " * 1_000_000)
    assert "compression" in refusal(buffer.getvalue()).message


def test_flat_element_flood_refused_or_harmless() -> None:
    flood = f'<worksheet xmlns="{MAIN}"><sheetData>' + "<a/>" * 3_000_000
    flood += "</sheetData></worksheet>"
    start = time.monotonic()
    result = refusal_or_none(workbook({"PLC Tags": flood}))
    assert result is None or result.file == "tags.xlsx"
    assert time.monotonic() - start < 20


def test_deep_nesting_refused() -> None:
    deep = f'<worksheet xmlns="{MAIN}">' + "<a>" * 5000 + "</a>" * 5000 + "</worksheet>"
    start = time.monotonic()
    error = refusal(workbook({"PLC Tags": deep}))
    assert "nested" in error.message
    assert time.monotonic() - start < 5


def test_many_children_in_one_row_refused() -> None:
    row = "<row>" + "<c/>" * 200_000 + "</row>"
    sheet = f'<worksheet xmlns="{MAIN}"><sheetData>{row}</sheetData></worksheet>'
    assert "large" in refusal(workbook({"PLC Tags": sheet})).message


def test_many_flat_siblings_in_workbook_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "<w>" + "<a/>" * 300_000 + "</w>")
        archive.writestr("xl/_rels/workbook.xml.rels", "<r/>")
    refusal(buffer.getvalue())


def test_string_with_length_and_type_names_ignore_case() -> None:
    rows = [["S", "", "String[12]", "%MB10", ""], ["B", "", "BOOL", "%m1.0", ""]]
    text, flag = parse_tag_table(table(rows), "t")
    assert (text.data_type, text.length, text.problem) == (DataType.STRING, 12, None)
    assert (flag.data_type, flag.problem) == (DataType.BOOL, None)


def test_blank_rows_are_skipped() -> None:
    rows = [["", "", "", "", ""], ["A", "", "Int", "%MW0", ""]]
    assert [c.path for c in parse_tag_table(table(rows), "t")] == ["A"]


def test_missing_required_column_refused() -> None:
    error = refusal(table([["A", "Int"]], header=["Name", "Data Type"]))
    assert "Logical Address" in error.message


def test_not_a_zip() -> None:
    error = refusal(b"hello, this is text")
    assert error.message == "This is not an .xlsx file."


def test_truncated_zip_refused() -> None:
    data = table([["A", "", "Int", "%MW0", ""]])
    assert refusal(data[: len(data) // 2]).message
    assert refusal(data[:-10]).message


def test_zip_without_workbook_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "x")
    assert "workbook" in refusal(buffer.getvalue()).message.lower()


@pytest.mark.parametrize(
    "xml",
    [
        f'<worksheet xmlns="{MAIN}"><sheetData><row><c></row></sheetData></worksheet>',
        "",
        "\x00\x01\x02",
    ],
    ids=["malformed", "empty", "binary"],
)
def test_malformed_xml_refused(xml: str) -> None:
    error = refusal(workbook({"PLC Tags": xml}))
    assert error.file == "tags.xlsx"


def test_malformed_workbook_xml_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook><sheets>")
    refusal(buffer.getvalue())


@pytest.mark.parametrize(
    "prefix",
    [
        '<!DOCTYPE foo [<!ENTITY x "y">]>',
        '<!DOCTYPE foo SYSTEM "file:///etc/passwd">',
        "<!-- " + "a" * 5000 + ' --><!DOCTYPE foo [<!ENTITY x "y">]>',
    ],
    ids=["internal", "external", "after-long-comment"],
)
def test_doctype_refused(prefix: str) -> None:
    sheet = sheet_xml([HEADER]).replace("<worksheet", prefix + "<worksheet", 1)
    error = refusal(workbook({"PLC Tags": sheet}))
    assert "DOCTYPE" in error.message or "entit" in error.message.lower()


def test_doctype_in_workbook_xml_refused() -> None:
    sheet = sheet_xml([HEADER])
    data = workbook({"PLC Tags": sheet})
    source = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for info in source.infolist():
            content = source.read(info)
            if info.filename == "xl/workbook.xml":
                content = b'<!DOCTYPE a [<!ENTITY b "c">]>' + content
            archive.writestr(info.filename, content)
    assert "DOCTYPE" in refusal(buffer.getvalue()).message


def test_utf16_xml_refused() -> None:
    sheet = sheet_xml([HEADER]).replace('encoding="UTF-8"', 'encoding="UTF-16"')
    refusal(workbook({"PLC Tags": sheet}))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "<a/>".encode("utf-16"))
    refusal(buffer.getvalue())


def test_sheet_inflating_past_the_bound_refused() -> None:
    bomb = (
        f'<worksheet xmlns="{MAIN}"><sheetData>'
        + "<!--"
        + "a" * (60 * 1024 * 1024)
        + "-->"
        + "</sheetData></worksheet>"
    )
    data = workbook({"PLC Tags": bomb})
    assert len(data) < tag_table.MAX_COMPRESSED_BYTES
    assert "too large" in refusal(data).message.lower()


def test_bound_counts_what_is_really_inflated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tag_table, "MAX_UNCOMPRESSED_BYTES", 2000)
    rows = [[f"T{i}", "", "Int", "%MW0", ""] for i in range(200)]
    assert "too large" in refusal(table(rows)).message.lower()


def test_compressed_input_too_large_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tag_table, "MAX_COMPRESSED_BYTES", 100)
    assert "too large" in refusal(table([])).message.lower()


def test_too_many_rows_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tag_table, "MAX_ROWS", 5)
    rows = [[f"T{i}", "", "Int", "%MW0", ""] for i in range(10)]
    assert "rows" in refusal(table(rows)).message.lower()


def test_row_count_bound_is_100000() -> None:
    assert tag_table.MAX_ROWS == 100_000
    assert tag_table.MAX_COMPRESSED_BYTES == 5 * 1024 * 1024
    assert tag_table.MAX_UNCOMPRESSED_BYTES == 50 * 1024 * 1024
