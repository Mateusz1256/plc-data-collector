"""TIA Portal DB/UDT source parser: lexing, structure, refusals with file and line."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from plc_data_collector.tia.source import (
    Array,
    DbSource,
    Elementary,
    Named,
    SourceSet,
    Struct,
    TiaSourceError,
    decode_source,
    parse_sources,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tia"


def fixture(name: str) -> tuple[str, str]:
    return name, decode_source((FIXTURES / name).read_bytes(), name)


def only_db(text: str, name: str = "Test.db") -> DbSource:
    sources = parse_sources([(name, text)])
    assert len(sources.blocks) == 1
    return sources.blocks[0]


def db_text(body: str, header: str = "{ S7_Optimized_Access := 'FALSE' }") -> str:
    return (
        f'DATA_BLOCK "D"\n{header}\nVERSION : 0.1\nNON_RETAIN\n   STRUCT\n'
        f"{body}   END_STRUCT;\n\nBEGIN\n\nEND_DATA_BLOCK\n"
    )


def refusal(files: list[tuple[str, str]]) -> TiaSourceError:
    with pytest.raises(TiaSourceError) as caught:
        parse_sources(files)
    return caught.value


# --- decoding ---------------------------------------------------------------


def test_decode_strips_utf8_bom() -> None:
    data = b"\xef\xbb\xbf" + "// Öl\n".encode()
    assert decode_source(data, "a.db") == "// Öl\n"


def test_decode_falls_back_to_cp1252() -> None:
    data = "// Öltemperatur über 60 °C\n".encode("cp1252")
    assert decode_source(data, "a.db") == "// Öltemperatur über 60 °C\n"


def test_decode_refuses_bytes_neither_utf8_nor_cp1252() -> None:
    with pytest.raises(TiaSourceError) as caught:
        decode_source(b"\x81\x8d\x8f", "bin.db")
    assert caught.value.file == "bin.db"
    assert caught.value.line == 1


# --- structure ----------------------------------------------------------------


def test_realistic_db_source() -> None:
    sources = parse_sources([fixture("Recipe.db"), fixture("Types.udt")])
    assert isinstance(sources, SourceSet)
    (db,) = sources.blocks
    assert db.name == "Recipe Data"
    assert db.number is None
    assert db.access == "standard"
    assert db.base_type is None
    assert db.line == 1
    assert db.file == "Recipe.db"
    names = [m.name for m in db.members]
    assert names == ["Set.Point", "Name", "Motor 1", "Steps", "Flags"]
    point, name, motor, steps, flags = db.members
    assert point.type == Elementary("real")
    assert point.comment == "Sollwert in °C"
    assert point.line == 11
    assert name.type == Elementary("string", string_length=8)
    assert name.comment == "Rezeptname; mit Semikolon"
    assert isinstance(motor.type, Struct)
    assert motor.comment == "Motor eins"
    assert motor.line == 15
    speed, times = motor.type.members
    assert (speed.name, speed.type, speed.comment) == (
        "Speed",
        Elementary("int"),
        "Drehzahl",
    )
    assert times.type == Array(((0, 1),), Elementary("time"))
    assert times.comment == ""
    assert steps.type == Array(((1, 2),), Named("typeStep", quoted=True))
    assert flags.type == Array(((0, 1), (0, 1)), Elementary("bool"))
    assert set(sources.types) == {"typeStep", "typeValve"}
    step = sources.types["typeStep"]
    assert step.file == "Types.udt"
    assert step.line == 1
    assert [m.name for m in step.members] == ["Duration", "Valve"]
    assert step.members[1].type == Named("typeValve", quoted=True)
    assert sources.types["typeValve"].line == 10


def test_crlf_bom_umlauts_and_quoted_names_keep_exact_paths() -> None:
    text = (
        "﻿DATA_BLOCK \"Öl Daten\"\r\n{ S7_Optimized_Access := 'FALSE' }\r\n"
        "VERSION : 0.1\r\n   STRUCT \r\n"
        '      "Druck.Ist wert" : Real;   // Öldruck in bar\r\n'
        '      "Pumpe 1" : Struct   // Pumpe für Öl\r\n'
        '         "Läuft" : Bool;   // läuft\r\n'
        "      END_STRUCT;\r\n   END_STRUCT;\r\nBEGIN\r\nEND_DATA_BLOCK\r\n"
    )
    db = only_db(text)
    assert db.name == "Öl Daten"
    first, pump = db.members
    assert (first.name, first.comment, first.line) == (
        "Druck.Ist wert",
        "Öldruck in bar",
        5,
    )
    assert pump.name == "Pumpe 1"
    assert pump.comment == "Pumpe für Öl"
    assert isinstance(pump.type, Struct)
    (runs,) = pump.type.members
    assert (runs.name, runs.comment, runs.line) == ("Läuft", "läuft", 7)


def test_db_number_from_classic_header() -> None:
    assert only_db(db_text("      a : Int;\n").replace('"D"', "DB 12")).number == 12
    assert only_db(db_text("      a : Int;\n").replace('"D"', "DB7")).number == 7


def test_access_attribute() -> None:
    body = "      a : Int;\n"
    assert only_db(db_text(body)).access == "standard"
    optimized = db_text(body, "{ S7_Optimized_Access := 'TRUE' }")
    assert only_db(optimized).access == "optimized"
    assert only_db(db_text(body, "")).access == "unknown"
    lower = db_text(body, "{ s7_optimized_access := 'false' }")
    assert only_db(lower).access == "standard"


def test_types_are_case_insensitive_and_aliases_resolve() -> None:
    db = only_db(
        db_text(
            "      a : BOOL;\n      b : dint;\n      c : TOD;\n      d : DT;\n"
            "      e : string;\n      f : WString[10];\n      g : LWord;\n"
            "      h : ARRAY [0 .. 3] OF BYTE;\n      i : Array[-2..2] of Int;\n"
        )
    )
    types = [m.type for m in db.members]
    assert types == [
        Elementary("bool"),
        Elementary("dint"),
        Elementary("time_of_day"),
        Elementary("date_and_time"),
        Elementary("string", string_length=254),
        Elementary("wstring", string_length=10),
        Elementary("lword"),
        Array(((0, 3),), Elementary("byte")),
        Array(((-2, 2),), Elementary("int")),
    ]


def test_unknown_and_special_type_names_parse_as_named() -> None:
    db = only_db(
        db_text(
            '      a : Variant;\n      b : IEC_TIMER;\n      c : "typeX";\n'
            "      d : DB_ANY;\n"
        )
    )
    assert [m.type for m in db.members] == [
        Named("Variant", quoted=False),
        Named("IEC_TIMER", quoted=False),
        Named("typeX", quoted=True),
        Named("DB_ANY", quoted=False),
    ]


def test_stray_semicolon_after_struct_and_array_of_struct() -> None:
    db = only_db(
        db_text(
            "      s : Struct;\n         x : Byte;\n      END_STRUCT;\n"
            "      l : Array[0..1] of Struct   // Linien\n"
            "         t : Int;\n      END_STRUCT;\n"
        )
    )
    s, line = db.members
    assert isinstance(s.type, Struct)
    assert [(m.name, m.type) for m in s.type.members] == [("x", Elementary("byte"))]
    assert isinstance(line.type, Array)
    assert isinstance(line.type.element, Struct)
    assert line.comment == "Linien"
    assert [m.name for m in line.type.element.members] == ["t"]


def test_udt_based_and_instance_db_keep_base_type() -> None:
    sources = parse_sources([fixture("Instance.db")])
    (db,) = sources.blocks
    assert db.base_type == "PressControl"
    assert db.members == ()
    classic = (
        "DATA_BLOCK DB 5\nVERSION : 0.1\nFB 10\nBEGIN\nEND_DATA_BLOCK\n"
        'DATA_BLOCK "T"\nIEC_TIMER\nBEGIN\nEND_DATA_BLOCK\n'
    )
    first, second = parse_sources([("c.db", classic)]).blocks
    assert (first.number, first.base_type) == (5, "FB10")
    assert second.base_type == "IEC_TIMER"


def test_db_without_begin_and_several_blocks_in_one_file() -> None:
    text = (
        db_text("      a : Int;\n").replace("BEGIN\n\n", "")
        + 'TYPE "u"\n   STRUCT\n      x : Int;\n   END_STRUCT;\nEND_TYPE\n'
        + db_text("      b : Int;\n").replace('"D"', '"E"')
    )
    sources = parse_sources([("m.db", text)])
    assert [b.name for b in sources.blocks] == ["D", "E"]
    assert list(sources.types) == ["u"]


# --- refusals -------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "line", "fragment"),
    [
        ("      a : Int\n      b : Int;\n", 7, '";"'),
        ("      a : Array[*] of Int;\n", 6, "*"),
        ('      a : Array[0.."MAX"] of Int;\n', 6, "MAX"),
        ("      a : Array[3..1] of Int;\n", 6, "3..1"),
        ("      a : String[0];\n", 6, "1-254"),
        ("      a : String[255];\n", 6, "1-254"),
        ("      a : Array[0..1] of Array[0..1] of Int;\n", 6, "Array"),
        ("      a Int;\n", 6, "Int"),
        ("      a : Int;\n      : Int;\n", 7, ":"),
        ("      a : Int;\n      (* never closed\n", 7, "comment"),
    ],
)
def test_syntax_refusals_name_file_and_line(
    body: str, line: int, fragment: str
) -> None:
    error = refusal([("Bad.db", db_text(body))])
    assert error.file == "Bad.db"
    assert error.line == line
    assert fragment in error.message
    assert str(error) == f"Bad.db, line {line}: {error.message}"


def test_unknown_top_level_construct() -> None:
    error = refusal([("f.db", '\nFUNCTION_BLOCK "FB"\nEND_FUNCTION_BLOCK\n')])
    assert (error.file, error.line) == ("f.db", 2)
    assert "FUNCTION_BLOCK" in error.message


def test_unknown_header_construct() -> None:
    text = db_text("      a : Int;\n").replace("NON_RETAIN", "STRANGE_THING 3")
    error = refusal([("h.db", text)])
    assert (error.line, "STRANGE_THING" in error.message) == (4, True)


def test_bad_access_value() -> None:
    text = db_text("      a : Int;\n", "{ S7_Optimized_Access := 'MAYBE' }")
    error = refusal([("h.db", text)])
    assert error.line == 2
    assert "MAYBE" in error.message


def test_struct_and_base_type_together_refused() -> None:
    text = db_text("      a : Int;\n").replace("NON_RETAIN", '"typeX"')
    assert refusal([("h.db", text)]).line == 5


def test_missing_end_refused_at_end_of_file() -> None:
    text = 'DATA_BLOCK "D"\n   STRUCT\n      a : Int;\n'
    error = refusal([("e.db", text)])
    assert error.line == 3
    assert "end of file" in error.message


def test_duplicate_type_refused() -> None:
    udt = 'TYPE "u"\n   STRUCT\n      x : Int;\n   END_STRUCT;\nEND_TYPE\n'
    error = refusal([("a.udt", udt), ("b.udt", udt)])
    assert (error.file, error.line) == ("b.udt", 1)
    assert '"u"' in error.message


def test_unterminated_string_refused() -> None:
    error = refusal([("s.db", db_text("      a : String[4] := 'abc;\n"))])
    assert (error.file, error.line) == ("s.db", 6)


# --- hostile input ------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "      a : Array[0..2147483648] of Bool;\n",
        "      a : Array[-2147483649..0] of Bool;\n",
        "      a : Array[0..99999999999999999999999] of Bool;\n",
        f"      a : Array[0..{'9' * 5000}] of Bool;\n",
        f"      a : String[{'9' * 5000}];\n",
    ],
    ids=["above-dint", "below-dint", "23-digits", "5000-digits", "string-5000-digits"],
)
def test_bounds_beyond_dint_or_huge_literals_refused(body: str) -> None:
    error = refusal([("H.db", db_text(body))])
    assert (error.file, error.line) == ("H.db", 6)


def test_deep_struct_nesting_refused_quickly() -> None:
    depth = 400
    body = "".join(f"      s{i} : Struct\n" for i in range(depth))
    body += "      x : Int;\n" + "      END_STRUCT;\n" * depth
    start = time.monotonic()
    error = refusal([("N.db", db_text(body))])
    assert time.monotonic() - start < 1
    assert error.file == "N.db"
    assert 6 <= error.line < 6 + depth
    assert "nest" in error.message
