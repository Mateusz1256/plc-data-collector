"""pyMeterBus as an oracle for the M-Bus codec (test-only dependency).

pyMeterBus scales values with floats, so values agree within 1e-9 relative;
the codec's own tests pin the exact decimals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import meterbus  # type: ignore[import-untyped]
import pytest
from mbus_fixtures import fixture_names, read_frame

from plc_data_collector.protocols.mbus_codec import (
    FrameReader,
    LongFrame,
    MBusRecord,
    parse_rsp_ud,
)
from plc_data_collector.protocols.mbus_tags import Function, Quantity, convert

_RELATIVE = Decimal("1e-9")
_FLOAT32 = Decimal(2) ** -23
_DIF_REAL = 0x5
# pyMeterBus drops records without data (DIF data field 0x0 and 0x8).
_NO_DATA_FIELDS = frozenset({0x0, 0x8})

# pyMeterBus record type -> (quantity, the unit its value is expressed in).
_TYPES: dict[str, tuple[Quantity, str]] = {
    "VIFUnit.ENERGY_WH": (Quantity.ENERGY, "Wh"),
    "VIFUnit.ENERGY_J": (Quantity.ENERGY, "J"),
    "VIFUnit.VOLUME": (Quantity.VOLUME, "m3"),
    "VIFUnit.MASS": (Quantity.MASS, "kg"),
    "VIFUnit.ON_TIME": (Quantity.ON_TIME, "s"),
    "VIFUnit.OPERATING_TIME": (Quantity.OPERATING_TIME, "s"),
    "VIFUnit.POWER_W": (Quantity.POWER, "W"),
    "VIFUnit.POWER_J_H": (Quantity.POWER, "J/h"),
    "VIFUnit.VOLUME_FLOW": (Quantity.VOLUME_FLOW, "m3/h"),
    "VIFUnit.VOLUME_FLOW_EXT": (Quantity.VOLUME_FLOW, "m3/min"),
    "VIFUnit.VOLUME_FLOW_EXT_S": (Quantity.VOLUME_FLOW, "m3/s"),
    "VIFUnit.MASS_FLOW": (Quantity.MASS_FLOW, "kg/h"),
    "VIFUnit.FLOW_TEMPERATURE": (Quantity.FLOW_TEMPERATURE, "C"),
    "VIFUnit.RETURN_TEMPERATURE": (Quantity.RETURN_TEMPERATURE, "C"),
    "VIFUnit.TEMPERATURE_DIFFERENCE": (Quantity.TEMPERATURE_DIFFERENCE, "K"),
    "VIFUnit.EXTERNAL_TEMPERATURE": (Quantity.EXTERNAL_TEMPERATURE, "C"),
    "VIFUnit.PRESSURE": (Quantity.PRESSURE, "bar"),
    "VIFUnit.UNITS_FOR_HCA": (Quantity.HCA_UNITS, ""),
    "VIFUnitExt.VOLTS": (Quantity.VOLTAGE, "V"),
    "VIFUnitExt.AMPERE": (Quantity.CURRENT, "A"),
}
# VIF 0xFB energy: pyMeterBus types both rows "Energy"; MWh is unit Wh and GJ
# is unit "Reserved", both scaled to the base unit.
_FB_ENERGY = {"MeasureUnit.WH": "Wh", "Reserved": "J"}

_FUNCTIONS = {
    "FunctionType.INSTANTANEOUS_VALUE": Function.INSTANTANEOUS,
    "FunctionType.MAXIMUM_VALUE": Function.MAXIMUM,
    "FunctionType.MINIMUM_VALUE": Function.MINIMUM,
    "FunctionType.ERROR_STATE_VALUE": Function.ERROR,
}

# Records the codec decodes but pyMeterBus cannot, by (fixture, record index).
_PYMETERBUS_CANNOT = {
    ("hand_keys.hex", 8): "pyMeterBus ignores multiplicative-correction VIFE 0x7D",
    ("hand_keys.hex", 9): "pyMeterBus ignores multiplicative-correction VIFE 0x73",
}


@dataclass(frozen=True)
class _Theirs:
    quantity: Quantity | None
    unit: str | None
    storage: int
    tariff: int
    subunit: int
    function: Function | None
    value: Any


def _theirs(record: Any) -> _Theirs:
    info = record.interpreted
    kind = info["type"]
    quantity: Quantity | None
    unit: str | None
    if kind == "Energy" and info["unit"] in _FB_ENERGY:
        quantity, unit = Quantity.ENERGY, _FB_ENERGY[info["unit"]]
    else:
        quantity, unit = _TYPES.get(kind, (None, None))
    return _Theirs(
        quantity=quantity,
        unit=unit,
        storage=info["storage_number"],
        tariff=info.get("tariff", 0),
        subunit=info.get("device", 0),
        function=_FUNCTIONS.get(info["function"]),
        value=info["value"],
    )


def _data_records(telegram: Any) -> list[Any]:
    skipped = {"FunctionType.MORE_RECORDS_FOLLOW", "FunctionType.SPECIAL_FUNCTION"}
    return [r for r in telegram.records if r.interpreted["function"] not in skipped]


def _long_frames(data: bytes) -> list[LongFrame]:
    reader = FrameReader()
    reader.feed(data)
    frames = []
    while (frame := reader.next_frame()) is not None:
        if isinstance(frame, LongFrame):
            frames.append(frame)
    return frames


def _agree(name: str, index: int, ours: MBusRecord, theirs: _Theirs) -> None:
    where = f"{name} record {index}"
    assert ours.key is not None
    assert ours.family is not None
    assert ours.unit is not None
    assert ours.exact_value is not None
    assert theirs.quantity == ours.key.quantity, where
    assert theirs.storage == ours.key.storage, where
    assert theirs.tariff == ours.key.tariff, where
    assert theirs.subunit == ours.key.subunit, where
    assert theirs.function == ours.key.function, where
    assert theirs.unit is not None, where
    expected = Decimal(theirs.value)
    got = convert(Decimal(ours.exact_value), ours.unit, theirs.unit, ours.family)
    # A real keeps its float32 binary value in pyMeterBus; the codec takes the
    # shortest decimal that reads back as the same float32.
    relative = _FLOAT32 if ours.raw[0] & 0x0F == _DIF_REAL else _RELATIVE
    assert abs(got - expected) <= relative * abs(expected), (where, got, expected)


def _compare_fixture(name: str) -> tuple[int, set[tuple[str, int]]]:
    compared = 0
    skipped: set[tuple[str, int]] = set()
    data = read_frame(name)
    for frame in _long_frames(data):
        ours = [
            record
            for record in parse_rsp_ud(frame).records
            if record.raw[0] & 0x0F not in _NO_DATA_FIELDS
        ]
        theirs = _data_records(meterbus.load(data))
        assert len(ours) == len(theirs), name
        for index, (mine, other) in enumerate(zip(ours, theirs, strict=True)):
            if mine.key is None or mine.exact_value is None:
                continue  # unsupported or a value error: nothing to compare
            if (name, index) in _PYMETERBUS_CANNOT:
                skipped.add((name, index))
                continue
            _agree(name, index, mine, _theirs(other))
            compared += 1
    return compared, skipped


@pytest.mark.parametrize("name", fixture_names())
def test_records_agree_with_pymeterbus(name: str) -> None:
    compared, skipped = _compare_fixture(name)
    listed = {key for key in _PYMETERBUS_CANNOT if key[0] == name}
    assert skipped == listed
    if name.startswith("hand_"):
        assert compared > 0


def test_oracle_compares_most_records() -> None:
    total = sum(_compare_fixture(name)[0] for name in fixture_names())
    assert total >= 60


def test_application_never_imports_pymeterbus() -> None:
    importing = re.compile(r"^\s*(import|from)\s+meterbus\b", re.MULTILINE)
    source = Path(__file__).parents[1] / "src"
    for path in source.rglob("*.py"):
        assert not importing.search(path.read_text(encoding="utf-8")), path
