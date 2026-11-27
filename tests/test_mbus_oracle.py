"""pyMeterBus as an oracle for the M-Bus codec (test-only dependency).

pyMeterBus scales values with floats, so values agree within 1e-9 relative;
the codec's own tests pin the exact decimals.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mbus_fixtures import fixture_names, read_frame
from mbus_support import oracle_compare

# Records the codec decodes but pyMeterBus cannot, by (fixture, record index).
_PYMETERBUS_CANNOT = {
    ("hand_keys.hex", 8): "pyMeterBus ignores multiplicative-correction VIFE 0x7D",
    ("hand_keys.hex", 9): "pyMeterBus ignores multiplicative-correction VIFE 0x73",
}


def _compare_fixture(name: str) -> tuple[int, set[tuple[str, int]]]:
    cannot = frozenset(
        index for fixture, index in _PYMETERBUS_CANNOT if fixture == name
    )
    compared, skipped = oracle_compare(read_frame(name), name, cannot)
    return compared, {(name, index) for index in skipped}


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
