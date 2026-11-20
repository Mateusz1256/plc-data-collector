"""Modbus tag model, address numbering and register decoding (no I/O)."""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    PermanentProtocolError,
    TagConfig,
    TagValue,
    ValueType,
)

MODBUS_PROTOCOL = "modbus"
MAX_REGISTERS = 125
MAX_BITS = 2000
_ADDRESS_SPACE = 65536
# Integer values are stored in a signed 64-bit column.
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class Area(StrEnum):
    """Modbus data area."""

    COILS = "co"
    DISCRETE_INPUTS = "di"
    HOLDING_REGISTERS = "hr"
    INPUT_REGISTERS = "ir"

    @property
    def function_code(self) -> int:
        """Read function code of the area."""
        return _AREA_INFO[self][0]

    @property
    def is_bit_area(self) -> bool:
        """Whether the area holds single bits."""
        return self in (Area.COILS, Area.DISCRETE_INPUTS)

    @property
    def label(self) -> str:
        """Human-readable name with function code."""
        return _AREA_INFO[self][1]

    @property
    def modicon_digit(self) -> str:
        """Leading digit of Modicon-style addresses."""
        return _AREA_INFO[self][2]


_AREA_INFO: dict[Area, tuple[int, str, str]] = {
    Area.COILS: (1, "Coils (FC01)", "0"),
    Area.DISCRETE_INPUTS: (2, "Discrete inputs (FC02)", "1"),
    Area.HOLDING_REGISTERS: (3, "Holding registers (FC03)", "4"),
    Area.INPUT_REGISTERS: (4, "Input registers (FC04)", "3"),
}
_AREA_NOUN = {
    Area.COILS: "a coil",
    Area.DISCRETE_INPUTS: "a discrete input",
    Area.HOLDING_REGISTERS: "a holding register",
    Area.INPUT_REGISTERS: "an input register",
}


class DataType(StrEnum):
    """Tag data type."""

    BOOL = "bool"
    BIT = "bit"
    INT16 = "int16"
    UINT16 = "uint16"
    INT32 = "int32"
    UINT32 = "uint32"
    INT64 = "int64"
    UINT64 = "uint64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    STRING = "string"


class ByteOrder(StrEnum):
    """Byte order inside a register."""

    AB = "ab"
    BA = "ba"


class WordOrder(StrEnum):
    """Word order inside multi-register values."""

    HI = "hi"
    LO = "lo"


class AddressNumbering(StrEnum):
    """How addresses are shown and typed in the Panel."""

    ZERO = "zero"
    ONE = "one"
    MODICON = "modicon"


_SPANS = {
    DataType.BOOL: 1,
    DataType.BIT: 1,
    DataType.INT16: 1,
    DataType.UINT16: 1,
    DataType.INT32: 2,
    DataType.UINT32: 2,
    DataType.FLOAT32: 2,
    DataType.INT64: 4,
    DataType.UINT64: 4,
    DataType.FLOAT64: 4,
}
_STRUCT_FORMATS = {
    DataType.INT16: ">h",
    DataType.UINT16: ">H",
    DataType.INT32: ">i",
    DataType.UINT32: ">I",
    DataType.INT64: ">q",
    DataType.UINT64: ">Q",
    DataType.FLOAT32: ">f",
    DataType.FLOAT64: ">d",
}
_FLOAT_TYPES = (DataType.FLOAT32, DataType.FLOAT64)


def _readable(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        field = ".".join(str(p) for p in item["loc"]) or "options"
        message = str(item["msg"]).removeprefix("Value error, ")
        parts.append(f"{field}: {message}")
    return "; ".join(parts)


class ModbusConnectionOptions(BaseModel):
    """Connection-level Modbus options."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    unit_id: int = Field(default=1, ge=0, le=255)
    byte_order: ByteOrder = ByteOrder.AB
    word_order: WordOrder = WordOrder.HI
    address_numbering: AddressNumbering = AddressNumbering.ZERO
    max_registers_per_read: int = Field(default=MAX_REGISTERS, ge=1, le=MAX_REGISTERS)
    max_bits_per_read: int = Field(default=MAX_BITS, ge=1, le=MAX_BITS)
    max_gap: int = Field(default=0, ge=0, le=MAX_REGISTERS)

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> Self:
        """Validate a stored options mapping."""
        try:
            return cls.model_validate(dict(options))
        except ValidationError as error:
            raise ConfigurationError(
                f"Invalid Modbus connection options: {_readable(error)}",
                code="invalid_modbus_connection",
            ) from error


class ModbusTagOptions(BaseModel):
    """Tag-level Modbus options."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_type: DataType
    length: int | None = Field(default=None, ge=1, le=MAX_REGISTERS)
    byte_order: ByteOrder | None = None
    word_order: WordOrder | None = None
    unit_id: int | None = Field(default=None, ge=0, le=255)
    scale: float | None = Field(default=None, allow_inf_nan=False)
    offset: float | None = Field(default=None, allow_inf_nan=False)

    @field_validator("scale")
    @classmethod
    def _scale_not_zero(cls, value: float | None) -> float | None:
        if value == 0:
            raise ValueError("scale must not be 0")
        return value

    def to_options(self) -> dict[str, object]:
        """Return only the set options, with enums as plain strings."""
        return self.model_dump(mode="json", exclude_none=True)


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    """Split ``tcp://host:port`` into host and port."""
    try:
        parts = urlsplit(endpoint)
        port = parts.port
        host = parts.hostname
    except ValueError as error:
        raise _endpoint_error(endpoint) from error
    if (
        parts.scheme != "tcp"
        or "@" in parts.netloc
        or parts.path
        or parts.query
        or parts.fragment
        or "?" in endpoint
        or "#" in endpoint
        or not host
        or port is None
        or not 1 <= port <= 65535
    ):
        raise _endpoint_error(endpoint)
    return host, port


def _endpoint_error(endpoint: str) -> ConfigurationError:
    return ConfigurationError(
        f"Invalid Modbus endpoint {endpoint!r}; "
        "use tcp://host:port with a port from 1 to 65535.",
        code="invalid_modbus_endpoint",
    )


def format_endpoint(host: str, port: int) -> str:
    """Build ``tcp://host:port``, bracketing IPv6 hosts."""
    shown = f"[{host}]" if ":" in host else host
    return f"tcp://{shown}:{port}"


@dataclass(frozen=True, slots=True)
class ModbusAddress:
    """Parsed stored (zero-based) address."""

    area: Area
    address: int
    bit: int | None = None

    def __str__(self) -> str:
        suffix = "" if self.bit is None else f".{self.bit}"
        return f"{self.area.value}:{self.address}{suffix}"


_ADDRESS_RE = re.compile(
    r"(co|di|hr|ir):(0|[1-9][0-9]{0,4})(?:\.(0|[1-9][0-9]?))?", re.ASCII
)


def _address_error(message: str) -> ConfigurationError:
    return ConfigurationError(message, code="invalid_modbus_address")


def parse_address(text: str) -> ModbusAddress:
    """Parse ``area:address[.bit]`` (zero-based)."""
    match = _ADDRESS_RE.fullmatch(text)
    if match is None:
        raise _address_error(
            f"Invalid Modbus address {text!r}; "
            "use area:address, for example hr:120 or hr:100.3."
        )
    area = Area(match.group(1))
    address = int(match.group(2))
    bit = None if match.group(3) is None else int(match.group(3))
    if address >= _ADDRESS_SPACE:
        raise _address_error(f"Address {address} is outside 0-65535.")
    if bit is not None and (area.is_bit_area or bit > 15):
        raise _address_error(
            f"Invalid bit in {text!r}; bits 0-15 exist only in HR and IR."
        )
    return ModbusAddress(area, address, bit)


def display_address(area: Area, address: int, numbering: AddressNumbering) -> str:
    """Format a zero-based address for display."""
    if numbering is AddressNumbering.ZERO:
        return str(address)
    one_based = address + 1
    if numbering is AddressNumbering.ONE:
        return str(one_based)
    width = 4 if one_based <= 9999 else 5
    return f"{area.modicon_digit}{one_based:0{width}d}"


def parse_display_address(area: Area, text: str, numbering: AddressNumbering) -> int:
    """Convert a displayed address back to the zero-based address."""
    stripped = text.strip()
    if not re.fullmatch(r"[0-9]+", stripped, re.ASCII):
        raise _address_error(f"Address {text!r} must be a number.")
    if len(stripped) > 6:
        raise _address_error(
            f"Address {stripped[:12]}... is outside the Modbus address space."
        )
    if numbering is AddressNumbering.MODICON:
        if len(stripped) not in (5, 6):
            raise _address_error(
                f"Modicon address {stripped} must have 5 or 6 digits, for example "
                f"{area.modicon_digit}0001."
            )
        digit = stripped[0]
        if digit != area.modicon_digit:
            other = next((a for a in Area if a.modicon_digit == digit), None)
            if other is None:
                raise _address_error(
                    f"Modicon address {stripped} must start with 0, 1, 3 or 4."
                )
            raise _address_error(
                f"Modicon address {stripped} is {_AREA_NOUN[other]}; "
                f"choose {other.label} or use {area.modicon_digit}xxxx."
            )
        number = int(stripped[1:])
    else:
        number = int(stripped)
    if numbering is AddressNumbering.ZERO:
        result = number
    else:
        if number < 1:
            raise _address_error("Modicon and one-based addresses start at 1.")
        result = number - 1
    if not 0 <= result < _ADDRESS_SPACE:
        raise _address_error(f"Address {text!r} is outside the Modbus address space.")
    return result


@dataclass(frozen=True, slots=True)
class ModbusTagPlan:
    """A fully resolved tag, ready for block planning and decoding."""

    area: Area
    address: int
    bit: int | None
    data_type: DataType
    span: int
    unit_id: int
    byte_order: ByteOrder
    word_order: WordOrder
    length: int | None
    scale: float | None
    offset: float | None
    value_type: ValueType


def derive_value_type(options: ModbusTagOptions) -> ValueType:
    """Normalized value type produced by a tag."""
    data_type = options.data_type
    if data_type in (DataType.BOOL, DataType.BIT):
        return ValueType.BOOLEAN
    if data_type is DataType.STRING:
        return ValueType.TEXT
    if (
        options.scale is not None
        or options.offset is not None
        or data_type in _FLOAT_TYPES
    ):
        return ValueType.NUMERIC
    return ValueType.INTEGER


def _tag_error(address: str, problem: str) -> ConfigurationError:
    return ConfigurationError(
        f"Invalid Modbus tag {address!r}: {problem}", code="invalid_modbus_tag"
    )


def resolve_tag(
    address: str, options: Mapping[str, object], connection: ModbusConnectionOptions
) -> ModbusTagPlan:
    """Validate a tag and resolve it into a plan."""
    try:
        parsed = parse_address(address)
    except ConfigurationError as error:
        raise _tag_error(address, error.message) from error
    try:
        tag = ModbusTagOptions.model_validate(dict(options))
    except ValidationError as error:
        raise _tag_error(address, _readable(error)) from error

    data_type = tag.data_type
    if parsed.area.is_bit_area:
        if data_type is not DataType.BOOL:
            raise _tag_error(
                address, f"{parsed.area.label} supports only data_type bool."
            )
    elif data_type is DataType.BOOL:
        raise _tag_error(
            address, "data_type bool is only valid for coils and discrete inputs."
        )
    if (data_type is DataType.BIT) != (parsed.bit is not None):
        raise _tag_error(
            address,
            "a bit address (.0-.15) is required for, and only for, data_type bit.",
        )
    if data_type is DataType.STRING:
        if tag.length is None:
            raise _tag_error(address, "length is required for data_type string.")
        span = tag.length
    else:
        if tag.length is not None:
            raise _tag_error(address, "length is only valid for data_type string.")
        span = _SPANS[data_type]
    if parsed.address + span > _ADDRESS_SPACE:
        raise _tag_error(address, "the value does not fit in the 65536-address space.")
    if (tag.scale is not None or tag.offset is not None) and data_type in (
        DataType.BOOL,
        DataType.BIT,
        DataType.STRING,
    ):
        raise _tag_error(
            address, f"scale and offset are not allowed for data_type {data_type}."
        )

    return ModbusTagPlan(
        area=parsed.area,
        address=parsed.address,
        bit=parsed.bit,
        data_type=data_type,
        span=span,
        unit_id=connection.unit_id if tag.unit_id is None else tag.unit_id,
        byte_order=tag.byte_order or connection.byte_order,
        word_order=tag.word_order or connection.word_order,
        length=tag.length,
        scale=tag.scale,
        offset=tag.offset,
        value_type=derive_value_type(tag),
    )


def _not_finite() -> PermanentProtocolError:
    return PermanentProtocolError(
        "Modbus value is not a finite number.", code="modbus_value_not_finite"
    )


def _register_bytes(
    plan: ModbusTagPlan,
    registers: Sequence[int],
    *,
    words: bool,
    span: int | None = None,
) -> bytes:
    span = plan.span if span is None else span
    if len(registers) < span:
        raise PermanentProtocolError(
            f"Expected {span} registers, got {len(registers)}.",
            code="modbus_short_read",
        )
    values = [int(r) & 0xFFFF for r in registers[:span]]
    if words and plan.word_order is WordOrder.LO:
        values.reverse()
    if plan.byte_order is ByteOrder.BA:
        values = [((v & 0xFF) << 8) | (v >> 8) for v in values]
    return b"".join(v.to_bytes(2, "big") for v in values)


def decode_registers(plan: ModbusTagPlan, registers: Sequence[int]) -> TagValue:
    """Decode register contents according to the plan."""
    data_type = plan.data_type
    if data_type is DataType.BIT:
        if plan.bit is None or not registers:
            raise PermanentProtocolError(
                "Missing register for bit tag.", code="modbus_short_read"
            )
        raw = _register_bytes(plan, registers[:1], words=False, span=1)
        return TagValue.boolean(bool((int.from_bytes(raw, "big") >> plan.bit) & 1))
    if data_type is DataType.STRING:
        raw = _register_bytes(plan, registers, words=False)
        raw = raw.split(b"\x00", 1)[0].rstrip(b" ")
        try:
            return TagValue.text(raw.decode("ascii"))
        except UnicodeDecodeError as error:
            raise PermanentProtocolError(
                "Modbus string contains non-ASCII bytes.",
                code="modbus_string_not_ascii",
            ) from error
    number: int | float = struct.unpack(
        _STRUCT_FORMATS[data_type], _register_bytes(plan, registers, words=True)
    )[0]
    if data_type in _FLOAT_TYPES and not math.isfinite(number):
        raise _not_finite()
    if plan.scale is None and plan.offset is None:
        if isinstance(number, int):
            if not INT64_MIN <= number <= INT64_MAX:
                raise PermanentProtocolError(
                    f"Value {number} does not fit a 64-bit signed integer; "
                    "set scale to 1 to store it as a float.",
                    code="modbus_value_out_of_range",
                )
            return TagValue.integer(number)
        return TagValue.numeric(number)
    scaled = float(number) * (plan.scale or 1) + (plan.offset or 0)
    if not math.isfinite(scaled):
        raise _not_finite()
    return TagValue.numeric(scaled)


def decode_bits(plan: ModbusTagPlan, bits: Sequence[bool]) -> TagValue:
    """Decode a coil or discrete input."""
    if not bits:
        raise PermanentProtocolError("Missing bit value.", code="modbus_short_read")
    return TagValue.boolean(bool(bits[0]))


class ModbusRules:
    """Validation of Modbus Connections and Tags."""

    def validate_connection(self, connection: ConnectionConfig) -> None:
        """Check the endpoint and the connection options."""
        parse_endpoint(connection.endpoint)
        ModbusConnectionOptions.from_options(connection.protocol_options)

    def validate_tag(self, tag: TagConfig, connection: ConnectionConfig) -> None:
        """Resolve the tag and check its value type matches the derived one."""
        options = ModbusConnectionOptions.from_options(connection.protocol_options)
        plan = resolve_tag(tag.address, tag.protocol_options, options)
        if tag.value_type != plan.value_type:
            raise ConfigurationError(
                f"value_type is {tag.value_type} but {plan.data_type}"
                f"{' with scale' if plan.scale is not None else ''}"
                f"{' with offset' if plan.offset is not None else ''}"
                f" gives {plan.value_type}",
                code="modbus_value_type_mismatch",
            )
