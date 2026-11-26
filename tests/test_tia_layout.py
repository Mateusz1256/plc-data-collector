"""Standard-access offsets of TIA DB sources, and the refusals.

Every expected offset below is worked out by hand from the offset rules of the
spec (docs/superpowers/specs/2026-11-25-v0.5.0-s7-design.md, "TIA Portal
import", "Offset rules"), which follow Siemens' documentation of the memory
layout of blocks with standard (non-optimized) access: STEP 7 V5 "Programming
with STEP 7" / STEP 7 online help, "Data types and parameter types" (BOOL one
bit; BYTE and CHAR one byte; WORD-sized and larger types, STRING, ARRAY and
STRUCT start at a word, i.e. even byte, boundary; STRUCT and ARRAY end on a word
boundary; STRING[n] takes n + 2 bytes; multi-dimensional arrays are stored
with the last index varying fastest), and the TIA Portal information system,
"Basics of block access", which describes standard access as the S7-300/400
layout. None of them is the output of the layout code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from s7_layout_support import documented_rows

from plc_data_collector.protocols.s7_tags import Area, DataType, S7Address
from plc_data_collector.simulators import s7 as simulator
from plc_data_collector.tia.layout import Candidate, DbLayout, layout_db
from plc_data_collector.tia.source import (
    ELEMENTARY_TYPES,
    TiaSourceError,
    decode_source,
    parse_sources,
)

ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "tia"


def udt(name: str, body: str) -> str:
    return f'TYPE "{name}"\n   STRUCT\n{body}   END_STRUCT;\nEND_TYPE\n'


UDT_A = udt("udtA", "      a : Int;\n      b : Bool;\n")


def db_text(body: str, header: str = "{ S7_Optimized_Access := 'FALSE' }") -> str:
    return (
        f"DATA_BLOCK DB 3\n{header}\nVERSION : 0.1\n   STRUCT\n"
        f"{body}   END_STRUCT;\nBEGIN\nEND_DATA_BLOCK\n"
    )


def lay_out(body: str, *extra: str, header: str | None = None) -> DbLayout:
    text = db_text(body) if header is None else db_text(body, header)
    files = [("T.db", text)] + [(f"u{i}.udt", t) for i, t in enumerate(extra)]
    sources = parse_sources(files)
    return layout_db(sources.blocks[0], sources.types, classic_confirmed=False)


def offsets(layout: DbLayout) -> dict[str, str]:
    return {c.path: offset(c.address) for c in layout.candidates}


def offset(address: S7Address) -> str:
    return f"{address.byte}" if address.bit is None else f"{address.byte}.{address.bit}"


def refused(
    text: str, *extra: tuple[str, str], classic: bool = False
) -> TiaSourceError:
    sources = parse_sources([("R.db", text), *extra])
    with pytest.raises(TiaSourceError) as caught:
        layout_db(sources.blocks[0], sources.types, classic_confirmed=classic)
    return caught.value


# --- worked examples ----------------------------------------------------------


def test_three_bools_then_byte() -> None:
    # Bools fill bits 0-2 of byte 0; a Byte takes the next free byte.
    layout = lay_out(
        "      a : Bool;\n      b : Bool;\n      c : Bool;\n      d : Byte;\n"
    )
    assert offsets(layout) == {"a": "0.0", "b": "0.1", "c": "0.2", "d": "1"}
    assert layout.size == 2


def test_nine_bools_roll_into_next_byte_then_byte() -> None:
    body = "".join(f"      b{i} : Bool;\n" for i in range(9)) + "      x : Byte;\n"
    layout = offsets(lay_out(body))
    assert layout["b7"] == "0.7"
    assert layout["b8"] == "1.0"
    assert layout["x"] == "2"


def test_bool_then_int() -> None:
    # Int is word-sized: next even byte.
    assert offsets(lay_out("      a : Bool;\n      b : Int;\n")) == {
        "a": "0.0",
        "b": "2",
    }


def test_byte_byte_int() -> None:
    layout = lay_out("      a : Byte;\n      b : Byte;\n      c : Int;\n")
    assert offsets(layout) == {"a": "0", "b": "1", "c": "2"}
    assert layout.size == 4


def test_byte_like_types_and_bool_after_byte() -> None:
    # Char, SInt, USInt behave like Byte; a Bool after a byte starts a new byte.
    layout = lay_out(
        "      a : Char;\n      b : SInt;\n      c : USInt;\n      d : Bool;\n"
        "      e : Byte;\n      f : Bool;\n"
    )
    assert offsets(layout) == {
        "a": "0",
        "b": "1",
        "c": "2",
        "d": "3.0",
        "e": "4",
        "f": "5.0",
    }
    assert layout.size == 6


def test_bool_then_struct() -> None:
    # A Struct starts on an even byte.
    layout = lay_out(
        "      a : Bool;\n      s : Struct\n         x : Byte;\n      END_STRUCT;\n"
    )
    assert offsets(layout) == {"a": "0.0", "s.x": "2"}


def test_struct_of_one_byte_then_byte() -> None:
    # Struct size 1 rounds up to 2, so the next member is at 2; DB size 3 -> 4.
    layout = lay_out(
        "      s : Struct\n         x : Byte;\n      END_STRUCT;\n      y : Byte;\n"
    )
    assert offsets(layout) == {"s.x": "0", "y": "2"}
    assert layout.size == 4


def test_single_byte_db_size_rounds_to_even() -> None:
    assert lay_out("      a : Byte;\n").size == 2


def test_array_of_bool_then_int() -> None:
    # Array of Bool packs bits: [0..7] in byte 0, [8], [9] in byte 1; 2 bytes.
    layout = offsets(lay_out("      f : Array[0..9] of Bool;\n      i : Int;\n"))
    expected = {f"f[{i}]": f"{i // 8}.{i % 8}" for i in range(10)}
    expected["i"] = "2"
    assert layout == expected


def test_array_of_byte_then_int() -> None:
    # Three bytes 0, 1, 2; array size 3 rounds to 4, so Int at 4.
    layout = offsets(lay_out("      a : Array[1..3] of Byte;\n      i : Int;\n"))
    assert layout == {"a[1]": "0", "a[2]": "1", "a[3]": "2", "i": "4"}


def test_array_of_char_then_byte_starts_even_after_array() -> None:
    layout = offsets(lay_out("      c : Array[0..2] of Char;\n      b : Byte;\n"))
    assert layout == {"c[0]": "0", "c[1]": "1", "c[2]": "2", "b": "4"}


def test_array_after_byte_starts_even_and_negative_bounds() -> None:
    layout = offsets(lay_out("      b : Byte;\n      a : Array[-1..1] of Int;\n"))
    assert layout == {"b": "0", "a[-1]": "2", "a[0]": "4", "a[1]": "6"}


def test_two_dimensional_array_of_int() -> None:
    # Last index varies fastest; each Int 2 bytes.
    layout = lay_out("      m : Array[0..1, 0..2] of Int;\n")
    assert offsets(layout) == {
        "m[0,0]": "0",
        "m[0,1]": "2",
        "m[0,2]": "4",
        "m[1,0]": "6",
        "m[1,1]": "8",
        "m[1,2]": "10",
    }
    assert layout.size == 12


def test_string_then_int() -> None:
    # String[20] takes 20 + 2 = 22 bytes.
    layout = lay_out("      s : String[20];\n      i : Int;\n")
    assert offsets(layout) == {"s": "0", "i": "22"}
    string = layout.candidates[0]
    assert (string.data_type, string.length) == (DataType.STRING, 20)
    assert layout.candidates[1].length is None


def test_string_without_length_is_254_and_odd_string_is_followed_by_byte() -> None:
    layout = offsets(
        lay_out(
            "      s : String;\n      i : Int;\n      t : String[3];\n      b : Byte;\n"
        )
    )
    # String = String[254]: 256 bytes; String[3] at 258 takes 5 bytes, then a
    # Byte at the next free byte 263.
    assert layout == {"s": "0", "i": "256", "t": "258", "b": "263"}


def test_array_of_even_strings() -> None:
    layout = offsets(lay_out("      s : Array[0..1] of String[4];\n      b : Byte;\n"))
    assert layout == {"s[0]": "0", "s[1]": "6", "b": "12"}


def test_udt_instances_in_array() -> None:
    # udtA: a Int at 0, b Bool at 2.0, size 3 -> 4. The array starts at the
    # even byte 2 after the Byte; elements every 4 bytes; Byte after at 10.
    layout = lay_out(
        '      x : Byte;\n      u : Array[0..1] of "udtA";\n      y : Byte;\n', UDT_A
    )
    assert offsets(layout) == {
        "x": "0",
        "u[0].a": "2",
        "u[0].b": "4.0",
        "u[1].a": "6",
        "u[1].b": "8.0",
        "y": "10",
    }


def test_db_based_on_udt() -> None:
    text = (
        "DATA_BLOCK \"D\"\n{ S7_Optimized_Access := 'FALSE' }\nVERSION : 0.1\n"
        '"udtA"\n\nBEGIN\nEND_DATA_BLOCK\n'
    )
    sources = parse_sources([("D.db", text), ("A.udt", UDT_A)])
    layout = layout_db(sources.blocks[0], sources.types, classic_confirmed=False)
    assert (layout.name, layout.number, layout.size) == ("D", None, 4)
    assert offsets(layout) == {"a": "0", "b": "2.0"}
    assert layout.candidates[0].address == S7Address(Area.DB, None, 0, None)


def test_nested_struct_inside_array_of_struct() -> None:
    # Element: flag 0.0; inner starts at 2 (v Int 2, c Char 4, size 3 -> 4),
    # element size 6. Element [1] starts at 6.
    layout = lay_out(
        "      line : Array[0..1] of Struct\n         flag : Bool;\n"
        "         inner : Struct\n            v : Int;\n            c : Char;\n"
        "         END_STRUCT;\n      END_STRUCT;\n"
    )
    assert offsets(layout) == {
        "line[0].flag": "0.0",
        "line[0].inner.v": "2",
        "line[0].inner.c": "4",
        "line[1].flag": "6.0",
        "line[1].inner.v": "8",
        "line[1].inner.c": "10",
    }
    assert layout.size == 12


def test_unsupported_types_have_sizes_and_become_problem_candidates() -> None:
    # Date 2, Time_Of_Day 4, Date_And_Time 8, DTL 12, LTime 8, WChar 2,
    # WString[3] 2*3+4 = 10; all start on even bytes. LWord (8) is supported.
    layout = lay_out(
        "      a : Date;\n      b : Time_Of_Day;\n      c : Date_And_Time;\n"
        "      d : DTL;\n      e : LTime;\n      f : WChar;\n      g : WString[3];\n"
        "      h : LWord;\n      i : Int;\n"
    )
    assert offsets(layout) == {
        "a": "0",
        "b": "2",
        "c": "6",
        "d": "14",
        "e": "26",
        "f": "34",
        "g": "36",
        "h": "46",
        "i": "54",
    }
    by_path = {c.path: c for c in layout.candidates}
    for path in "abcdefg":
        assert by_path[path].data_type is None
        assert by_path[path].problem == "Not supported in 0.5.0"
    assert by_path["h"].data_type is DataType.LWORD
    assert by_path["h"].problem is None
    assert layout.size == 56


def test_every_supported_type_maps_to_its_data_type() -> None:
    names = [t.value for t in DataType if t is not DataType.STRING]
    body = (
        "".join(f"      m_{n} : {n};\n" for n in names)
        + "      m_string : String[5];\n"
    )
    layout = lay_out(body)
    assert {c.path: c.data_type for c in layout.candidates} == {
        f"m_{t.value}": t for t in DataType
    }


def test_candidates_carry_address_and_comments() -> None:
    sources = parse_sources(
        [
            (
                "Recipe.db",
                decode_source((FIXTURES / "Recipe.db").read_bytes(), "Recipe.db"),
            ),
            (
                "Types.udt",
                decode_source((FIXTURES / "Types.udt").read_bytes(), "Types.udt"),
            ),
        ]
    )
    db = sources.blocks[0]
    layout = layout_db(db, sources.types, classic_confirmed=False)
    # Set.Point Real 0; Name String[8] 4 (10 bytes); Motor 1 at 14: Speed 14,
    # Times Array of Time 16, 20; Steps Array[1..2] of typeStep at 24
    # (typeStep: Duration Time 0, Valve typeValve at 4 with Open 4.0,
    # Closed 4.1, typeValve size 1 -> 2, typeStep size 6); Flags at 36.
    assert offsets(layout) == {
        "Set.Point": "0",
        "Name": "4",
        "Motor 1.Speed": "14",
        "Motor 1.Times[0]": "16",
        "Motor 1.Times[1]": "20",
        "Steps[1].Duration": "24",
        "Steps[1].Valve.Open": "28.0",
        "Steps[1].Valve.Closed": "28.1",
        "Steps[2].Duration": "30",
        "Steps[2].Valve.Open": "34.0",
        "Steps[2].Valve.Closed": "34.1",
        "Flags[0,0]": "36.0",
        "Flags[0,1]": "36.1",
        "Flags[1,0]": "36.2",
        "Flags[1,1]": "36.3",
    }
    assert layout.size == 38
    by_path = {c.path: c for c in layout.candidates}
    assert by_path["Set.Point"].comment == "Sollwert in °C"
    assert by_path["Motor 1.Speed"].comment == "Drehzahl"
    assert by_path["Motor 1.Times[1]"].comment == ""
    assert by_path["Steps[2].Duration"].comment == "Dauer"
    assert by_path["Steps[1].Valve.Closed"].comment == "geschlossen"
    assert by_path["Flags[1,1]"] == Candidate(
        path="Flags[1,1]",
        address=S7Address(Area.DB, None, 36, 3),
        data_type=DataType.BOOL,
        length=None,
        comment="",
        problem=None,
    )


def test_addresses_use_the_db_number() -> None:
    layout = lay_out("      a : Bool;\n      b : Real;\n")
    assert layout.number == 3
    assert [c.address for c in layout.candidates] == [
        S7Address(Area.DB, 3, 0, 0),
        S7Address(Area.DB, 3, 2, None),
    ]
    assert [str(c.address) for c in layout.candidates] == ["DB3.0.0", "DB3.2"]


# --- simulator agreement ------------------------------------------------------


def test_demo_source_matches_simulator_layout() -> None:
    demo = ROOT / "deploy" / "demo" / "s7"
    files = [
        (name, decode_source((demo / name).read_bytes(), name))
        for name in ("PressData.db", "PressTypes.udt")
    ]
    sources = parse_sources(files)
    (db,) = sources.blocks
    layout = layout_db(db, sources.types, classic_confirmed=False)
    actual = [
        (
            c.path,
            f"string[{c.length}]"
            if c.data_type is DataType.STRING
            else str(c.data_type),
            offset(c.address),
        )
        for c in layout.candidates
    ]
    expected = [
        (
            row.name,
            f"string[{row.length}]" if row.data_type == "string" else row.data_type,
            row.offset,
        )
        for row in documented_rows()
    ]
    assert actual == expected
    assert all(c.problem is None for c in layout.candidates)
    assert layout.size == simulator.DB1_SIZE


# --- refusals -------------------------------------------------------------


def test_optimized_db_refused() -> None:
    text = db_text("      a : Int;\n", "{ S7_Optimized_Access := 'TRUE' }")
    error = refused(text, classic=True)
    assert (error.file, error.line) == ("R.db", 1)
    assert error.message == (
        "DB is optimized; switch off 'Optimized block access' in TIA, compile, "
        "download, export again."
    )


def test_missing_attribute_needs_the_s7_300_400_tick() -> None:
    text = db_text("      a : Int;\n", "")
    error = refused(text)
    assert (error.file, error.line) == ("R.db", 1)
    assert "S7-300/400" in error.message
    sources = parse_sources([("R.db", text)])
    layout = layout_db(sources.blocks[0], sources.types, classic_confirmed=True)
    assert offsets(layout) == {"a": "0"}


def test_missing_udt_refused() -> None:
    error = refused(db_text('      a : Int;\n      u : "typeGone";\n'))
    assert (error.file, error.line) == ("R.db", 6)
    assert '"typeGone"' in error.message


def test_missing_udt_inside_udt_names_the_udt_file() -> None:
    outer = udt("outer", '      x : Int;\n      y : "inner";\n')
    error = refused(db_text('      o : "outer";\n'), ("O.udt", outer))
    assert (error.file, error.line) == ("O.udt", 4)
    assert '"inner"' in error.message


def test_instance_db_refused() -> None:
    text = (FIXTURES / "Instance.db").read_text("utf-8")
    error = refused(text)
    assert (error.file, error.line) == ("R.db", 1)
    assert '"PressControl"' in error.message
    assert "instance DB" in error.message
    classic = "DATA_BLOCK DB 5\nFB 10\nBEGIN\nEND_DATA_BLOCK\n"
    assert "instance DB" in refused(classic, classic=True).message


@pytest.mark.parametrize("name", ["Variant", "Pointer", "Any", "DB_ANY", "VARIANT"])
def test_variant_pointer_any_db_any_refused(name: str) -> None:
    error = refused(db_text(f"      a : Int;\n      b : {name};\n"))
    assert (error.file, error.line) == ("R.db", 6)
    assert name in error.message


def test_unknown_type_refused() -> None:
    error = refused(db_text("      a : Int;\n      t : IEC_TIMER;\n"))
    assert (error.file, error.line) == ("R.db", 6)
    assert "IEC_TIMER" in error.message


def test_array_of_odd_strings_refused() -> None:
    error = refused(db_text("      a : Int;\n      s : Array[0..1] of String[5];\n"))
    assert (error.file, error.line) == ("R.db", 6)
    assert "String[5]" in error.message


def test_recursive_udt_refused() -> None:
    loop = udt("loop", '      x : Int;\n      y : "loop";\n')
    error = refused(db_text('      o : "loop";\n'), ("L.udt", loop))
    assert (error.file, error.line) == ("L.udt", 4)
    assert '"loop"' in error.message


def test_empty_struct_refused() -> None:
    error = refused(db_text("      a : Int;\n      s : Struct\n      END_STRUCT;\n"))
    assert (error.file, error.line) == ("R.db", 6)


def test_db_larger_than_address_space_refused() -> None:
    error = refused(db_text("      a : Array[0..40000] of Int;\n"))
    assert (error.file, error.line) == ("R.db", 5)
    assert "65536" in error.message


def test_every_elementary_type_the_parser_knows_has_a_size() -> None:
    for name in sorted(ELEMENTARY_TYPES):
        layout = lay_out(f"      a : {name};\n      b : Byte;\n")
        assert [c.path for c in layout.candidates] == ["a", "b"], name
