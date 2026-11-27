"""Wired M-Bus tag model: meter addresses, options, units and rules (no I/O)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagConfig,
    TagRequest,
    ValueType,
)
from plc_data_collector.protocols.endpoints import (
    is_serial_endpoint,
    parse_endpoint,
    parse_serial_endpoint,
)

MBUS_PROTOCOL = "mbus"
MAX_PRIMARY_ADDRESS = 250
_WILDCARD_BYTE = 0xFF
_WILDCARD_MANUFACTURER = 0xFFFF


# Meter addresses


@dataclass(frozen=True, slots=True)
class SecondaryAddress:
    """A secondary address; ``None`` fields and ``F`` ID digits are wildcards."""

    id_digits: str
    manufacturer: int | None
    version: int | None
    medium: int | None

    def pattern(self) -> bytes:
        """The 8-byte selection payload (EN 13757-3), least significant first."""
        id_bytes = bytes.fromhex(self.id_digits)[::-1]
        manufacturer = (
            _WILDCARD_MANUFACTURER if self.manufacturer is None else self.manufacturer
        )
        return (
            id_bytes
            + manufacturer.to_bytes(2, "little")
            + bytes(
                (
                    _WILDCARD_BYTE if self.version is None else self.version,
                    _WILDCARD_BYTE if self.medium is None else self.medium,
                )
            )
        )


@dataclass(frozen=True, slots=True)
class MeterAddress:
    """A meter on the bus: exactly one of a primary or a secondary address."""

    primary: int | None
    secondary: SecondaryAddress | None

    def __post_init__(self) -> None:
        """Require exactly one address form."""
        if (self.primary is None) == (self.secondary is None):
            raise ValueError("a meter has exactly one of primary or secondary")


_FORMS = "5 (primary 0-250), 12345678 or 123456782C2D0104 (secondary)"
_PRIMARY_RE = re.compile(r"[0-9]{1,3}", re.ASCII)
_SECONDARY_RE = re.compile(r"([0-9F]{8})(?:([0-9A-F]{4})([0-9A-F]{2})([0-9A-F]{2}))?")


def _address_error(text: str) -> ConfigurationError:
    return ConfigurationError(
        f"Invalid M-Bus meter address {text!r}; use {_FORMS}.",
        code="mbus_invalid_address",
    )


def _optional_byte(text: str | None, wildcard: int) -> int | None:
    if text is None:
        return None
    value = int(text, 16)
    return None if value == wildcard else value


def parse_meter_address(text: str) -> MeterAddress:
    """Parse a stored or typed meter address (trimmed, case-insensitive)."""
    stripped = text.strip().upper()
    if _PRIMARY_RE.fullmatch(stripped):
        primary = int(stripped)
        if primary > MAX_PRIMARY_ADDRESS:
            raise _address_error(text)
        return MeterAddress(primary=primary, secondary=None)
    match = _SECONDARY_RE.fullmatch(stripped)
    if match is None:
        raise _address_error(text)
    id_digits, manufacturer, version, medium = match.groups()
    secondary = SecondaryAddress(
        id_digits=id_digits,
        manufacturer=_optional_byte(manufacturer, _WILDCARD_MANUFACTURER),
        version=_optional_byte(version, _WILDCARD_BYTE),
        medium=_optional_byte(medium, _WILDCARD_BYTE),
    )
    return MeterAddress(primary=None, secondary=secondary)


# EN 13757-3 Table 3 (device type / medium), short display names.
MEDIUM_NAMES: Mapping[int, str] = MappingProxyType(
    {
        0x00: "other",
        0x01: "oil",
        0x02: "electricity",
        0x03: "gas",
        0x04: "heat",
        0x05: "steam",
        0x06: "warm water",
        0x07: "water",
        0x08: "heat cost allocator",
        0x09: "compressed air",
        0x0A: "cooling (outlet)",
        0x0B: "cooling (inlet)",
        0x0C: "heat (inlet)",
        0x0D: "heat/cooling",
        0x0E: "bus/system",
        0x0F: "unknown",
        0x15: "hot water",
        0x16: "cold water",
        0x17: "dual water",
        0x18: "pressure",
        0x19: "A/D converter",
    }
)


def manufacturer_code(value: int) -> str:
    """Three-letter manufacturer code (EN 13757-3 / FLAG), or hex if invalid."""
    letters = [(value >> shift) & 0x1F for shift in (10, 5, 0)]
    if value > 0x7FFF or not all(1 <= letter <= 26 for letter in letters):
        return f"{value:04X}"
    return "".join(chr(ord("A") - 1 + letter) for letter in letters)


def medium_name(value: int) -> str:
    """Display name of a medium code."""
    return MEDIUM_NAMES.get(value, f"medium {value:02X}")


def canonical_meter_address(address: MeterAddress) -> str:
    """The stored form: ``5``; ``12345678``; or 16 upper-case hex digits.

    A full secondary address whose manufacturer, version and medium are all
    wildcards means the same meter as its 8-digit ID, so it is stored as that.
    """
    if address.secondary is None:
        return str(address.primary)
    secondary = address.secondary
    if (secondary.manufacturer, secondary.version, secondary.medium) == (
        None,
        None,
        None,
    ):
        return secondary.id_digits
    manufacturer = (
        _WILDCARD_MANUFACTURER
        if secondary.manufacturer is None
        else secondary.manufacturer
    )
    version = _WILDCARD_BYTE if secondary.version is None else secondary.version
    medium = _WILDCARD_BYTE if secondary.medium is None else secondary.medium
    return f"{secondary.id_digits}{manufacturer:04X}{version:02X}{medium:02X}"


def display_meter_address(address: MeterAddress) -> str:
    """``5``; ``12345678``; or ``12345678 PDC heat`` when known."""
    if address.secondary is None:
        return str(address.primary)
    secondary = address.secondary
    parts = [secondary.id_digits]
    if secondary.manufacturer is not None:
        parts.append(manufacturer_code(secondary.manufacturer))
    if secondary.medium is not None:
        parts.append(medium_name(secondary.medium))
    return " ".join(parts)


# Quantities, units and qualifiers


class Function(StrEnum):
    """DIF function field."""

    INSTANTANEOUS = "instantaneous"
    MAXIMUM = "maximum"
    MINIMUM = "minimum"
    ERROR = "error"


class Quantity(StrEnum):
    """What a record measures (primary VIF and the listed extensions)."""

    ENERGY = "energy"
    VOLUME = "volume"
    MASS = "mass"
    ON_TIME = "on_time"
    OPERATING_TIME = "operating_time"
    POWER = "power"
    VOLUME_FLOW = "volume_flow"
    MASS_FLOW = "mass_flow"
    FLOW_TEMPERATURE = "flow_temperature"
    RETURN_TEMPERATURE = "return_temperature"
    TEMPERATURE_DIFFERENCE = "temperature_difference"
    EXTERNAL_TEMPERATURE = "external_temperature"
    PRESSURE = "pressure"
    HCA_UNITS = "hca_units"
    VOLTAGE = "voltage"
    CURRENT = "current"


@dataclass(frozen=True, slots=True)
class UnitFamily:
    """Units that convert exactly; a factor is one unit in base units."""

    name: str
    base_unit: str
    factors: Mapping[str, Decimal]


def _family(name: str, base_unit: str, factors: Mapping[str, int | str]) -> UnitFamily:
    return UnitFamily(
        name=name,
        base_unit=base_unit,
        factors=MappingProxyType({unit: Decimal(f) for unit, f in factors.items()}),
    )


_DECIMAL_PREFIXES = {"": 1, "k": 1000, "M": 10**6, "G": 10**9}

UNIT_FAMILIES: Mapping[str, UnitFamily] = MappingProxyType(
    {
        family.name: family
        for family in (
            _family(
                "energy_wh", "Wh", {f"{p}Wh": f for p, f in _DECIMAL_PREFIXES.items()}
            ),
            _family(
                "energy_j", "J", {f"{p}J": f for p, f in _DECIMAL_PREFIXES.items()}
            ),
            _family("volume", "m3", {"m3": 1, "L": "0.001"}),
            _family("mass", "kg", {"kg": 1, "t": 1000}),
            # min/h/d are what meters send; Tags store integer seconds only.
            _family("duration", "s", {"s": 1, "min": 60, "h": 3600, "d": 86400}),
            _family("power_w", "W", {"W": 1, "kW": 1000, "MW": 10**6}),
            _family(
                "power_jh", "J/h", {f"{p}J/h": f for p, f in _DECIMAL_PREFIXES.items()}
            ),
            _family(
                "volume_flow",
                "m3/h",
                {"m3/h": 1, "L/h": "0.001", "m3/min": 60, "m3/s": 3600},
            ),
            _family("mass_flow", "kg/h", {"kg/h": 1, "t/h": 1000}),
            _family("temperature", "C", {"C": 1}),
            _family("temperature_difference", "K", {"K": 1}),
            _family("pressure", "bar", {"bar": 1}),
            # Heat cost allocator units carry no physical unit.
            _family("hca_units", "", {"": 1}),
            _family("voltage", "V", {"V": 1, "kV": 1000}),
            _family("current", "A", {"A": 1}),
        )
    }
)

_QUANTITY_FAMILIES: Mapping[Quantity, tuple[str, ...]] = MappingProxyType(
    {
        Quantity.ENERGY: ("energy_wh", "energy_j"),
        Quantity.VOLUME: ("volume",),
        Quantity.MASS: ("mass",),
        Quantity.ON_TIME: ("duration",),
        Quantity.OPERATING_TIME: ("duration",),
        Quantity.POWER: ("power_w", "power_jh"),
        Quantity.VOLUME_FLOW: ("volume_flow",),
        Quantity.MASS_FLOW: ("mass_flow",),
        Quantity.FLOW_TEMPERATURE: ("temperature",),
        Quantity.RETURN_TEMPERATURE: ("temperature",),
        Quantity.TEMPERATURE_DIFFERENCE: ("temperature_difference",),
        Quantity.EXTERNAL_TEMPERATURE: ("temperature",),
        Quantity.PRESSURE: ("pressure",),
        Quantity.HCA_UNITS: ("hca_units",),
        Quantity.VOLTAGE: ("voltage",),
        Quantity.CURRENT: ("current",),
    }
)
_DURATIONS = frozenset({Quantity.ON_TIME, Quantity.OPERATING_TIME})
_DURATION_UNITS = ("s",)

QUALIFIERS: frozenset[str] = frozenset(
    {"backward", "forward", "per_hour", "per_minute", "per_second"}
)


def units_for(quantity: Quantity) -> tuple[str, ...]:
    """Units a Tag of the quantity may store its value in (empty: no unit)."""
    if quantity in _DURATIONS:
        return _DURATION_UNITS
    return tuple(
        unit
        for name in _QUANTITY_FAMILIES[quantity]
        for unit in UNIT_FAMILIES[name].factors
        if unit
    )


def family_of(quantity: Quantity, unit: str) -> UnitFamily:
    """The quantity's family that contains ``unit`` (Tag or meter unit).

    Raises ``KeyError`` when no family of the quantity knows the unit.
    """
    for name in _QUANTITY_FAMILIES[quantity]:
        family = UNIT_FAMILIES[name]
        if unit in family.factors:
            return family
    raise KeyError(f"{unit!r} is not a unit of {quantity}")


def value_type_of(quantity: Quantity) -> ValueType:
    """Durations are integer seconds; everything else is numeric."""
    return ValueType.INTEGER if quantity in _DURATIONS else ValueType.NUMERIC


# Enough digits for any meter value (at most 20 digits) times any factor.
_CONVERT_PRECISION = 60


def convert(
    value: Decimal, from_unit: str, to_unit: str, family: UnitFamily
) -> Decimal:
    """Convert inside one family, in decimal.

    Exact whenever the result has a finite decimal form (every conversion to a
    larger factor, and to a smaller one by powers of ten); a division by 60 or
    3600 that does not terminate is rounded to 60 significant digits.
    Raises ``KeyError`` for a unit outside the family.
    """
    source = family.factors[from_unit]
    target = family.factors[to_unit]
    if source == target:
        return value
    with localcontext() as context:
        context.prec = _CONVERT_PRECISION
        return value * source / target


# Options


def _readable(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        field = ".".join(str(p) for p in item["loc"]) or "options"
        message = str(item["msg"]).removeprefix("Value error, ")
        parts.append(f"{field}: {message}")
    return "; ".join(parts)


_BAUD_RATES = frozenset({300, 600, 1200, 2400, 4800, 9600, 19200, 38400})


class MBusConnectionOptions(BaseModel):
    """Connection-level M-Bus options (strict numbers, unknown keys refused)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    baud_rate: int = Field(default=2400, strict=True)
    scan_timeout_ms: int = Field(default=500, ge=50, le=5000, strict=True)
    max_telegrams: int = Field(default=8, ge=1, le=16, strict=True)

    @field_validator("baud_rate")
    @classmethod
    def _known_baud_rate(cls, value: int) -> int:
        if value not in _BAUD_RATES:
            allowed = ", ".join(str(rate) for rate in sorted(_BAUD_RATES))
            raise ValueError(f"must be one of {allowed}")
        return value

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> Self:
        """Validate a stored options mapping."""
        try:
            return cls.model_validate(dict(options))
        except ValidationError as error:
            raise ConfigurationError(
                f"Invalid M-Bus connection options: {_readable(error)}",
                code="invalid_mbus_connection",
            ) from error


class MBusTagOptions(BaseModel):
    """Tag-level M-Bus options (strict numbers, unknown keys refused)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quantity: Quantity
    unit: str | None = Field(default=None, strict=True)
    storage: int = Field(default=0, ge=0, le=2**40, strict=True)
    tariff: int = Field(default=0, ge=0, le=2**20, strict=True)
    subunit: int = Field(default=0, ge=0, le=2**10, strict=True)
    function: Function = Function.INSTANTANEOUS
    qualifiers: tuple[str, ...] = ()
    occurrence: int = Field(default=1, ge=1, le=32, strict=True)
    meter_id: str | None = Field(default=None, strict=True, pattern=r"^[0-9]{8}$")

    @field_validator("qualifiers")
    @classmethod
    def _known_sorted_qualifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - QUALIFIERS)
        if unknown:
            allowed = ", ".join(sorted(QUALIFIERS))
            raise ValueError(f"unknown {', '.join(unknown)}; use {allowed}")
        if len(set(value)) != len(value):
            raise ValueError("each qualifier may appear once")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _unit_of_quantity(self) -> Self:
        units = units_for(self.quantity)
        if not units:
            if self.unit is not None:
                raise ValueError(f"{self.quantity} has no unit; remove unit")
        elif self.unit is None:
            raise ValueError(f"unit is required for {self.quantity}")
        elif self.unit not in units:
            raise ValueError(
                f"unit {self.unit!r} is not a unit of {self.quantity}; "
                f"use {', '.join(units)}"
            )
        return self

    def to_options(self) -> dict[str, object]:
        """Return the options without defaults, enums as plain strings."""
        return self.model_dump(mode="json", exclude_defaults=True)


# Resolved Tags


@dataclass(frozen=True, slots=True)
class RecordKey:
    """Which record of a meter's reply a Tag reads (without the occurrence)."""

    quantity: Quantity
    storage: int
    tariff: int
    subunit: int
    function: Function
    qualifiers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MBusTagPlan:
    """A fully resolved Tag, ready for the driver and the codec."""

    meter: MeterAddress
    key: RecordKey
    occurrence: int
    unit: str
    family: UnitFamily
    value_type: ValueType
    meter_id: str | None


def _tag_error(address: str, code: str, problem: str) -> ConfigurationError:
    return ConfigurationError(f"Invalid M-Bus tag {address!r}: {problem}", code=code)


def _parse(tag: TagConfig | TagRequest) -> tuple[MeterAddress, MBusTagOptions]:
    try:
        meter = parse_meter_address(tag.address)
    except ConfigurationError as error:
        raise _tag_error(tag.address, "mbus_invalid_address", error.message) from error
    try:
        options = MBusTagOptions.model_validate(dict(tag.protocol_options))
    except ValidationError as error:
        raise _tag_error(
            tag.address, "mbus_invalid_options", _readable(error)
        ) from error
    return meter, options


def _key(options: MBusTagOptions) -> RecordKey:
    return RecordKey(
        quantity=options.quantity,
        storage=options.storage,
        tariff=options.tariff,
        subunit=options.subunit,
        function=options.function,
        qualifiers=options.qualifiers,
    )


def resolve_tag(
    tag: TagConfig | TagRequest, connection: ConnectionConfig
) -> MBusTagPlan:
    """Validate a Tag (configured, or as the driver gets it) into a plan.

    Quantities without a unit (``hca_units``) resolve to unit ``""``.
    """
    meter, options = _parse(tag)
    unit = options.unit or ""
    return MBusTagPlan(
        meter=meter,
        key=_key(options),
        occurrence=options.occurrence,
        unit=unit,
        family=family_of(options.quantity, unit),
        value_type=value_type_of(options.quantity),
        meter_id=options.meter_id,
    )


def register_key(tag: TagConfig) -> tuple[str, RecordKey, int]:
    """Meter display, record key and occurrence: the duplicate-check key."""
    meter, options = _parse(tag)
    return display_meter_address(meter), _key(options), options.occurrence


# Rules


def endpoint_key(endpoint: str) -> tuple[str, str] | tuple[str, str, int]:
    """Normalised endpoint (lower-case host, device as written) for comparison.

    Raises ``ConfigurationError`` ``invalid_mbus_endpoint`` when invalid.
    """
    try:
        if is_serial_endpoint(endpoint):
            return ("serial", parse_serial_endpoint(endpoint))
        host, port = parse_endpoint(endpoint, protocol_label="M-Bus")
    except ConfigurationError as error:
        raise ConfigurationError(
            f"Invalid M-Bus endpoint {endpoint!r}; use tcp://host:port with a "
            "port from 1 to 65535, or serial://<device> such as "
            "serial:///dev/ttyUSB0 or serial://COM3.",
            code="invalid_mbus_endpoint",
        ) from error
    return ("tcp", host.lower(), port)


class MBusRules:
    """Validation of M-Bus Connections and Tags."""

    def validate_connection(self, connection: ConnectionConfig) -> None:
        """Check the endpoint and the connection options."""
        key = endpoint_key(connection.endpoint)
        MBusConnectionOptions.from_options(connection.protocol_options)
        if key[0] == "tcp" and "baud_rate" in connection.protocol_options:
            raise ConfigurationError(
                "Invalid M-Bus connection options: baud_rate applies to serial "
                "ports only; a TCP gateway sets its own baud rate.",
                code="invalid_mbus_connection",
            )

    def validate_tag(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> dict[str, object]:
        """Resolve the Tag, check its value type and return normalised options."""
        plan = resolve_tag(tag, connection)
        if tag.value_type != plan.value_type:
            raise _tag_error(
                tag.address,
                "mbus_invalid_options",
                f"value_type is {tag.value_type} but {plan.key.quantity} "
                f"gives {plan.value_type}.",
            )
        # resolve_tag has validated the options, so this cannot fail.
        return MBusTagOptions.model_validate(dict(tag.protocol_options)).to_options()
