"""Wired M-Bus simulator: five meters on one bus, for demos and tests.

Run it with ``python -m plc_data_collector.simulators.mbus`` (TCP, default
port 10001, like an M-Bus/TCP gateway). ``SimulatedBus.handle`` answers one
request frame the way the meters on a real bus would (EN 13757-2 link layer,
EN 13757-3 variable-data RSP_UD), so it is transport-agnostic; ``serve_tcp``
and ``serve_serial`` (a pty master, POSIX only) put it on a wire.

Link layer:

* SND_NKE to a primary address resets that meter's frame count bit (FCB)
  state; the meter answers ``E5``. SND_NKE to 255 (broadcast) resets every
  meter and nobody answers. SND_NKE to 253 deselects the selected meters,
  which answer ``E5``.
* SND_UD with CI ``0x52`` to 253 selects every meter that matches the 8-byte
  pattern (ID digits with ``F`` nibble wildcards, then manufacturer, version
  and medium with ``FFFF``/``FF`` wildcards); the others are deselected; a
  matching meter answers ``E5``.
* REQ_UD2 to a primary address, or to 253 for the selected meter, returns
  the next RSP_UD (CI ``0x72``). A telegram that does not hold the last
  records ends with DIF ``0x1F``; the next REQ_UD2 with the FCB toggled
  returns the next telegram, and after the last one the first again. A
  REQ_UD2 with the same FCB as the one before repeats the last telegram
  byte for byte (the master lost it). REQ_UD2 to 255 gets no answer.
* When more than one meter would answer a request, the simulator sends a
  deliberately garbled frame (a long frame with a bad checksum), which is how
  overlapping replies look to a master. Colliding meters keep their state.
* A meter with primary address 0 (unconfigured) does not answer primary
  addressing at all, so only the secondary scan finds it. (A real
  unconfigured meter answers on address 0; the demo wants it hidden.)
* Garbled or unknown requests (REQ_UD1, SND_UD other than the selection,
  addresses 0 and 251-254) get no answer: the bus is read-only.

Every meter uses the fictional manufacturer code ``PDC``, version ``01``
and a status byte that is ``00`` except meter 5's "power low" (``04``).
Values follow the bus's clock (seconds since the bus was made): counters
(energy, volume, operating time) go up one step every few seconds (the
operating time every hour) and never decrease,
billing-date (storage 1) values are fixed, temperatures, power, flow,
voltage and current wander on a sine around their initial value. Under a
fake clock every frame is deterministic.

The tables below were written by hand from EN 13757-3; the tests check the
frames against them, and later tasks use them as the independent reference.
Medium is the header code (04 heat, 07 water, 02 electricity, 06 warm
water). Tg is the telegram, St the storage number, Sub the subunit. Unit is
the meter's natural unit as the codec reports it, Initial the value at
time 0 in that unit. The ``date`` row is a billing date (VIF
``0x6C``, type G), which the collector does not support.

<!-- meters:begin -->
| Meter | Primary | ID | Manufacturer | Version | Medium | Status | Telegrams |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 12345678 | PDC | 01 | 04 | 00 | 2 |
| 2 | 2 | 12399999 | PDC | 01 | 07 | 00 | 1 |
| 3 | 3 | 22223333 | PDC | 01 | 02 | 00 | 1 |
| 4 | 0 | 87654321 | PDC | 01 | 04 | 00 | 1 |
| 5 | 5 | 55667788 | PDC | 01 | 06 | 04 | 1 |

| Meter | Tg | Quantity | Unit | St | Tariff | Sub | Function | Qualifiers | Initial |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | energy | MWh | 0 | 0 | 0 | instantaneous | - | 1234 |
| 1 | 1 | energy | MWh | 1 | 0 | 0 | instantaneous | - | 1100 |
| 1 | 1 | volume | L | 0 | 0 | 0 | instantaneous | - | 4567890 |
| 1 | 1 | power | W | 0 | 0 | 0 | instantaneous | - | 12500 |
| 1 | 1 | volume_flow | L/h | 0 | 0 | 0 | instantaneous | - | 850 |
| 1 | 2 | flow_temperature | C | 0 | 0 | 0 | instantaneous | - | 70.00 |
| 1 | 2 | return_temperature | C | 0 | 0 | 0 | instantaneous | - | 45.00 |
| 1 | 2 | temperature_difference | K | 0 | 0 | 0 | instantaneous | - | 25.00 |
| 1 | 2 | operating_time | h | 0 | 0 | 0 | instantaneous | - | 21000 |
| 1 | 2 | date | - | 1 | 0 | 0 | instantaneous | - | 2025-12-31 |
| 2 | 1 | volume | L | 0 | 0 | 0 | instantaneous | - | 123456 |
| 2 | 1 | volume | L | 1 | 0 | 0 | instantaneous | - | 100000 |
| 2 | 1 | volume_flow | L/h | 0 | 0 | 0 | instantaneous | - | 1800 |
| 3 | 1 | energy | Wh | 0 | 1 | 0 | instantaneous | forward | 12345670 |
| 3 | 1 | energy | Wh | 0 | 2 | 0 | instantaneous | forward | 6543210 |
| 3 | 1 | energy | Wh | 0 | 1 | 0 | instantaneous | backward | 1230 |
| 3 | 1 | energy | Wh | 0 | 2 | 0 | instantaneous | backward | 560 |
| 3 | 1 | power | W | 0 | 0 | 0 | instantaneous | - | 2345.6 |
| 3 | 1 | voltage | V | 0 | 0 | 0 | instantaneous | - | 230.0 |
| 3 | 1 | current | A | 0 | 0 | 0 | instantaneous | - | 10.200 |
| 4 | 1 | energy | GJ | 0 | 0 | 0 | instantaneous | - | 4321 |
| 4 | 1 | volume | L | 0 | 0 | 0 | instantaneous | - | 987650 |
| 4 | 1 | flow_temperature | C | 0 | 0 | 0 | instantaneous | - | 65.00 |
| 4 | 1 | return_temperature | C | 0 | 0 | 0 | instantaneous | - | 40.00 |
| 5 | 1 | volume | L | 0 | 0 | 0 | instantaneous | - | 54321 |
| 5 | 1 | volume_flow | L/h | 0 | 0 | 0 | instantaneous | - | 300 |
<!-- meters:end -->

Encodings (DIF data field / VIF): meter 1 energy BCD8 ``FB 01`` (MWh),
volume BCD8 ``14`` (10 L), power int24 ``2B`` (W), flow int16 ``3B`` (L/h),
temperatures int16 ``59``/``5D``/``61`` (0.01), operating time int32 ``26``
(h), date int16 ``6C``; meter 2 volume BCD8 ``13`` (L); meter 3 energy
``84 3B``/``84 3C`` (10 Wh, forward/backward) BCD8 and int32 with a tariff
DIFE, power real ``2B``, voltage int16 ``FD 48`` (0.1 V), current int16
``FD 59`` (mA); meter 4 energy BCD8 ``FB 09`` (GJ).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import logging
import math
import os
import struct
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from plc_data_collector.app.logging import configure_logging
from plc_data_collector.app.service import install_signal_handlers
from plc_data_collector.protocols.mbus_codec import (
    ACK,
    ADDRESS_BROADCAST,
    ADDRESS_SECONDARY,
    Frame,
    FrameReader,
    GarbledFrame,
    LongFrame,
    ShortFrame,
)
from plc_data_collector.protocols.mbus_tags import Function, Quantity

LOGGER = logging.getLogger("plc_data_collector.simulators.mbus")

DEFAULT_PORT = 10001
MANUFACTURER = "PDC"

# Link layer (EN 13757-2). The simulator keeps its own constants, so its
# frames check the codec rather than mirror it.
_C_SND_NKE = 0x40
_C_REQ_UD2 = 0x4B  # with the FCB (0x20) and FCV (0x10) bits masked out
_C_SND_UD = 0x43
_C_RSP_UD = 0x08
_C_FCB = 0x20
_C_FCV = 0x10
_C_FUNCTION_MASK = 0xCF
_CI_SELECT = 0x52
_CI_RSP_UD = 0x72
_MAX_PRIMARY = 250
_PATTERN_LENGTH = 8
_MORE_RECORDS = 0x1F
_EXTENSION = 0x80
_ACK = bytes((ACK,))
# Overlapping replies: a long frame whose checksum is off by one.
_GARBLED = bytes((0x68, 0x03, 0x03, 0x68, _C_RSP_UD, 0xFD, _CI_RSP_UD))
_GARBLED += bytes(((sum(_GARBLED[4:]) + 1) & 0xFF, 0x16))

_FUNCTION_BITS = {
    Function.INSTANTANEOUS: 0,
    Function.MAXIMUM: 1,
    Function.MINIMUM: 2,
    Function.ERROR: 3,
}


class Coding(Enum):
    """A DIF data field: (code, bytes)."""

    INT16 = (0x2, 2)
    INT24 = (0x3, 3)
    INT32 = (0x4, 4)
    REAL = (0x5, 4)
    BCD8 = (0xC, 4)

    @property
    def code(self) -> int:
        """The DIF data field code."""
        return self.value[0]

    @property
    def length(self) -> int:
        """Bytes of data."""
        return self.value[1]


@dataclass(frozen=True, slots=True)
class SimulatedRecord:
    """A data record whose value follows the clock.

    ``vif`` is the whole VIF/VIFE chain, extension bits included. ``unit``
    and ``step`` (one raw count, or the resolution of a real) are in the
    meter's natural unit; ``unit`` and ``quantity``/``qualifiers`` describe
    the record for readers and tests, the bytes come from ``vif``. A record
    with ``step_every_s`` goes up one step every that many seconds; one with
    ``wander`` swings on a sine of ``period_s``; otherwise it keeps
    ``initial``.
    """

    quantity: Quantity
    unit: str
    coding: Coding
    vif: bytes
    step: Decimal
    initial: Decimal
    storage: int = 0
    tariff: int = 0
    subunit: int = 0
    function: Function = Function.INSTANTANEOUS
    qualifiers: tuple[str, ...] = ()
    step_every_s: float | None = None
    wander: Decimal = Decimal(0)
    period_s: float = 600.0

    def value(self, elapsed_s: float) -> Decimal:
        """The value ``elapsed_s`` seconds after the bus started."""
        if self.step_every_s is not None:
            steps = math.floor(max(elapsed_s, 0.0) / self.step_every_s)
            return self.initial + steps * self.step
        if self.wander:
            angle = 2 * math.pi * elapsed_s / self.period_s
            swing = float(self.wander) * math.sin(angle)
            return self.initial + round(swing / float(self.step)) * self.step
        return self.initial

    def encode(self, elapsed_s: float) -> bytes:
        """DIF, DIFEs, VIF chain and data."""
        head = _data_information(
            self.coding.code, self.function, self.storage, self.tariff, self.subunit
        )
        return head + self.vif + self._data(self.value(elapsed_s))

    def _data(self, value: Decimal) -> bytes:
        if self.coding is Coding.REAL:
            return struct.pack("<f", float(value))
        counts = value / self.step
        if counts != counts.to_integral_value():
            raise ValueError(f"{value} is not a whole number of {self.step}")
        raw = int(counts)
        if self.coding is Coding.BCD8:
            if not 0 <= raw <= 99_999_999:
                raise ValueError(f"{raw} does not fit 8 BCD digits")
            return bytes.fromhex(f"{raw:08d}")[::-1]
        return raw.to_bytes(self.coding.length, "little", signed=True)


@dataclass(frozen=True, slots=True)
class SimulatedDate:
    """A date record (VIF ``0x6C``, type G, int16), unsupported by the codec."""

    day: datetime.date
    storage: int = 0

    def encode(self, elapsed_s: float) -> bytes:
        """DIF, DIFEs, VIF and the type G date (the clock does not move it)."""
        del elapsed_s
        year = self.day.year % 100
        data = bytes(
            (
                self.day.day | (year & 0x07) << 5,
                self.day.month | (year >> 3) << 4,
            )
        )
        head = _data_information(
            Coding.INT16.code, Function.INSTANTANEOUS, self.storage, 0, 0
        )
        return head + bytes((0x6C,)) + data


type Record = SimulatedRecord | SimulatedDate


def _data_information(
    code: int, function: Function, storage: int, tariff: int, subunit: int
) -> bytes:
    """DIF and DIFEs (EN 13757-3).

    Storage bit 0 goes in the DIF; the higher storage bits, the tariff and the
    subunit go in as many DIFEs as they need.
    """
    fields = [code | _FUNCTION_BITS[function] << 4 | (storage & 1) << 6]
    storage >>= 1
    while storage or tariff or subunit:
        fields.append((storage & 0x0F) | (tariff & 0x03) << 4 | (subunit & 1) << 6)
        storage >>= 4
        tariff >>= 2
        subunit >>= 1
    last = len(fields) - 1
    return bytes(
        byte | (_EXTENSION if index < last else 0) for index, byte in enumerate(fields)
    )


@dataclass(slots=True)
class SimulatedMeter:
    """One meter on the bus. Fields are settable so tests can vary a meter.

    ``primary`` 0 means unconfigured: the meter answers secondary addressing
    only. ``records_per_telegram`` splits the records into several RSP_UD
    telegrams (``None``: all in one).
    """

    primary: int
    id_digits: str
    medium: int
    records: tuple[Record, ...]
    status: int = 0
    version: int = 0x01
    manufacturer: str = MANUFACTURER
    records_per_telegram: int | None = None

    def __post_init__(self) -> None:
        """Check the address and identity."""
        if not 0 <= self.primary <= _MAX_PRIMARY:
            raise ValueError(f"primary address must be 0-250, got {self.primary}")
        if len(self.id_digits) != 8 or not self.id_digits.isdigit():
            raise ValueError(f"ID must be 8 digits, got {self.id_digits!r}")

    def telegrams(self) -> list[tuple[Record, ...]]:
        """The records of each telegram, in order."""
        size = self.records_per_telegram or len(self.records) or 1
        return [
            self.records[start : start + size]
            for start in range(0, max(len(self.records), 1), size)
        ]

    def header(self, access_no: int) -> bytes:
        """The 12-byte RSP_UD fixed header."""
        return (
            bytes.fromhex(self.id_digits)[::-1]
            + _manufacturer_value(self.manufacturer).to_bytes(2, "little")
            + bytes((self.version, self.medium, access_no, self.status, 0, 0))
        )

    def matches(self, pattern: bytes) -> bool:
        """Whether a secondary selection pattern selects this meter."""
        id_bytes = bytes.fromhex(self.id_digits)[::-1]
        for wanted, have in zip(pattern[:4], id_bytes, strict=True):
            for shift in (0, 4):
                nibble = wanted >> shift & 0x0F
                if nibble != 0x0F and nibble != have >> shift & 0x0F:
                    return False
        manufacturer = int.from_bytes(pattern[4:6], "little")
        if manufacturer not in (0xFFFF, _manufacturer_value(self.manufacturer)):
            return False
        return pattern[6] in (0xFF, self.version) and pattern[7] in (0xFF, self.medium)


def _manufacturer_value(code: str) -> int:
    """The 2-byte manufacturer field: three letters, five bits each."""
    value = 0
    for letter in code:
        value = value << 5 | (ord(letter) - ord("A") + 1)
    return value


def _long_frame(control: int, address: int, ci: int, payload: bytes) -> bytes:
    body = bytes((control, address, ci)) + payload
    if len(body) > 255:
        raise ValueError("telegram longer than a long frame allows")
    size = len(body)
    return bytes((0x68, size, size, 0x68)) + body + bytes((sum(body) & 0xFF, 0x16))


@dataclass(slots=True)
class _LinkState:
    selected: bool = False
    last_fcb: bool | None = None
    last_reply: bytes | None = None
    next_telegram: int = 0
    access_no: int = 0


@dataclass(slots=True)
class SimulatedBus:
    """Meters on one bus answering the requests of one master.

    ``clock`` returns seconds; values follow the time since the bus was
    made. ``meters`` may be changed in place (a meter's status, say), but not
    added to or removed from once the bus exists.
    """

    meters: list[SimulatedMeter]
    clock: Callable[[], float] = time.monotonic
    _started: float = field(init=False)
    _states: list[_LinkState] = field(init=False)

    def __post_init__(self) -> None:
        """Start the clock and the link state of each meter."""
        self._started = self.clock()
        self._states = [_LinkState() for _ in self.meters]

    def handle(self, request: bytes) -> bytes | None:
        """The bus's answer to one request frame; ``None`` when nobody answers.

        Bytes that are not exactly one valid frame get no answer.
        """
        reader = FrameReader()
        reader.feed(request)
        try:
            frame = reader.next_frame()
            if frame is None or reader.next_frame() is not None:
                return None
        except GarbledFrame:
            return None
        return self.answer(frame)

    def answer(self, frame: Frame) -> bytes | None:
        """The bus's answer to one decoded request frame."""
        if isinstance(frame, ShortFrame):
            if frame.control == _C_SND_NKE:
                return self._snd_nke(frame.address)
            if frame.control & _C_FUNCTION_MASK == _C_REQ_UD2:
                fcv = bool(frame.control & _C_FCV)
                return self._req_ud2(frame.address, fcv, bool(frame.control & _C_FCB))
        elif (
            isinstance(frame, LongFrame)
            and frame.control & _C_FUNCTION_MASK == _C_SND_UD
            and frame.address == ADDRESS_SECONDARY
            and frame.ci == _CI_SELECT
            and len(frame.payload) == _PATTERN_LENGTH
        ):
            return self._select(frame.payload)
        return None

    def _addressed(self, address: int) -> list[int]:
        if address == ADDRESS_SECONDARY:
            return [i for i, state in enumerate(self._states) if state.selected]
        if 1 <= address <= _MAX_PRIMARY:
            return [i for i, m in enumerate(self.meters) if m.primary == address]
        return []

    def _snd_nke(self, address: int) -> bytes | None:
        if address == ADDRESS_BROADCAST:
            for state in self._states:
                _reset(state)
            return None
        targets = self._addressed(address)
        for index in targets:
            state = self._states[index]
            _reset(state)
            if address == ADDRESS_SECONDARY:
                state.selected = False
        return _reply(targets, lambda _: _ACK)

    def _select(self, pattern: bytes) -> bytes | None:
        targets = []
        for index, meter in enumerate(self.meters):
            selected = meter.matches(pattern)
            self._states[index].selected = selected
            if selected:
                targets.append(index)
        return _reply(targets, lambda _: _ACK)

    def _req_ud2(self, address: int, fcv: bool, fcb: bool) -> bytes | None:
        if address == ADDRESS_BROADCAST:
            return None
        return _reply(self._addressed(address), lambda i: self._rsp_ud(i, fcv, fcb))

    def _rsp_ud(self, index: int, fcv: bool, fcb: bool) -> bytes:
        state = self._states[index]
        if fcv and fcb == state.last_fcb and state.last_reply is not None:
            return state.last_reply  # the master did not get it: repeat
        meter = self.meters[index]
        telegrams = meter.telegrams()
        number = state.next_telegram if state.next_telegram < len(telegrams) else 0
        more = number + 1 < len(telegrams)
        elapsed = self.clock() - self._started
        state.access_no = (state.access_no + 1) & 0xFF
        payload = meter.header(state.access_no) + b"".join(
            record.encode(elapsed) for record in telegrams[number]
        )
        if more:
            payload += bytes((_MORE_RECORDS,))
        reply = _long_frame(_C_RSP_UD, meter.primary, _CI_RSP_UD, payload)
        state.next_telegram = number + 1 if more else 0
        state.last_fcb = fcb if fcv else None
        state.last_reply = reply
        return reply


def _reset(state: _LinkState) -> None:
    state.last_fcb = None
    state.last_reply = None
    state.next_telegram = 0


def _reply(targets: list[int], make: Callable[[int], bytes]) -> bytes | None:
    if not targets:
        return None
    if len(targets) > 1:
        return _GARBLED
    return make(targets[0])


# The demo meters


def _counter(
    quantity: Quantity,
    unit: str,
    coding: Coding,
    vif: bytes,
    step: str,
    initial: str,
    every_s: float | None = None,
    *,
    storage: int = 0,
) -> SimulatedRecord:
    """A counter going up one ``step`` every ``every_s`` (``None``: fixed)."""
    return SimulatedRecord(
        quantity,
        unit,
        coding,
        vif,
        Decimal(step),
        Decimal(initial),
        storage=storage,
        step_every_s=every_s,
    )


def _wandering(
    quantity: Quantity,
    unit: str,
    coding: Coding,
    vif: bytes,
    step: str,
    initial: str,
    wander: str,
    period_s: float,
) -> SimulatedRecord:
    """A value swinging ``wander`` around ``initial`` every ``period_s``."""
    return SimulatedRecord(
        quantity,
        unit,
        coding,
        vif,
        Decimal(step),
        Decimal(initial),
        wander=Decimal(wander),
        period_s=period_s,
    )


def _electricity(
    vif: bytes, coding: Coding, initial: str, every_s: float | None, tariff: int
) -> SimulatedRecord:
    qualifier = "forward" if vif == _FORWARD_10WH else "backward"
    return SimulatedRecord(
        Quantity.ENERGY,
        "Wh",
        coding,
        vif,
        Decimal(10),
        Decimal(initial),
        tariff=tariff,
        qualifiers=(qualifier,),
        step_every_s=every_s,
    )


_Q = Quantity
_MWH = bytes((0xFB, 0x01))
_GJ = bytes((0xFB, 0x09))
_VOLUME_10L = bytes((0x14,))
_VOLUME_L = bytes((0x13,))
_POWER_W = bytes((0x2B,))
_FLOW_LH = bytes((0x3B,))
_FLOW_TEMP = bytes((0x59,))
_RETURN_TEMP = bytes((0x5D,))
_TEMP_DIFF = bytes((0x61,))
_HOURS = bytes((0x26,))
_FORWARD_10WH = bytes((0x84, 0x3B))
_BACKWARD_10WH = bytes((0x84, 0x3C))
_VOLTS_TENTH = bytes((0xFD, 0x48))
_MILLIAMPERES = bytes((0xFD, 0x59))
_HEAT, _WATER, _ELECTRICITY, _WARM_WATER = 0x04, 0x07, 0x02, 0x06
_POWER_LOW = 0x04


def demo_meters() -> list[SimulatedMeter]:
    """The five meters of the module docstring, as new objects."""
    return [
        SimulatedMeter(
            primary=1,
            id_digits="12345678",
            medium=_HEAT,
            records_per_telegram=5,
            records=(
                _counter(_Q.ENERGY, "MWh", Coding.BCD8, _MWH, "1", "1234", 50),
                _counter(_Q.ENERGY, "MWh", Coding.BCD8, _MWH, "1", "1100", storage=1),
                _counter(_Q.VOLUME, "L", Coding.BCD8, _VOLUME_10L, "10", "4567890", 10),
                _wandering(
                    _Q.POWER, "W", Coding.INT24, _POWER_W, "1", "12500", "1500", 300
                ),
                _wandering(
                    _Q.VOLUME_FLOW,
                    "L/h",
                    Coding.INT16,
                    _FLOW_LH,
                    "1",
                    "850",
                    "100",
                    240,
                ),
                _wandering(
                    _Q.FLOW_TEMPERATURE,
                    "C",
                    Coding.INT16,
                    _FLOW_TEMP,
                    "0.01",
                    "70.00",
                    "2.5",
                    900,
                ),
                _wandering(
                    _Q.RETURN_TEMPERATURE,
                    "C",
                    Coding.INT16,
                    _RETURN_TEMP,
                    "0.01",
                    "45.00",
                    "1.5",
                    700,
                ),
                _wandering(
                    _Q.TEMPERATURE_DIFFERENCE,
                    "K",
                    Coding.INT16,
                    _TEMP_DIFF,
                    "0.01",
                    "25.00",
                    "1",
                    800,
                ),
                _counter(
                    _Q.OPERATING_TIME,
                    "h",
                    Coding.INT32,
                    _HOURS,
                    "1",
                    "21000",
                    3600,
                ),
                SimulatedDate(datetime.date(2025, 12, 31), storage=1),
            ),
        ),
        SimulatedMeter(
            primary=2,
            id_digits="12399999",
            medium=_WATER,
            records=(
                _counter(_Q.VOLUME, "L", Coding.BCD8, _VOLUME_L, "1", "123456", 2),
                _counter(
                    _Q.VOLUME, "L", Coding.BCD8, _VOLUME_L, "1", "100000", storage=1
                ),
                _wandering(
                    _Q.VOLUME_FLOW,
                    "L/h",
                    Coding.INT16,
                    _FLOW_LH,
                    "1",
                    "1800",
                    "200",
                    240,
                ),
            ),
        ),
        SimulatedMeter(
            primary=3,
            id_digits="22223333",
            medium=_ELECTRICITY,
            records=(
                _electricity(_FORWARD_10WH, Coding.BCD8, "12345670", 3, 1),
                _electricity(_FORWARD_10WH, Coding.BCD8, "6543210", 10, 2),
                _electricity(_BACKWARD_10WH, Coding.INT32, "1230", 200, 1),
                _electricity(_BACKWARD_10WH, Coding.INT32, "560", None, 2),
                _wandering(
                    _Q.POWER, "W", Coding.REAL, _POWER_W, "0.1", "2345.6", "300", 120
                ),
                _wandering(
                    _Q.VOLTAGE, "V", Coding.INT16, _VOLTS_TENTH, "0.1", "230.0", "2", 90
                ),
                _wandering(
                    _Q.CURRENT,
                    "A",
                    Coding.INT16,
                    _MILLIAMPERES,
                    "0.001",
                    "10.200",
                    "1.5",
                    120,
                ),
            ),
        ),
        SimulatedMeter(
            primary=0,
            id_digits="87654321",
            medium=_HEAT,
            records=(
                _counter(_Q.ENERGY, "GJ", Coding.BCD8, _GJ, "1", "4321", 100),
                _counter(_Q.VOLUME, "L", Coding.BCD8, _VOLUME_10L, "10", "987650", 20),
                _wandering(
                    _Q.FLOW_TEMPERATURE,
                    "C",
                    Coding.INT16,
                    _FLOW_TEMP,
                    "0.01",
                    "65.00",
                    "2",
                    600,
                ),
                _wandering(
                    _Q.RETURN_TEMPERATURE,
                    "C",
                    Coding.INT16,
                    _RETURN_TEMP,
                    "0.01",
                    "40.00",
                    "1.5",
                    500,
                ),
            ),
        ),
        SimulatedMeter(
            primary=5,
            id_digits="55667788",
            medium=_WARM_WATER,
            status=_POWER_LOW,
            records=(
                _counter(_Q.VOLUME, "L", Coding.BCD8, _VOLUME_L, "1", "54321", 5),
                _wandering(
                    _Q.VOLUME_FLOW, "L/h", Coding.INT16, _FLOW_LH, "1", "300", "50", 180
                ),
            ),
        ),
    ]


# Transports


async def _serve_stream(
    bus: SimulatedBus,
    receive: Callable[[], Awaitable[bytes]],
    send: Callable[[bytes], Awaitable[None]],
) -> None:
    """Assemble request frames from a byte stream and send the bus's answers.

    Returns when ``receive`` returns no bytes (the other end closed). Garbled
    input is dropped without an answer, as a meter would.
    """
    reader = FrameReader()
    while data := await receive():
        reader.feed(data)
        while True:
            try:
                frame = reader.next_frame()
            except GarbledFrame as error:
                LOGGER.debug("garbled_request", extra={"reason": str(error)})
                break
            if frame is None:
                break
            reply = bus.answer(frame)
            if reply is not None:
                await send(reply)


class TcpSimulator:
    """A listening TCP simulator; ``close`` also drops connected clients."""

    def __init__(self, server: asyncio.Server, clients: set[asyncio.StreamWriter]):
        """Wrap a started server and the set its handler keeps clients in."""
        self._server = server
        self._clients = clients

    @property
    def port(self) -> int:
        """The bound port (useful with port 0)."""
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        """Stop listening, close every client and wait for them to finish."""
        self._server.close()
        for writer in list(self._clients):
            writer.close()
        await self._server.wait_closed()


async def serve_tcp(bus: SimulatedBus, host: str, port: int) -> TcpSimulator:
    """Serve ``bus`` to TCP clients (several clients share the one bus)."""
    clients: set[asyncio.StreamWriter] = set()

    async def client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        clients.add(writer)

        async def send(reply: bytes) -> None:
            writer.write(reply)
            await writer.drain()

        try:
            await _serve_stream(bus, lambda: reader.read(512), send)
        except OSError:
            pass  # the client went away
        finally:
            clients.discard(writer)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_server(client, host, port)
    return TcpSimulator(server, clients)


async def serve_serial(bus: SimulatedBus, fd: int) -> None:
    """Serve ``bus`` on a pseudo-terminal master ``fd`` (POSIX only).

    Returns when the slave side is closed; the caller keeps owning ``fd``.
    """
    loop = asyncio.get_running_loop()
    was_blocking = os.get_blocking(fd)
    os.set_blocking(fd, False)
    incoming: asyncio.Queue[bytes] = asyncio.Queue()

    def readable() -> None:
        try:
            data = os.read(fd, 512)
        except BlockingIOError:
            return
        except OSError:  # EIO: every slave fd is closed
            data = b""
        if not data:
            loop.remove_reader(fd)
        incoming.put_nowait(data)

    async def send(reply: bytes) -> None:
        view = memoryview(reply)
        while view:
            try:
                view = view[os.write(fd, view) :]
            except BlockingIOError:
                await asyncio.sleep(0.001)

    loop.add_reader(fd, readable)
    try:
        await _serve_stream(bus, incoming.get, send)
    except OSError:
        pass  # the slave side went away while answering
    finally:
        loop.remove_reader(fd)
        os.set_blocking(fd, was_blocking)


# Command line


async def run_simulator(
    *,
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Serve the five demo meters over TCP until ``stop_event`` is set."""
    stop = asyncio.Event() if stop_event is None else stop_event
    server = await serve_tcp(SimulatedBus(demo_meters()), host, port)
    try:
        LOGGER.info("simulator_started", extra={"host": host, "port": port})
        await stop.wait()
        LOGGER.info("simulator_stopping")
    finally:
        await server.close()
    LOGGER.info("simulator_stopped")


def build_parser() -> argparse.ArgumentParser:
    """Build the simulator command line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m plc_data_collector.simulators.mbus",
        description="Wired M-Bus simulator: five meters behind a TCP gateway.",
    )
    parser.add_argument("--host", default=os.environ.get("MBUS_SIM_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MBUS_SIM_PORT", str(DEFAULT_PORT))),
    )
    return parser


async def _serve(host: str, port: int) -> None:
    stop_event = asyncio.Event()
    callbacks = install_signal_handlers(stop_event)
    try:
        await run_simulator(host=host, port=port, stop_event=stop_event)
    finally:
        for callback in callbacks:
            callback()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the simulator until SIGTERM or SIGINT."""
    args = build_parser().parse_args(argv)
    configure_logging()
    asyncio.run(_serve(args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
