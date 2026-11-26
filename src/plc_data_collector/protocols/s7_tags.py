"""Siemens S7 tag model, address forms and value decoding (no I/O)."""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    PermanentProtocolError,
    TagConfig,
    TagValue,
    ValueType,
)
from plc_data_collector.protocols.endpoints import parse_endpoint
from plc_data_collector.protocols.modbus_tags import INT64_MAX, INT64_MIN

S7_PROTOCOL = "s7"
MAX_STRING_LENGTH = 254
_ADDRESS_SPACE = 65536
_MAX_DB = 65535
_MAX_BIT = 7


class Area(StrEnum):
    """S7 memory area."""

    DB = "db"
    M = "m"
    I = "i"  # noqa: E741
    Q = "q"

    @property
    def wire_code(self) -> int:
        """Area code used in S7 read requests."""
        return _AREA_INFO[self][0]

    @property
    def label(self) -> str:
        """Human-readable area name."""
        return _AREA_INFO[self][1]


_AREA_INFO: dict[Area, tuple[int, str]] = {
    Area.DB: (0x84, "Data block"),
    Area.M: (0x83, "Merkers"),
    Area.I: (0x81, "Inputs"),
    Area.Q: (0x82, "Outputs"),
}


class DataType(StrEnum):
    """Tag data type."""

    BOOL = "bool"
    BYTE = "byte"
    WORD = "word"
    DWORD = "dword"
    LWORD = "lword"
    CHAR = "char"
    SINT = "sint"
    USINT = "usint"
    INT = "int"
    UINT = "uint"
    DINT = "dint"
    UDINT = "udint"
    LINT = "lint"
    ULINT = "ulint"
    REAL = "real"
    LREAL = "lreal"
    STRING = "string"
    TIME = "time"


_SPANS = {
    DataType.BOOL: 1,
    DataType.BYTE: 1,
    DataType.CHAR: 1,
    DataType.SINT: 1,
    DataType.USINT: 1,
    DataType.WORD: 2,
    DataType.INT: 2,
    DataType.UINT: 2,
    DataType.DWORD: 4,
    DataType.DINT: 4,
    DataType.UDINT: 4,
    DataType.REAL: 4,
    DataType.TIME: 4,
    DataType.LWORD: 8,
    DataType.LINT: 8,
    DataType.ULINT: 8,
    DataType.LREAL: 8,
}
_STRUCT_FORMATS = {
    DataType.BYTE: ">B",
    DataType.WORD: ">H",
    DataType.DWORD: ">I",
    DataType.LWORD: ">Q",
    DataType.SINT: ">b",
    DataType.USINT: ">B",
    DataType.INT: ">h",
    DataType.UINT: ">H",
    DataType.DINT: ">i",
    DataType.UDINT: ">I",
    DataType.LINT: ">q",
    DataType.ULINT: ">Q",
    DataType.REAL: ">f",
    DataType.LREAL: ">d",
    DataType.TIME: ">i",
}
_FLOAT_TYPES = (DataType.REAL, DataType.LREAL)
_NO_SCALE_TYPES = (DataType.BOOL, DataType.CHAR, DataType.STRING)


def byte_span(data_type: DataType, length: int | None) -> int:
    """Number of bytes a value of the type occupies (a bool is one byte)."""
    if data_type is DataType.STRING:
        if length is None:
            raise ValueError("length is required for data_type string")
        return length + 2
    return _SPANS[data_type]


def _readable(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        field = ".".join(str(p) for p in item["loc"]) or "options"
        message = str(item["msg"]).removeprefix("Value error, ")
        parts.append(f"{field}: {message}")
    return "; ".join(parts)


class S7ConnectionOptions(BaseModel):
    """Connection-level S7 options."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rack: int = Field(default=0, ge=0, le=7)
    slot: int = Field(default=1, ge=0, le=31)
    max_gap: int = Field(default=16, ge=0, le=256)

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> Self:
        """Validate a stored options mapping."""
        try:
            return cls.model_validate(dict(options))
        except ValidationError as error:
            raise ConfigurationError(
                f"Invalid S7 connection options: {_readable(error)}",
                code="invalid_s7_connection",
            ) from error


class S7TagOptions(BaseModel):
    """Tag-level S7 options (strict numbers, unknown keys refused)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_type: DataType
    length: int | None = Field(default=None, ge=1, le=MAX_STRING_LENGTH, strict=True)
    scale: float | None = Field(default=None, allow_inf_nan=False, strict=True)
    offset: float | None = Field(default=None, allow_inf_nan=False, strict=True)

    @field_validator("scale")
    @classmethod
    def _scale_not_zero(cls, value: float | None) -> float | None:
        if value == 0:
            raise ValueError("scale must not be 0")
        return value

    def to_options(self) -> dict[str, object]:
        """Return only the set options, with enums as plain strings."""
        return self.model_dump(mode="json", exclude_none=True)


@dataclass(frozen=True, slots=True)
class S7Address:
    """Parsed stored address."""

    area: Area
    db: int | None
    byte: int
    bit: int | None

    def __str__(self) -> str:
        prefix = f"DB{self.db}." if self.area is Area.DB else self.area.name
        suffix = "" if self.bit is None else f".{self.bit}"
        return f"{prefix}{self.byte}{suffix}"


def _address_error(message: str) -> ConfigurationError:
    return ConfigurationError(message, code="s7_invalid_address")


_NUMBER = r"(0|[1-9][0-9]{0,5})"
_CANONICAL_RE = re.compile(
    rf"(?:DB{_NUMBER}\.|([MIQ])){_NUMBER}(?:\.{_NUMBER})?", re.ASCII
)
_STEP7_DB_RE = re.compile(
    rf"%?DB{_NUMBER}\.DB([XBWD]){_NUMBER}(?:\.{_NUMBER})?", re.ASCII
)
_STEP7_AREA_RE = re.compile(rf"%?([MIQEA])([BWD])?{_NUMBER}(?:\.{_NUMBER})?", re.ASCII)
_AREA_LETTERS = {
    "M": Area.M,
    "I": Area.I,
    "E": Area.I,
    "Q": Area.Q,
    "A": Area.Q,
}
_SIZE_BYTES = {"X": 0, "B": 1, "W": 2, "D": 4}
_FORMS = "DB10.4, DB10.4.0, M20, I0.3 or Q8"


def _build(
    area: Area, db: str | None, byte: str, bit: str | None, text: str
) -> S7Address:
    db_number = None if db is None else int(db)
    if db_number is not None and not 1 <= db_number <= _MAX_DB:
        raise _address_error(f"DB number {db_number} in {text!r} is outside 1-65535.")
    if int(byte) >= _ADDRESS_SPACE:
        raise _address_error(f"Byte {byte} in {text!r} is outside 0-65535.")
    bit_number = None if bit is None else int(bit)
    if bit_number is not None and bit_number > _MAX_BIT:
        raise _address_error(f"Bit {bit} in {text!r} is outside 0-7.")
    return S7Address(area, db_number, int(byte), bit_number)


def parse_address(text: str) -> S7Address:
    """Parse the canonical stored form (no ``%``, no size letter)."""
    match = _CANONICAL_RE.fullmatch(text)
    if match is None:
        raise _address_error(f"Invalid S7 address {text!r}; use for example {_FORMS}.")
    db, letter, byte, bit = match.groups()
    area = Area.DB if letter is None else Area(letter.lower())
    return _build(area, db, byte, bit, text)


def parse_step7(text: str) -> tuple[S7Address, int | None]:
    """Parse canonical or STEP7 input.

    Returns the address and the size its letter implies in bytes (``0`` for a
    single bit), or ``None`` when the text names no size.
    """
    stripped = text.strip().upper()
    match = _STEP7_DB_RE.fullmatch(stripped)
    if match is not None:
        db, letter, byte, bit = match.groups()
        return _with_size(_build(Area.DB, db, byte, bit, text), letter, text)
    match = _STEP7_AREA_RE.fullmatch(stripped)
    if match is not None:
        area_letter, letter, byte, bit = match.groups()
        address = _build(_AREA_LETTERS[area_letter], None, byte, bit, text)
        return _with_size(address, letter, text)
    match = _CANONICAL_RE.fullmatch(stripped.removeprefix("%"))
    if match is None:
        raise _address_error(
            f"Invalid S7 address {text!r}; use for example {_FORMS}, "
            "or STEP7 forms like %DB10.DBD4 or %MW20."
        )
    db, letter, byte, bit = match.groups()
    area = Area.DB if letter is None else Area(letter.lower())
    # _build keeps the precise range message (DB number, byte or bit).
    address = _build(area, db, byte, bit, text)
    return address, (0 if address.bit is not None else None)


def _with_size(
    address: S7Address, letter: str | None, text: str
) -> tuple[S7Address, int | None]:
    if letter is None:
        return address, (0 if address.bit is not None else None)
    if (letter == "X") != (address.bit is not None):
        raise _address_error(
            f"Invalid S7 address {text!r}; a bit address (.0-.7) is required for, "
            "and only for, the X size letter."
        )
    return address, _SIZE_BYTES[letter]


def size_letter_error(text: str, size: int | None, data_type: DataType) -> str | None:
    """Return why the address ``text`` does not fit ``data_type``, or None.

    ``size`` is what ``parse_step7`` returned: the bytes its size letter names,
    ``0`` for a bit address, None for a byte address without a letter. For
    ``string`` the letter is not compared (B is how TIA shows strings).
    """
    if data_type is DataType.BOOL:
        if size == 0:
            return None
        if size is None:
            return f"{text} has no bit; bool needs a bit address (.0-.7)."
        return f"{text} is a {size}-byte address; bool needs a bit address (.0-.7)."
    if size == 0:
        return f"{text} is a bit address; {data_type} needs {_need(data_type)}."
    if size is None or data_type is DataType.STRING:
        return None
    span = byte_span(data_type, None)
    if size != span:
        return f"{text} is a {size}-byte address; {data_type} needs {_need(data_type)}."
    return None


def _need(data_type: DataType) -> str:
    if data_type is DataType.STRING:
        return "a byte address"
    span = byte_span(data_type, None)
    return "1 byte" if span == 1 else f"{span} bytes"


def display_address(address: S7Address, data_type: DataType) -> str:
    """STEP7 form of an address for the data type."""
    if address.bit is not None:
        letter = "X"
    else:
        span = _SPANS.get(data_type)
        letter = {2: "W", 4: "D"}.get(span or 0, "B")
    if address.area is Area.DB:
        suffix = "" if address.bit is None else f".{address.bit}"
        return f"DB{address.db}.DB{letter}{address.byte}{suffix}"
    if address.bit is not None:
        return f"{address.area.name}{address.byte}.{address.bit}"
    return f"{address.area.name}{letter}{address.byte}"


@dataclass(frozen=True, slots=True)
class S7TagPlan:
    """A fully resolved tag, ready for block planning and decoding."""

    area: Area
    db: int | None
    byte: int
    bit: int | None
    data_type: DataType
    span: int
    length: int | None
    scale: float | None
    offset: float | None
    value_type: ValueType


def derive_value_type(options: S7TagOptions) -> ValueType:
    """Normalized value type produced by a tag."""
    data_type = options.data_type
    if data_type is DataType.BOOL:
        return ValueType.BOOLEAN
    if data_type in (DataType.CHAR, DataType.STRING):
        return ValueType.TEXT
    if (
        options.scale is not None
        or options.offset is not None
        or data_type in _FLOAT_TYPES
    ):
        return ValueType.NUMERIC
    return ValueType.INTEGER


def _tag_error(address: str, code: str, problem: str) -> ConfigurationError:
    return ConfigurationError(f"Invalid S7 tag {address!r}: {problem}", code=code)


def resolve_tag(address: str, options: Mapping[str, object]) -> S7TagPlan:
    """Validate a tag and resolve it into a plan."""
    try:
        parsed = parse_address(address)
    except ConfigurationError as error:
        raise _tag_error(address, "s7_invalid_address", error.message) from error
    try:
        tag = S7TagOptions.model_validate(dict(options))
    except ValidationError as error:
        raise _tag_error(address, "s7_invalid_options", _readable(error)) from error

    def invalid(problem: str) -> ConfigurationError:
        return _tag_error(address, "s7_invalid_options", problem)

    data_type = tag.data_type
    if (data_type is DataType.BOOL) != (parsed.bit is not None):
        raise _tag_error(
            address,
            "s7_invalid_address",
            "a bit address (.0-.7) is required for, and only for, data_type bool.",
        )
    if data_type is DataType.STRING:
        if tag.length is None:
            raise invalid("length is required for data_type string.")
    elif tag.length is not None:
        raise invalid("length is only valid for data_type string.")
    span = byte_span(data_type, tag.length)
    if parsed.byte + span > _ADDRESS_SPACE:
        raise _tag_error(
            address, "s7_invalid_address", "the value does not fit in bytes 0-65535."
        )
    if (tag.scale is not None or tag.offset is not None) and (
        data_type in _NO_SCALE_TYPES
    ):
        raise invalid(f"scale and offset are not allowed for data_type {data_type}.")
    return S7TagPlan(
        area=parsed.area,
        db=parsed.db,
        byte=parsed.byte,
        bit=parsed.bit,
        data_type=data_type,
        span=span,
        length=tag.length,
        scale=tag.scale,
        offset=tag.offset,
        value_type=derive_value_type(tag),
    )


def _not_finite() -> PermanentProtocolError:
    return PermanentProtocolError(
        "S7 value is not a finite number.", code="s7_value_not_finite"
    )


def _string_invalid(problem: str) -> PermanentProtocolError:
    return PermanentProtocolError(
        f"S7 STRING is invalid: {problem}", code="s7_string_invalid"
    )


def decode(plan: S7TagPlan, data: bytes) -> TagValue:
    """Decode the plan's bytes (for a bool, the byte holding the bit)."""
    if len(data) != plan.span:
        raise PermanentProtocolError(
            f"Expected {plan.span} bytes, got {len(data)}.", code="s7_invalid_tag"
        )
    data_type = plan.data_type
    if data_type is DataType.BOOL:
        if plan.bit is None:
            raise PermanentProtocolError("Bool tag has no bit.", code="s7_invalid_tag")
        return TagValue.boolean(bool((data[0] >> plan.bit) & 1))
    if data_type is DataType.CHAR:
        return TagValue.text(data.decode("latin-1"))
    if data_type is DataType.STRING:
        maximum, actual = data[0], data[1]
        if actual > maximum or actual > len(data) - 2:
            raise _string_invalid(
                f"actual length {actual} exceeds the maximum length "
                f"{min(maximum, len(data) - 2)}."
            )
        return TagValue.text(data[2 : 2 + actual].decode("latin-1"))
    number: int | float = struct.unpack(_STRUCT_FORMATS[data_type], data)[0]
    if data_type in _FLOAT_TYPES and not math.isfinite(number):
        raise _not_finite()
    if plan.scale is None and plan.offset is None:
        if isinstance(number, int):
            if not INT64_MIN <= number <= INT64_MAX:
                raise PermanentProtocolError(
                    f"Value {number} does not fit a 64-bit signed integer; "
                    "set scale to 1 to store it as a float.",
                    code="s7_value_out_of_range",
                )
            return TagValue.integer(number)
        return TagValue.numeric(number)
    scaled = float(number) * (plan.scale or 1) + (plan.offset or 0)
    if not math.isfinite(scaled):
        raise _not_finite()
    return TagValue.numeric(scaled)


class S7Rules:
    """Validation of S7 Connections and Tags."""

    def validate_connection(self, connection: ConnectionConfig) -> None:
        """Check the endpoint and the connection options."""
        parse_endpoint(connection.endpoint, protocol_label="S7")
        S7ConnectionOptions.from_options(connection.protocol_options)

    def validate_tag(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> dict[str, object]:
        """Resolve the tag, check its value type and return normalised options."""
        plan = resolve_tag(tag.address, tag.protocol_options)
        if tag.value_type != plan.value_type:
            raise ConfigurationError(
                f"value_type is {tag.value_type} but {plan.data_type}"
                f"{' with scale' if plan.scale is not None else ''}"
                f"{' with offset' if plan.offset is not None else ''}"
                f" gives {plan.value_type}",
                code="s7_value_type_mismatch",
            )
        # resolve_tag has validated the options, so this cannot fail.
        return S7TagOptions.model_validate(dict(tag.protocol_options)).to_options()
