"""Standard-access offsets of a TIA data block, as Tag candidates.

The rules (Siemens' memory layout of blocks with standard access, as on the
S7-300/400; see the spec's "Offset rules"):

* ``Bool`` takes one bit; consecutive Bools fill bits 0-7 of a byte.
* ``Byte``, ``Char``, ``SInt``, ``USInt`` start at the next free byte.
* Every other elementary type, ``String``, ``Struct``, ``Array`` and UDT
  instance starts at the next even byte.
* A Struct, UDT or Array ends on an even byte (its size rounds up to even), so
  the next member starts on an even byte; ``Array of Bool`` packs its bits.
* ``String[n]`` takes n + 2 bytes; ``WString[n]`` 2n + 4.

What cannot be laid out with certainty is refused with ``TiaSourceError``
instead of guessed: a wrong offset would silently read the wrong bytes.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from plc_data_collector.protocols.s7_tags import Area, DataType, S7Address, byte_span
from plc_data_collector.tia.source import (
    MAX_NESTING,
    Array,
    DbSource,
    Elementary,
    Member,
    Named,
    Struct,
    TiaSourceError,
    TypeRef,
    TypeSource,
)

UNSUPPORTED_PROBLEM = "Not supported in 0.5.0"
OPTIMIZED_MESSAGE = (
    "DB is optimized; switch off 'Optimized block access' in TIA, compile, "
    "download, export again."
)
UNKNOWN_ACCESS_MESSAGE = (
    "The source has no S7_Optimized_Access attribute. That is what sources from "
    "an S7-300/400 look like (those CPUs have no optimized access); tick "
    '"This DB is from an S7-300/400 (standard access)" to import it. A DB from '
    "an S7-1200/1500 must be exported with the attribute."
)

_SUPPORTED = {data_type.value: data_type for data_type in DataType}
_UNSUPPORTED_SIZES = {
    "date": 2,
    "s5time": 2,
    "wchar": 2,
    "time_of_day": 4,
    "ltime": 8,
    "ltime_of_day": 8,
    "date_and_time": 8,
    "ldt": 8,
    "dtl": 12,
}
_BYTE_TYPES = frozenset({"byte", "char", "sint", "usint"})
_NO_FIXED_LAYOUT = frozenset({"variant", "pointer", "any", "db_any"})
_ADDRESS_SPACE = 65536
_TOO_LARGE = (
    f"The DB is larger than {_ADDRESS_SPACE} bytes; S7 addresses end at byte 65535."
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One leaf member of a DB, ready to become a Tag.

    ``path`` is relative to the DB (``Motor.Speed``, ``Temps[3]``,
    ``Line[1,2].Temp``). ``data_type`` is None for a type this release cannot
    read; ``problem`` then says why. ``length`` is the String length.
    """

    path: str
    address: S7Address | None
    data_type: DataType | None
    length: int | None
    comment: str
    problem: str | None
    source_address: str = ""


@dataclass(frozen=True, slots=True)
class DbLayout:
    """The laid-out DB: its size in bytes and its candidates in address order.

    ``number`` is the DB number the source states (None when it states only a
    name); the candidates' addresses carry the same number, so set it on the
    ``DbSource`` (``dataclasses.replace``) before laying out to get addresses
    that can be stored.
    """

    name: str
    number: int | None
    size: int
    candidates: tuple[Candidate, ...]


def layout_db(
    db: DbSource, types: Mapping[str, TypeSource], *, classic_confirmed: bool
) -> DbLayout:
    """Lay out ``db`` with standard access; raise ``TiaSourceError`` when it
    cannot be imported.

    ``classic_confirmed`` is the user's confirmation that a source without the
    ``S7_Optimized_Access`` attribute comes from an S7-300/400.
    """
    base: TypeSource | None = None
    if db.base_type is not None:
        base = types.get(db.base_type)
        if base is None:
            raise TiaSourceError(
                db.file,
                db.line,
                f'The DB is based on "{db.base_type}", which is not a PLC data type '
                "in the uploaded files. An instance DB of a function block cannot "
                f'be imported; if "{db.base_type}" is a PLC data type, add its .udt '
                "file.",
            )
    if db.access == "optimized":
        raise TiaSourceError(db.file, db.line, OPTIMIZED_MESSAGE)
    if db.access == "unknown" and not classic_confirmed:
        raise TiaSourceError(db.file, db.line, UNKNOWN_ACCESS_MESSAGE)
    layout = _Layout(db.number, types)
    if base is not None:
        layout.place_udt(base, "", db.file, db.line)
    else:
        layout.place_members(db.members, "", db.file)
    layout.align_even()
    return DbLayout(db.name, db.number, layout.byte, tuple(layout.candidates))


def _join(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


class _Layout:
    """A cursor (byte, bit) walking the members in declaration order."""

    def __init__(self, number: int | None, types: Mapping[str, TypeSource]) -> None:
        self.byte = 0
        self.bit = 0
        self.candidates: list[Candidate] = []
        self._number = number
        self._types = types
        self._open_types: list[str] = []
        self._depth = 0

    def next_free_byte(self) -> None:
        if self.bit:
            self.byte += 1
            self.bit = 0

    def align_even(self) -> None:
        self.next_free_byte()
        self.byte += self.byte % 2

    def place_members(
        self, members: tuple[Member, ...], prefix: str, file: str
    ) -> None:
        for member in members:
            self.place(
                member.type,
                _join(prefix, member.name),
                member.comment,
                file,
                member.line,
            )

    def place(
        self, type_ref: TypeRef, path: str, comment: str, file: str, line: int
    ) -> None:
        if isinstance(type_ref, Elementary):
            self._place_elementary(type_ref, path, comment, file, line)
            return
        if self._depth >= MAX_NESTING:
            raise TiaSourceError(
                file,
                line,
                f"Structs, arrays and PLC data types nest deeper than {MAX_NESTING} "
                "levels.",
            )
        self._depth += 1
        try:
            self._place_nested(type_ref, path, comment, file, line)
        finally:
            self._depth -= 1

    def _place_nested(
        self,
        type_ref: Struct | Array | Named,
        path: str,
        comment: str,
        file: str,
        line: int,
    ) -> None:
        if isinstance(type_ref, Struct):
            if not type_ref.members:
                raise TiaSourceError(
                    file, line, "A Struct without members cannot be laid out."
                )
            self.align_even()
            self.place_members(type_ref.members, path, file)
            self.align_even()
        elif isinstance(type_ref, Array):
            self._place_array(type_ref, path, comment, file, line)
        else:
            self._place_named(type_ref, path, file, line)

    def place_udt(self, udt: TypeSource, path: str, file: str, line: int) -> None:
        if udt.name in self._open_types:
            raise TiaSourceError(
                file, line, f'PLC data type "{udt.name}" contains itself.'
            )
        if not udt.members:
            raise TiaSourceError(
                file,
                line,
                f'PLC data type "{udt.name}" has no members and cannot be laid out.',
            )
        self._open_types.append(udt.name)
        self.align_even()
        self.place_members(udt.members, path, udt.file)
        self.align_even()
        self._open_types.pop()

    def _place_named(self, named: Named, path: str, file: str, line: int) -> None:
        udt = self._types.get(named.name)
        if udt is not None:
            self.place_udt(udt, path, file, line)
        elif named.name.lower() in _NO_FIXED_LAYOUT:
            raise TiaSourceError(
                file,
                line,
                f"{named.name} cannot be imported: Variant, Pointer, Any and DB_ANY "
                "have no fixed layout that could be read as a Tag.",
            )
        elif named.quoted:
            raise TiaSourceError(
                file,
                line,
                f'PLC data type "{named.name}" is not in the uploaded files; add its '
                ".udt file.",
            )
        else:
            raise TiaSourceError(
                file,
                line,
                f'Unknown construct "{named.name}": only elementary types, String, '
                "Struct, Array and PLC data types (UDTs) can be imported.",
            )

    def _place_array(
        self, array: Array, path: str, comment: str, file: str, line: int
    ) -> None:
        element = array.element
        if (
            isinstance(element, Elementary)
            and element.name == "string"
            and element.string_length is not None
            and element.string_length % 2
        ):
            raise TiaSourceError(
                file,
                line,
                f"Array of String[{element.string_length}] cannot be laid out: where "
                "each element of an array of strings with an odd length starts is "
                "not certain, so the import refuses rather than guess.",
            )
        self.align_even()
        # Refuse before expanding: every element takes at least one bit (Bool)
        # or one byte (anything else, as empty Structs/UDTs are refused).
        count = 1
        for low, high in array.dims:
            count *= high - low + 1
        element_bits = 1 if element == Elementary("bool") else 8
        free_bits = (_ADDRESS_SPACE - self.byte) * 8
        if count * element_bits > free_bits:
            raise TiaSourceError(file, line, _TOO_LARGE)
        for index in _indices(array.dims):
            suffix = ",".join(str(i) for i in index)
            self.place(element, f"{path}[{suffix}]", comment, file, line)
        self.align_even()

    def _place_elementary(
        self, elementary: Elementary, path: str, comment: str, file: str, line: int
    ) -> None:
        name = elementary.name
        data_type = _SUPPORTED.get(name)
        if name == "bool":
            bit: int | None = self.bit
        else:
            bit = None
            if name in _BYTE_TYPES:
                self.next_free_byte()
            else:
                self.align_even()
        if data_type is not None:
            length = elementary.string_length if data_type is DataType.STRING else None
            span = byte_span(data_type, length)
            problem = None
        else:
            length = None
            span = _unsupported_size(elementary)
            problem = UNSUPPORTED_PROBLEM
        end = self.byte + (1 if bit is not None else span)
        if end > _ADDRESS_SPACE:
            raise TiaSourceError(file, line, _TOO_LARGE)
        address = S7Address(Area.DB, self._number, self.byte, bit)
        self.candidates.append(
            Candidate(path, address, data_type, length, comment, problem)
        )
        if bit is None:
            self.byte += span
        elif self.bit == 7:
            self.byte += 1
            self.bit = 0
        else:
            self.bit += 1


def _unsupported_size(elementary: Elementary) -> int:
    if elementary.name == "wstring":
        assert elementary.string_length is not None  # the parser always sets it
        return 2 * elementary.string_length + 4
    return _UNSUPPORTED_SIZES[elementary.name]


def _indices(dims: tuple[tuple[int, int], ...]) -> Iterator[tuple[int, ...]]:
    """Array indices in storage order (last index fastest), one at a time."""
    index = [low for low, _ in dims]
    while True:
        yield tuple(index)
        position = len(dims) - 1
        while position >= 0 and index[position] == dims[position][1]:
            index[position] = dims[position][0]
            position -= 1
        if position < 0:
            return
        index[position] += 1
