"""Wired M-Bus frame and record codec (EN 13757-2/-3, read-only subset; no I/O).

Frames: the single character ``E5``, short frames ``10 C A CS 16`` and long
frames ``68 L L 68 C A CI data CS 16``. A frame that fails the start, length,
checksum or stop-byte checks raises ``GarbledFrame`` (distinct from no reply).

RSP_UD variable data (CI ``0x72``, and ``0x76`` with the same layout but
"mode 2" byte order, most significant byte first): a 12-byte fixed header,
then data records (DIF, DIFEs, VIF, VIFEs, data).
Values are exact: the raw integer or BCD digits times a power of ten, as
``Decimal`` (durations as ``int``); no float takes part except a 32-bit real
sent by the meter, which becomes the shortest decimal that reads back as the
same real.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

from plc_data_collector.protocols.mbus_tags import (
    Function,
    Quantity,
    RecordKey,
    UnitFamily,
    family_of,
    manufacturer_code,
    medium_name,
)

ACK = 0xE5
ADDRESS_SECONDARY = 253
ADDRESS_BROADCAST = 255

_SHORT_START = 0x10
_LONG_START = 0x68
_STOP = 0x16
_SHORT_LENGTH = 5
_LONG_OVERHEAD = 6  # 68 L L 68 ... CS 16
_MIN_LONG_L = 3  # C, A, CI
_MAX_LONG_L = 255
_C_SND_NKE = 0x40
_C_REQ_UD2 = 0x5B  # FCV set
_C_FCB = 0x20
_C_SND_UD = 0x53
_CI_SELECT = 0x52
_CI_VARIABLE = 0x72
_CI_VARIABLE_MSB = 0x76
_HEADER_LENGTH = 12
_PATTERN_LENGTH = 8
_MAX_ADDRESS = 255


class GarbledFrame(Exception):
    """Bytes that are not a valid M-Bus frame or telegram."""


# Frames


@dataclass(frozen=True, slots=True)
class Ack:
    """The single character acknowledgement ``E5``."""


@dataclass(frozen=True, slots=True)
class ShortFrame:
    """``10 C A CS 16``."""

    control: int
    address: int


@dataclass(frozen=True, slots=True)
class LongFrame:
    """``68 L L 68 C A CI payload CS 16``."""

    control: int
    address: int
    ci: int
    payload: bytes


type Frame = Ack | ShortFrame | LongFrame


def _check_address(address: int) -> None:
    if not 0 <= address <= _MAX_ADDRESS:
        raise ValueError(f"M-Bus address must be 0-255, got {address}")


def _short_frame(control: int, address: int) -> bytes:
    _check_address(address)
    return bytes((_SHORT_START, control, address, (control + address) & 0xFF, _STOP))


def _long_frame(control: int, address: int, ci: int, payload: bytes) -> bytes:
    body = bytes((control, address, ci)) + payload
    if len(body) > _MAX_LONG_L:
        raise ValueError("M-Bus long frame payload is longer than 252 bytes")
    length = len(body)
    return (
        bytes((_LONG_START, length, length, _LONG_START))
        + body
        + bytes((sum(body) & 0xFF, _STOP))
    )


def snd_nke(address: int) -> bytes:
    """SND_NKE: initialise the meter (resets its FCB and deselects it)."""
    return _short_frame(_C_SND_NKE, address)


def req_ud2(address: int, fcb: bool) -> bytes:
    """REQ_UD2: request class 2 data, with the frame count bit valid."""
    return _short_frame(_C_REQ_UD2 | (_C_FCB if fcb else 0), address)


def select_secondary(pattern: bytes) -> bytes:
    """SND_UD with CI ``0x52`` to the secondary address: select by pattern."""
    if len(pattern) != _PATTERN_LENGTH:
        raise ValueError("a secondary selection pattern has 8 bytes")
    return _long_frame(_C_SND_UD, ADDRESS_SECONDARY, _CI_SELECT, pattern)


class FrameReader:
    """Splits a byte stream into frames; feed bytes, then take frames.

    ``next_frame`` returns ``None`` until a whole frame is buffered. On a bad
    frame it raises ``GarbledFrame`` and drops everything buffered.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        """Append received bytes."""
        self._buffer += data

    def next_frame(self) -> Frame | None:
        """The next complete frame, or ``None`` while incomplete."""
        if not self._buffer:
            return None
        try:
            return self._take()
        except GarbledFrame:
            self._buffer.clear()
            raise

    def _take(self) -> Frame | None:
        buffer = self._buffer
        start = buffer[0]
        if start == ACK:
            del buffer[:1]
            return Ack()
        if start == _SHORT_START:
            if len(buffer) < _SHORT_LENGTH:
                return None
            control, address, checksum, stop = buffer[1:_SHORT_LENGTH]
            _check_end(bytes((control, address)), checksum, stop)
            del buffer[:_SHORT_LENGTH]
            return ShortFrame(control=control, address=address)
        if start != _LONG_START:
            raise GarbledFrame(f"bad start byte 0x{start:02X}")
        if len(buffer) < 4:
            return None
        length = buffer[1]
        if buffer[2] != length or buffer[3] != _LONG_START:
            raise GarbledFrame("bad long frame header")
        if length < _MIN_LONG_L:
            raise GarbledFrame(f"long frame length {length} is too short")
        total = length + _LONG_OVERHEAD
        if len(buffer) < total:
            return None
        body = bytes(buffer[4 : 4 + length])
        _check_end(body, buffer[total - 2], buffer[total - 1])
        del buffer[:total]
        return LongFrame(control=body[0], address=body[1], ci=body[2], payload=body[3:])


def _check_end(body: bytes, checksum: int, stop: int) -> None:
    if sum(body) & 0xFF != checksum:
        raise GarbledFrame("checksum mismatch")
    if stop != _STOP:
        raise GarbledFrame(f"bad stop byte 0x{stop:02X}")


# Fixed header and status


@dataclass(frozen=True, slots=True)
class MeterHeader:
    """RSP_UD fixed header."""

    id_digits: str
    manufacturer: str
    manufacturer_code: int
    version: int
    medium: int
    medium_name: str
    access_no: int
    status: int


_STATUS_APPLICATION = 0x03
_APPLICATION_BUSY = 0x01
_STATUS_POWER_LOW = 0x04
_STATUS_PERMANENT = 0x08
_STATUS_TEMPORARY = 0x10


def status_problems(status: int) -> tuple[str, ...]:
    """Status bits (EN 13757-3 Table 4) that make readings uncertain.

    Application bits ``01`` are "busy"; ``10`` and the reserved ``11`` are
    "error". Power low and the manufacturer bits 5-7 are not problems.
    """
    problems = []
    application = status & _STATUS_APPLICATION
    if application == _APPLICATION_BUSY:
        problems.append("application busy")
    elif application:
        problems.append("application error")
    if status & _STATUS_PERMANENT:
        problems.append("permanent error")
    if status & _STATUS_TEMPORARY:
        problems.append("temporary error")
    return tuple(problems)


def power_low(status: int) -> bool:
    """The status byte's power-low bit."""
    return bool(status & _STATUS_POWER_LOW)


def _header(payload: bytes, msb_first: bool) -> MeterHeader:
    if len(payload) < _HEADER_LENGTH:
        raise GarbledFrame("RSP_UD shorter than its fixed header")
    order: Literal["big", "little"] = "big" if msb_first else "little"
    id_bytes = payload[0:4] if msb_first else payload[3::-1]
    manufacturer = int.from_bytes(payload[4:6], order)
    return MeterHeader(
        id_digits=id_bytes.hex().upper(),
        manufacturer=manufacturer_code(manufacturer),
        manufacturer_code=manufacturer,
        version=payload[6],
        medium=payload[7],
        medium_name=medium_name(payload[7]),
        access_no=payload[8],
        status=payload[9],
    )


# Value information (EN 13757-3 VIF and VIFE code tables): the one place the
# codec keeps them.


@dataclass(frozen=True, slots=True)
class _Meaning:
    """A VIF's quantity, the family unit it counts in and its power of ten."""

    quantity: Quantity
    unit: str
    exponent: int


def _rows(
    first: int, quantity: Quantity, unit: str, exponents: Sequence[int]
) -> dict[int, _Meaning]:
    return {
        first + n: _Meaning(quantity, unit, exponent)
        for n, exponent in enumerate(exponents)
    }


def _decades(lowest: int, count: int) -> range:
    return range(lowest, lowest + count)


_DURATIONS = ("s", "min", "h", "d")

# Primary VIF codes (bit 7 removed).
_PRIMARY: Mapping[int, _Meaning] = MappingProxyType(
    {
        **_rows(0x00, Quantity.ENERGY, "Wh", _decades(-3, 8)),
        **_rows(0x08, Quantity.ENERGY, "J", _decades(0, 8)),
        **_rows(0x10, Quantity.VOLUME, "m3", _decades(-6, 8)),
        **_rows(0x18, Quantity.MASS, "kg", _decades(-3, 8)),
        **{
            0x20 + n: _Meaning(Quantity.ON_TIME, unit, 0)
            for n, unit in enumerate(_DURATIONS)
        },
        **{
            0x24 + n: _Meaning(Quantity.OPERATING_TIME, unit, 0)
            for n, unit in enumerate(_DURATIONS)
        },
        **_rows(0x28, Quantity.POWER, "W", _decades(-3, 8)),
        **_rows(0x30, Quantity.POWER, "J/h", _decades(0, 8)),
        **_rows(0x38, Quantity.VOLUME_FLOW, "m3/h", _decades(-6, 8)),
        **_rows(0x40, Quantity.VOLUME_FLOW, "m3/min", _decades(-7, 8)),
        **_rows(0x48, Quantity.VOLUME_FLOW, "m3/s", _decades(-9, 8)),
        **_rows(0x50, Quantity.MASS_FLOW, "kg/h", _decades(-3, 8)),
        **_rows(0x58, Quantity.FLOW_TEMPERATURE, "C", _decades(-3, 4)),
        **_rows(0x5C, Quantity.RETURN_TEMPERATURE, "C", _decades(-3, 4)),
        **_rows(0x60, Quantity.TEMPERATURE_DIFFERENCE, "K", _decades(-3, 4)),
        **_rows(0x64, Quantity.EXTERNAL_TEMPERATURE, "C", _decades(-3, 4)),
        **_rows(0x68, Quantity.PRESSURE, "bar", _decades(-3, 4)),
        0x6E: _Meaning(Quantity.HCA_UNITS, "", 0),
    }
)
# Codes after VIF 0xFB (alternate extension): MWh and GJ, 10^(n-1) MWh / GJ.
_FB: Mapping[int, _Meaning] = MappingProxyType(
    {
        **_rows(0x00, Quantity.ENERGY, "Wh", (5, 6)),
        **_rows(0x08, Quantity.ENERGY, "J", (8, 9)),
    }
)
# Codes after VIF 0xFD (main extension): volts 10^(nnnn-9), amperes
# 10^(nnnn-12).
_FD: Mapping[int, _Meaning] = MappingProxyType(
    {
        **_rows(0x40, Quantity.VOLTAGE, "V", _decades(-9, 16)),
        **_rows(0x50, Quantity.CURRENT, "A", _decades(-12, 16)),
    }
)
# Names of common primary VIFs the codec does not decode (for descriptions).
_PRIMARY_NAMES: Mapping[int, str] = MappingProxyType(
    {
        0x6C: "date",
        0x6D: "date and time",
        **{0x70 + n: "averaging duration" for n in range(4)},
        **{0x74 + n: "actuality duration" for n in range(4)},
        0x78: "fabrication number",
        0x79: "enhanced identification",
        0x7A: "bus address",
        0x7C: "plain-text unit",
        0x7E: "any VIF",
        0x7F: "manufacturer specific",
    }
)
_FD_NAMES: Mapping[int, str] = MappingProxyType(
    {
        0x08: "access number",
        0x09: "medium",
        0x0A: "manufacturer",
        0x0B: "parameter set identification",
        0x0C: "model/version",
        0x0D: "hardware version",
        0x0E: "firmware version",
        0x0F: "software version",
        0x16: "password",
        0x17: "error flags",
        0x1A: "digital output",
        0x1B: "digital input",
        0x3A: "dimensionless",
    }
)
_DATE_VIFS = frozenset({0x6C, 0x6D})
# Combinable (orthogonal) VIFEs: qualifiers, and the multiplicative correction
# factors 10^(nnn-6) (E111 0nnn) and 10^3 (E111 1101). Every other VIFE,
# including the additive correction and "future value", is unsupported.
_QUALIFIER_VIFES: Mapping[int, str] = MappingProxyType(
    {
        0x20: "per_second",
        0x21: "per_minute",
        0x22: "per_hour",
        0x3B: "forward",
        0x3C: "backward",
    }
)
_MULTIPLIER_VIFES: Mapping[int, int] = MappingProxyType(
    {**{0x70 + n: n - 6 for n in range(8)}, 0x7D: 3}
)
_VIF_FB = 0xFB
_VIF_FD = 0xFD
_PLAIN_TEXT = 0x7C
_EXTENSION = 0x80
_LOW_SEVEN = 0x7F


# Records


ValueErrorCode = Literal["mbus_value_invalid", "mbus_value_not_finite"]


@dataclass(frozen=True, slots=True)
class MBusRecord:
    """One data record.

    ``key`` is ``None`` when the record is unsupported (``unsupported_reason``
    says why). A value error keeps the key, unit and family and sets
    ``exact_value=None``, ``value_error`` and ``unsupported_reason``.
    ``unit`` is the meter's natural unit (R8) and ``exact_value`` is in it:
    ``Decimal``, or ``int`` for durations.
    """

    key: RecordKey | None
    unit: str | None
    family: UnitFamily | None
    exact_value: Decimal | int | None
    unsupported_reason: str | None
    raw: bytes
    description: str
    value_error: ValueErrorCode | None = None


@dataclass(frozen=True, slots=True)
class Telegram:
    """A decoded RSP_UD."""

    header: MeterHeader
    records: tuple[MBusRecord, ...]
    more_follows: bool


def parse_rsp_ud(frame: LongFrame) -> Telegram:
    """Decode a variable-data RSP_UD (CI ``0x72``, or ``0x76`` MSB first).

    Raises ``GarbledFrame`` for another CI, a short header or a record that
    runs past the end of the telegram.
    """
    if frame.ci not in (_CI_VARIABLE, _CI_VARIABLE_MSB):
        raise GarbledFrame(f"CI 0x{frame.ci:02X} is not a variable-data RSP_UD")
    msb_first = frame.ci == _CI_VARIABLE_MSB
    header = _header(frame.payload, msb_first)
    records, more_follows = _records(frame.payload[_HEADER_LENGTH:], msb_first)
    return Telegram(header=header, records=records, more_follows=more_follows)


_FILL = 0x2F
_MORE_FOLLOWS = 0x1F
_SPECIAL = 0x0F
_DATA_FIELD = 0x0F
_MAX_EXTENSIONS = 10
_NO_DATA = 0x0
_REAL = 0x5
_SELECTION = 0x8
_VARIABLE = 0xD
_INTEGER_LENGTHS: Mapping[int, int] = MappingProxyType(
    {0x1: 1, 0x2: 2, 0x3: 3, 0x4: 4, 0x6: 6, 0x7: 8}
)
_BCD_LENGTHS: Mapping[int, int] = MappingProxyType(
    {0x9: 1, 0xA: 2, 0xB: 3, 0xC: 4, 0xE: 6}
)
_FUNCTIONS = (
    Function.INSTANTANEOUS,
    Function.MAXIMUM,
    Function.MINIMUM,
    Function.ERROR,
)


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def more(self) -> bool:
        return self.pos < len(self.data)

    def take(self, count: int = 1) -> bytes:
        end = self.pos + count
        if end > len(self.data):
            raise GarbledFrame("data record runs past the end of the telegram")
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def byte(self) -> int:
        return self.take()[0]

    def extensions(self, more: bool) -> list[int]:
        found: list[int] = []
        while more:
            if len(found) == _MAX_EXTENSIONS:
                raise GarbledFrame("more than 10 extension bytes")
            value = self.byte()
            found.append(value)
            more = bool(value & _EXTENSION)
        return found


@dataclass(frozen=True, slots=True)
class _Location:
    storage: int
    tariff: int
    subunit: int
    function: Function

    def text(self) -> str:
        return (
            f"storage {self.storage}, tariff {self.tariff}, "
            f"subunit {self.subunit}, {self.function}"
        )


def _records(data: bytes, msb_first: bool) -> tuple[tuple[MBusRecord, ...], bool]:
    cursor = _Cursor(data)
    records: list[MBusRecord] = []
    while cursor.more():
        start = cursor.pos
        dif = cursor.byte()
        if dif == _FILL:
            continue
        if dif & _DATA_FIELD == _SPECIAL:
            # Manufacturer data (0x0F, 0x1F) or reserved: the records end.
            return tuple(records), dif == _MORE_FOLLOWS
        location = _location(dif, cursor.extensions(bool(dif & _EXTENSION)))
        vif = cursor.byte()
        if vif & _LOW_SEVEN == _PLAIN_TEXT:
            cursor.take(cursor.byte())
        vifes = cursor.extensions(bool(vif & _EXTENSION))
        data_field = dif & _DATA_FIELD
        length = _data_length(data_field, cursor)
        if length is None:
            raw = data[start:]
            records.append(
                _unsupported(
                    raw,
                    vif,
                    vifes,
                    location,
                    "variable-length data with a reserved LVAR",
                )
            )
            return tuple(records), False
        value = cursor.take(length)
        raw = data[start : cursor.pos]
        if msb_first:
            value = value[::-1]
        records.append(_record(raw, data_field, location, vif, vifes, value))
    return tuple(records), False


def _location(dif: int, difes: Sequence[int]) -> _Location:
    storage = (dif >> 6) & 1
    tariff = 0
    subunit = 0
    for index, dife in enumerate(difes):
        storage |= (dife & 0x0F) << (1 + 4 * index)
        tariff |= ((dife >> 4) & 0x03) << (2 * index)
        subunit |= ((dife >> 6) & 0x01) << index
    return _Location(storage, tariff, subunit, _FUNCTIONS[(dif >> 4) & 0x03])


def _data_length(data_field: int, cursor: _Cursor) -> int | None:
    """Bytes of data; ``None`` when a variable length cannot be known."""
    if data_field in (_NO_DATA, _SELECTION):
        return 0
    if data_field == _REAL:
        return 4
    if data_field in _INTEGER_LENGTHS:
        return _INTEGER_LENGTHS[data_field]
    if data_field in _BCD_LENGTHS:
        return _BCD_LENGTHS[data_field]
    lvar = cursor.byte()  # _VARIABLE
    if lvar <= 0xBF:
        return lvar  # text
    if lvar <= 0xEF:
        return lvar & 0x0F  # positive/negative BCD or binary
    if lvar <= 0xFA:
        return 4 * (lvar - 0xEC)
    return None


def _vif_text(vif: int, vifes: Sequence[int]) -> str:
    return "VIF " + " ".join(f"{byte:02X}" for byte in (vif, *vifes))


def _vif_name(vif: int, vifes: Sequence[int]) -> str:
    name: str | None
    if vif == _VIF_FD and vifes:
        name = _FD_NAMES.get(vifes[0] & _LOW_SEVEN)
    elif vif in (_VIF_FB, _VIF_FD):
        name = None
    else:
        name = _PRIMARY_NAMES.get(vif & _LOW_SEVEN)
    text = _vif_text(vif, vifes)
    return f"{name} ({text})" if name else text


def _unsupported(
    raw: bytes, vif: int, vifes: Sequence[int], location: _Location, reason: str
) -> MBusRecord:
    return MBusRecord(
        key=None,
        unit=None,
        family=None,
        exact_value=None,
        unsupported_reason=reason,
        raw=raw,
        description=f"{_vif_name(vif, vifes)}; {location.text()}",
    )


def _meaning(vif: int, vifes: Sequence[int]) -> tuple[_Meaning | None, int]:
    """The VIF's meaning and how many VIFEs it used."""
    if vif in (_VIF_FB, _VIF_FD):
        if not vifes:
            return None, 0
        table = _FB if vif == _VIF_FB else _FD
        return table.get(vifes[0] & _LOW_SEVEN), 1
    return _PRIMARY.get(vif & _LOW_SEVEN), 0


def _record(
    raw: bytes,
    data_field: int,
    location: _Location,
    vif: int,
    vifes: Sequence[int],
    value: bytes,
) -> MBusRecord:
    meaning, used = _meaning(vif, vifes)
    if meaning is None:
        if vif not in (_VIF_FB, _VIF_FD) and vif & _LOW_SEVEN in _DATE_VIFS:
            reason = "date/time records are not supported"
        else:
            reason = f"{_vif_name(vif, vifes)} is not a supported quantity"
        return _unsupported(raw, vif, vifes, location, reason)
    exponent = meaning.exponent
    qualifiers: list[str] = []
    for vife in vifes[used:]:
        code = vife & _LOW_SEVEN
        if code in _QUALIFIER_VIFES:
            qualifiers.append(_QUALIFIER_VIFES[code])
        elif code in _MULTIPLIER_VIFES:
            exponent += _MULTIPLIER_VIFES[code]
        else:
            reason = f"VIFE 0x{code:02X} is not supported"
            return _unsupported(raw, vif, vifes, location, reason)
    encoding = _ENCODING_PROBLEMS.get(data_field)
    if encoding is not None:
        return _unsupported(raw, vif, vifes, location, encoding)
    key = RecordKey(
        quantity=meaning.quantity,
        storage=location.storage,
        tariff=location.tariff,
        subunit=location.subunit,
        function=location.function,
        qualifiers=tuple(sorted(qualifiers)),
    )
    family = family_of(meaning.quantity, meaning.unit)
    unit, shift = _natural_unit(family, meaning.unit, exponent)
    description = _description(key, unit)
    decoded = _decode(data_field, value)
    if isinstance(decoded, _Bad):
        return MBusRecord(
            key=key,
            unit=unit,
            family=family,
            exact_value=None,
            unsupported_reason=decoded.reason,
            raw=raw,
            description=description,
            value_error=decoded.code,
        )
    scaled = _scale(decoded, shift)
    exact: Decimal | int = scaled
    if family.base_unit == "s":
        if scaled != scaled.to_integral_value():
            reason = "durations with a fraction are not supported"
            return _unsupported(raw, vif, vifes, location, reason)
        exact = int(scaled)
    return MBusRecord(
        key=key,
        unit=unit,
        family=family,
        exact_value=exact,
        unsupported_reason=None,
        raw=raw,
        description=description,
    )


_ENCODING_PROBLEMS: Mapping[int, str] = MappingProxyType(
    {
        _NO_DATA: "data field 'no data' is not supported",
        _SELECTION: "data field 'selection for readout' is not supported",
        _VARIABLE: "variable-length data is not supported",
    }
)


def _description(key: RecordKey, unit: str) -> str:
    parts = [f"{key.quantity} in {unit}" if unit else str(key.quantity)]
    parts.append(f"storage {key.storage}, tariff {key.tariff}")
    parts.append(f"subunit {key.subunit}, {key.function}")
    parts.extend(key.qualifiers)
    return ", ".join(parts)


def _natural_unit(family: UnitFamily, vif_unit: str, exponent: int) -> tuple[str, int]:
    """The meter's natural unit and the power of ten that scales into it.

    Candidates are the family units that differ from the VIF's unit by a power
    of ten; the largest whose factor is at most the VIF's multiplier wins,
    else the smallest candidate (R8).
    """
    base = family.factors[vif_unit]
    multiplier = base.scaleb(exponent)
    candidates: list[tuple[Decimal, str, int]] = []
    for unit, factor in family.factors.items():
        for power in range(-12, 13):
            if factor == base.scaleb(power):
                candidates.append((factor, unit, power))
    fitting = [c for c in candidates if c[0] <= multiplier]
    _, unit, power = max(fitting) if fitting else min(candidates)
    return unit, exponent - power


def _scale(value: Decimal, shift: int) -> Decimal:
    """``value`` times ``10**shift``, exactly; whole numbers without exponent."""
    sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)  # _decode returns finite values only
    scaled = Decimal((sign, digits, exponent + shift))
    return Decimal(int(scaled)) if exponent + shift > 0 else scaled


# Data fields


@dataclass(frozen=True, slots=True)
class _Bad:
    code: ValueErrorCode
    reason: str


def _decode(data_field: int, data: bytes) -> Decimal | _Bad:
    if data_field in _INTEGER_LENGTHS:
        return Decimal(int.from_bytes(data, "little", signed=True))
    if data_field == _REAL:
        return _real(data)
    return _bcd(data)


def _real(data: bytes) -> Decimal | _Bad:
    (number,) = struct.unpack("<f", data)
    if not math.isfinite(number):
        return _Bad("mbus_value_not_finite", f"the meter sent {number} as a real")
    for digits in range(1, 10):
        text = f"{number:.{digits}g}"
        if struct.pack("<f", float(text)) == data:
            return Decimal(text)
    return Decimal(repr(number))  # unreachable: 9 digits always round-trip


def _bcd(data: bytes) -> Decimal | _Bad:
    nibbles = [n for byte in reversed(data) for n in (byte >> 4, byte & 0x0F)]
    negative = nibbles[0] == 0x0F
    if negative:
        nibbles[0] = 0
    if any(n > 9 for n in nibbles):
        return _Bad(
            "mbus_value_invalid",
            f"BCD value {data[::-1].hex().upper()} has a digit outside 0-9",
        )
    value = int("".join(str(n) for n in nibbles))
    return Decimal(-value if negative else value)
