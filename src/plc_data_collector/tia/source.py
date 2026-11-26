"""Parser of TIA Portal DB and UDT sources ("Generate source from blocks").

Reads the SCL-like text TIA writes into ``.db`` and ``.udt`` files:
``DATA_BLOCK "Name"`` with its header, then ``STRUCT ... END_STRUCT;`` or the
name of the type the DB is based on, then ``BEGIN ... END_DATA_BLOCK``; and
``TYPE "Name" ... STRUCT ... END_STRUCT; END_TYPE``. Comments, attributes other
than ``S7_Optimized_Access``, initial values and the ``BEGIN`` section are
skipped. Anything else the parser does not know raises ``TiaSourceError`` with
the file and line: a guess here could later read the wrong bytes from a PLC.

The parser only records the declarations; ``tia.layout`` turns a DB into
offsets and refuses what cannot be laid out (optimized access, instance DBs,
missing UDTs, Variant and the like).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

Access = Literal["standard", "optimized", "unknown"]

# Elementary type names (lower case, canonical TIA spelling) and the aliases
# TIA also accepts. ``tia.layout`` knows the size of each.
ELEMENTARY_TYPES = frozenset(
    {
        "bool",
        "byte",
        "word",
        "dword",
        "lword",
        "char",
        "sint",
        "usint",
        "int",
        "uint",
        "dint",
        "udint",
        "lint",
        "ulint",
        "real",
        "lreal",
        "string",
        "time",
        "date",
        "time_of_day",
        "date_and_time",
        "dtl",
        "ltime",
        "ltime_of_day",
        "ldt",
        "s5time",
        "wchar",
        "wstring",
    }
)
_ALIASES = {
    "tod": "time_of_day",
    "dt": "date_and_time",
    "ltod": "ltime_of_day",
    "date_and_ltime": "ldt",
}
_STRING_LIMITS = {"string": 254, "wstring": 16382}
_HEADER_LINES = frozenset({"title", "author", "family", "name", "version"})
_HEADER_FLAGS = frozenset(
    {"non_retain", "know_how_protect", "read_only", "unlinked", "code_version1"}
)
_NUMBERED = re.compile(r"(DB|FB|SFB|UDT)(\d+)", re.IGNORECASE)
_DIGITS = frozenset("0123456789")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TITLE = re.compile(r"TITLE[ \t]*=[^\n]*", re.IGNORECASE)
_MAX_BLOCK_NUMBER = 65535
# Array bounds are DInt in TIA; more digits than that are never a valid number.
_DINT_MIN = -(2**31)
_DINT_MAX = 2**31 - 1
_MAX_DIGITS = 10
# Struct/Array nesting deeper than this is refused (no real DB comes close).
MAX_NESTING = 64


class TiaSourceError(Exception):
    """A source that cannot be parsed or a DB that cannot be laid out."""

    def __init__(self, file: str, line: int, message: str) -> None:
        """Record where the problem is and what it is."""
        super().__init__(f"{file}, line {line}: {message}")
        self.file = file
        self.line = line
        self.message = message


@dataclass(frozen=True, slots=True)
class Elementary:
    """An elementary type; ``string_length`` only for String and WString."""

    name: str
    string_length: int | None = None


@dataclass(frozen=True, slots=True)
class Struct:
    """An anonymous ``Struct ... END_STRUCT``."""

    members: tuple[Member, ...]


@dataclass(frozen=True, slots=True)
class Array:
    """``Array[a..b, c..d] of element``; ``dims`` are inclusive bounds."""

    dims: tuple[tuple[int, int], ...]
    element: TypeRef


@dataclass(frozen=True, slots=True)
class Named:
    """A type referenced by name: a UDT (quoted) or a system type (unquoted)."""

    name: str
    quoted: bool


TypeRef = Elementary | Struct | Array | Named


@dataclass(frozen=True, slots=True)
class Member:
    """One declared member; ``comment`` is the ``//`` comment on its line."""

    name: str
    type: TypeRef
    comment: str
    line: int


@dataclass(frozen=True, slots=True)
class TypeSource:
    """A PLC data type (``TYPE ... END_TYPE``)."""

    name: str
    members: tuple[Member, ...]
    file: str
    line: int


@dataclass(frozen=True, slots=True)
class DbSource:
    """A data block; ``base_type`` is set (and ``members`` empty) when the DB
    is declared by a type name instead of a STRUCT."""

    name: str
    number: int | None
    access: Access
    base_type: str | None
    members: tuple[Member, ...]
    line: int
    file: str


@dataclass(frozen=True, slots=True)
class SourceSet:
    """Everything declared in a set of uploaded source files."""

    blocks: tuple[DbSource, ...]
    types: Mapping[str, TypeSource]


def decode_source(data: bytes, file: str) -> str:
    """Decode an uploaded source: UTF-8 (BOM stripped), else Windows-1252."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        raise TiaSourceError(
            file, 1, "The file is neither UTF-8 nor Windows-1252 text."
        ) from None


def parse_sources(files: Sequence[tuple[str, str]]) -> SourceSet:
    """Parse ``(file name, text)`` pairs; raise ``TiaSourceError`` on the first
    problem."""
    blocks: list[DbSource] = []
    types: dict[str, TypeSource] = {}
    for file, text in files:
        file_blocks, file_types = _Parser(file, text.removeprefix("﻿")).parse()
        blocks.extend(file_blocks)
        for udt in file_types:
            if udt.name in types:
                raise TiaSourceError(
                    file, udt.line, f'PLC data type "{udt.name}" is defined twice.'
                )
            types[udt.name] = udt
    return SourceSet(tuple(blocks), types)


# --- lexer ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Token:
    kind: Literal["word", "quoted", "text", "number", "symbol", "end"]
    text: str
    line: int


def _tokenize(file: str, source: str) -> tuple[list[_Token], dict[int, str]]:
    """Split the source into tokens; return them and the first ``//`` comment
    of each line."""
    tokens: list[_Token] = []
    comments: dict[int, str] = {}
    line = 1
    index = 0
    size = len(source)
    while index < size:
        char = source[index]
        if char == "\n":
            line += 1
            index += 1
        elif char.isspace():
            index += 1
        elif source.startswith("//", index):
            end = source.find("\n", index)
            end = size if end < 0 else end
            comments.setdefault(line, source[index + 2 : end].strip())
            index = end
        elif source.startswith("(*", index) or source.startswith("/*", index):
            closing = "*)" if char == "(" else "*/"
            end = source.find(closing, index + 2)
            if end < 0:
                raise TiaSourceError(
                    file, line, f'The comment opened with "{char}*" is never closed.'
                )
            line += source.count("\n", index, end)
            index = end + 2
        elif char == '"':
            end = source.find('"', index + 1)
            if end < 0 or "\n" in source[index:end]:
                raise TiaSourceError(file, line, "A quoted name is not closed.")
            tokens.append(_Token("quoted", source[index + 1 : end], line))
            index = end + 1
        elif char == "'":
            end = _skip_text(file, source, index, line)
            tokens.append(_Token("text", source[index + 1 : end - 1], line))
            index = end
        elif match := _TITLE.match(source, index):
            index = match.end()  # free text up to the end of the line
        elif match := _IDENTIFIER.match(source, index):
            tokens.append(_Token("word", match.group(), line))
            index = match.end()
        elif char in _DIGITS:
            end = index
            while end < size and source[end] in _DIGITS:
                end += 1
            tokens.append(_Token("number", source[index:end], line))
            index = end
        elif source.startswith(":=", index) or source.startswith("..", index):
            tokens.append(_Token("symbol", source[index : index + 2], line))
            index += 2
        else:
            tokens.append(_Token("symbol", char, line))
            index += 1
    tokens.append(_Token("end", "", tokens[-1].line if tokens else line))
    return tokens, comments


def _skip_text(file: str, source: str, index: int, line: int) -> int:
    """Skip a quoted text (``$`` escapes the next character); return the
    index after its closing quote."""
    position = index + 1
    while position < len(source):
        char = source[position]
        if char == "\n":
            break
        if char == "$":
            position += 2
            continue
        if char == "'":
            return position + 1
        position += 1
    raise TiaSourceError(file, line, "A text in single quotes is not closed.")


# --- parser --------------------------------------------------------------------


def _describe(token: _Token) -> str:
    if token.kind == "end":
        return "end of file"
    if token.kind == "text":
        return "a quoted text"
    return f'"{token.text}"'


class _Parser:
    """Recursive-descent parser over the tokens of one file."""

    def __init__(self, file: str, source: str) -> None:
        self._file = file
        self._tokens, self._comments = _tokenize(file, source)
        self._position = 0
        self._depth = 0

    # token helpers

    def _peek(self, ahead: int = 0) -> _Token:
        return self._tokens[min(self._position + ahead, len(self._tokens) - 1)]

    def _next(self) -> _Token:
        token = self._peek()
        if token.kind != "end":
            self._position += 1
        return token

    def _error(self, token: _Token, message: str) -> TiaSourceError:
        return TiaSourceError(self._file, token.line, message)

    @staticmethod
    def _is(token: _Token, *words: str) -> bool:
        return token.kind == "word" and token.text.lower() in words

    @staticmethod
    def _is_symbol(token: _Token, symbol: str) -> bool:
        return token.kind == "symbol" and token.text == symbol

    def _expect_symbol(self, symbol: str, context: str) -> _Token:
        token = self._next()
        if not self._is_symbol(token, symbol):
            raise self._error(
                token, f'Expected "{symbol}" {context} but found {_describe(token)}.'
            )
        return token

    def _expect_word(self, word: str, context: str) -> _Token:
        token = self._next()
        if not self._is(token, word.lower()):
            raise self._error(
                token, f"Expected {word} {context} but found {_describe(token)}."
            )
        return token

    def _integer(self, token: _Token, digits: str) -> int:
        """The value of a run of digits; refuse more digits than a DInt has."""
        if len(digits.lstrip("0")) > _MAX_DIGITS:
            raise self._error(token, f"The number {digits[:12]}... is too large.")
        return int(digits)

    def _optional_semicolon(self) -> None:
        if self._is_symbol(self._peek(), ";"):
            self._next()

    # blocks

    def parse(self) -> tuple[list[DbSource], list[TypeSource]]:
        blocks: list[DbSource] = []
        types: list[TypeSource] = []
        while (token := self._peek()).kind != "end":
            if self._is(token, "data_block"):
                blocks.append(self._data_block())
            elif self._is(token, "type"):
                types.append(self._type_block())
            else:
                raise self._error(
                    token,
                    f"Unknown construct {_describe(token)}; a .db or .udt source "
                    "holds DATA_BLOCK and TYPE blocks.",
                )
        return blocks, types

    def _block_name(self) -> tuple[str, int | None]:
        token = self._next()
        if token.kind == "quoted":
            return token.text, None
        if token.kind == "word":
            numbered = self._numbered(token)
            if numbered is not None:
                return numbered
            return token.text, None
        raise self._error(token, f"Expected a block name but found {_describe(token)}.")

    def _numbered(self, token: _Token) -> tuple[str, int] | None:
        """``DB 12`` / ``DB12`` (and FB, SFB, UDT): canonical name and number."""
        if match := _NUMBERED.fullmatch(token.text):
            kind, digits = match.group(1).upper(), match.group(2)
        elif (
            self._is(token, "db", "fb", "sfb", "udt") and self._peek().kind == "number"
        ):
            kind, digits = token.text.upper(), self._next().text
        else:
            return None
        number = self._integer(token, digits)
        if not 1 <= number <= _MAX_BLOCK_NUMBER:
            raise self._error(token, f"Block number {number} is outside 1-65535.")
        return f"{kind}{number}", number

    def _header(self) -> Access:
        """Skip the block header; return the access its attributes state."""
        access: Access = "unknown"
        while True:
            token = self._peek()
            if self._is_symbol(token, "{"):
                for name, value in self._attributes():
                    if name.text.lower() == "s7_optimized_access":
                        access = self._access(value)
            elif self._is(token, *_HEADER_LINES):
                while self._peek().line == token.line and self._peek().kind != "end":
                    self._next()
            elif self._is(token, *_HEADER_FLAGS):
                self._next()
            else:
                return access

    def _access(self, value: _Token) -> Access:
        text = value.text.upper()
        if text == "TRUE":
            return "optimized"
        if text == "FALSE":
            return "standard"
        raise self._error(
            value, f"S7_Optimized_Access must be 'TRUE' or 'FALSE', not '{text}'."
        )

    def _attributes(self) -> list[tuple[_Token, _Token]]:
        """Parse ``{ Name := 'value'; ... }``; return the (name, value) pairs."""
        self._expect_symbol("{", "to open the attributes")
        pairs: list[tuple[_Token, _Token]] = []
        while not self._is_symbol(self._peek(), "}"):
            name = self._next()
            if name.kind != "word":
                raise self._error(
                    name, f"Expected an attribute name but found {_describe(name)}."
                )
            self._expect_symbol(":=", f"after attribute {name.text}")
            value = self._next()
            if value.kind != "text":
                raise self._error(
                    value,
                    f"Expected a quoted value for attribute {name.text} but found "
                    f"{_describe(value)}.",
                )
            pairs.append((name, value))
            if self._is_symbol(self._peek(), ";"):
                self._next()
            elif not self._is_symbol(self._peek(), "}"):
                token = self._peek()
                raise self._error(
                    token, f'Expected ";" or "}}" but found {_describe(token)}.'
                )
        self._next()
        return pairs

    def _data_block(self) -> DbSource:
        start = self._next()
        name, number = self._block_name()
        if number is not None and not name.upper().startswith("DB"):
            raise self._error(start, f"{name} is not a data block.")
        access = self._header()
        members: tuple[Member, ...] = ()
        base_type: str | None = None
        token = self._peek()
        if self._is(token, "struct"):
            self._next()
            members = self._members()
        elif token.kind == "quoted":
            base_type = self._next().text
        elif token.kind == "word" and (
            self._is(self._peek(1), "begin", "end_data_block")
            or self._is_symbol(self._peek(1), ";")
            or (self._is(token, "fb", "sfb", "udt") and self._peek(1).kind == "number")
        ):
            self._next()
            numbered = self._numbered(token)
            base_type = token.text if numbered is None else numbered[0]
        else:
            raise self._error(token, f"Unknown construct {_describe(token)}.")
        self._optional_semicolon()
        token = self._next()
        if self._is(token, "begin"):
            while not self._is(self._peek(), "end_data_block"):
                if self._peek().kind == "end":
                    raise self._error(
                        self._peek(), "Expected END_DATA_BLOCK but found end of file."
                    )
                self._next()
            token = self._next()
        if not self._is(token, "end_data_block"):
            raise self._error(
                token, f"Expected BEGIN or END_DATA_BLOCK but found {_describe(token)}."
            )
        return DbSource(
            name, number, access, base_type, members, start.line, self._file
        )

    def _type_block(self) -> TypeSource:
        start = self._next()
        name, _ = self._block_name()
        self._header()
        self._expect_word("STRUCT", f'in PLC data type "{name}"')
        members = self._members()
        self._optional_semicolon()
        self._expect_word("END_TYPE", f'to close PLC data type "{name}"')
        return TypeSource(name, members, self._file, start.line)

    # members and types

    def _members(self) -> tuple[Member, ...]:
        """Members up to and including ``END_STRUCT`` (and its ``;``)."""
        members: list[Member] = []
        while not self._is(self._peek(), "end_struct"):
            members.append(self._member())
        self._next()
        self._optional_semicolon()
        return tuple(members)

    def _member(self) -> Member:
        token = self._next()
        if token.kind not in ("word", "quoted"):
            raise self._error(
                token, f"Expected a member name but found {_describe(token)}."
            )
        if self._is_symbol(self._peek(), "{"):
            self._attributes()
        self._expect_symbol(":", f'after member "{token.text}"')
        type_ref, struct_line = self._type()
        if struct_line is not None:
            comment_line = struct_line
        else:
            if self._is_symbol(self._peek(), ":="):
                self._skip_initial_value()
            comment_line = self._expect_symbol(
                ";", f'after the type of member "{token.text}"'
            ).line
        return Member(
            token.text, type_ref, self._comments.get(comment_line, ""), token.line
        )

    def _type(self) -> tuple[TypeRef, int | None]:
        """Parse a type; for a Struct (or array of Struct) also return the line
        of the ``Struct`` keyword, which carries the member's comment."""
        token = self._next()
        if self._is(token, "struct", "array"):
            if self._depth >= MAX_NESTING:
                raise self._error(
                    token,
                    f"Structs and arrays nest deeper than {MAX_NESTING} levels.",
                )
            self._depth += 1
            try:
                return self._nested_type(token)
            finally:
                self._depth -= 1
        if token.kind == "quoted":
            return Named(token.text, quoted=True), None
        if token.kind == "word":
            name = token.text.lower()
            name = _ALIASES.get(name, name)
            if name in _STRING_LIMITS:
                return Elementary(name, self._string_length(name)), None
            if name in ELEMENTARY_TYPES:
                return Elementary(name), None
            return Named(token.text, quoted=False), None
        raise self._error(token, f"Expected a type but found {_describe(token)}.")

    def _nested_type(self, token: _Token) -> tuple[TypeRef, int | None]:
        """The rest of a ``Struct`` or ``Array`` type after its keyword."""
        if self._is(token, "struct"):
            self._optional_semicolon()
            return Struct(self._members()), token.line
        dims = self._dims()
        self._expect_word("of", "after the array bounds")
        if self._is(self._peek(), "array"):
            raise self._error(
                self._peek(),
                "Array of Array is not supported; TIA declares a "
                "multi-dimensional array as Array[a..b, c..d].",
            )
        element, struct_line = self._type()
        return Array(dims, element), struct_line

    def _string_length(self, name: str) -> int:
        limit = _STRING_LIMITS[name]
        if not self._is_symbol(self._peek(), "["):
            return 254
        self._next()
        token = self._next()
        if token.kind != "number":
            raise self._error(
                token, f"Expected a {name} length but found {_describe(token)}."
            )
        length = self._integer(token, token.text)
        if not 1 <= length <= limit:
            label = "String" if name == "string" else "WString"
            raise self._error(token, f"{label} length must be 1-{limit}, not {length}.")
        self._expect_symbol("]", f"after the {name} length")
        return length

    def _dims(self) -> tuple[tuple[int, int], ...]:
        self._expect_symbol("[", "after Array")
        dims: list[tuple[int, int]] = []
        while True:
            low = self._bound()
            self._expect_symbol("..", "between array bounds")
            high = self._bound()
            if high < low:
                raise self._error(
                    self._peek(), f"Array bounds {low}..{high} are reversed."
                )
            dims.append((low, high))
            token = self._next()
            if self._is_symbol(token, "]"):
                return tuple(dims)
            if not self._is_symbol(token, ","):
                raise self._error(
                    token, f'Expected "," or "]" but found {_describe(token)}.'
                )

    def _bound(self) -> int:
        sign = 1
        if self._peek().kind == "symbol" and self._peek().text in ("+", "-"):
            sign = -1 if self._next().text == "-" else 1
        token = self._next()
        if token.kind != "number":
            raise self._error(
                token,
                f"Expected a number as array bound but found {_describe(token)}; "
                "only literal bounds can be laid out.",
            )
        bound = sign * self._integer(token, token.text)
        if not _DINT_MIN <= bound <= _DINT_MAX:
            raise self._error(
                token, f"Array bound {bound} is outside the DInt range of TIA."
            )
        return bound

    def _skip_initial_value(self) -> None:
        """Skip ``:= value`` up to (not including) the ``;`` that ends it."""
        start = self._next()
        depth = 0
        while True:
            token = self._peek()
            if token.kind == "end":
                raise self._error(start, 'The initial value is never ended by ";".')
            if token.kind == "symbol":
                if token.text in ("[", "("):
                    depth += 1
                elif token.text in ("]", ")"):
                    depth -= 1
                elif token.text == ";" and depth <= 0:
                    return
            self._next()
