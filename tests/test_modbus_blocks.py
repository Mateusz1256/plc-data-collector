from __future__ import annotations

from plc_data_collector.protocols.modbus_blocks import plan_blocks
from plc_data_collector.protocols.modbus_tags import (
    Area,
    ModbusConnectionOptions,
    ModbusTagPlan,
    resolve_tag,
)

CONN = ModbusConnectionOptions()


def tag(
    tag_id: str, address: str, unit: int = 1, **options: object
) -> tuple[str, ModbusTagPlan]:
    options.setdefault("data_type", "int16")
    options["unit_id"] = unit
    return tag_id, resolve_tag(address, options, CONN)


def test_contiguous_registers_merge() -> None:
    tags = [
        tag("a", "hr:0"),
        tag("b", "hr:1", data_type="float32"),
        tag("c", "hr:3"),
    ]
    blocks = plan_blocks(tags, max_registers=125, max_bits=2000, max_gap=0)
    assert [(b.start, b.count) for b in blocks] == [(0, 4)]
    assert [m[0] for m in blocks[0].members] == ["a", "b", "c"]


def test_gap_respects_max_gap() -> None:
    tags = [tag("a", "hr:0"), tag("b", "hr:5")]
    assert len(plan_blocks(tags, max_registers=125, max_bits=2000, max_gap=0)) == 2
    blocks = plan_blocks(tags, max_registers=125, max_bits=2000, max_gap=4)
    assert [(b.start, b.count) for b in blocks] == [(0, 6)]


def test_overlapping_tags_share_registers() -> None:
    tags = [
        tag("a", "hr:10", data_type="uint32"),
        tag("b", "hr:11", data_type="uint16"),
    ]
    blocks = plan_blocks(tags, max_registers=125, max_bits=2000, max_gap=0)
    assert [(b.start, b.count) for b in blocks] == [(10, 2)]


def test_limit_splits_blocks() -> None:
    tags = [tag(f"t{i}", f"hr:{i}") for i in range(130)]
    blocks = plan_blocks(tags, max_registers=125, max_bits=2000, max_gap=0)
    assert [b.count for b in blocks] == [125, 5]
    coils = [tag(f"c{i}", f"co:{i}", data_type="bool") for i in range(2001)]
    blocks = plan_blocks(coils, max_registers=125, max_bits=2000, max_gap=0)
    assert [b.count for b in blocks] == [2000, 1]


def test_oversize_tag_gets_own_block() -> None:
    tags = [
        tag("a", "hr:0"),
        tag("s", "hr:1", data_type="string", length=100),
    ]
    blocks = plan_blocks(tags, max_registers=50, max_bits=2000, max_gap=0)
    assert [(b.start, b.count) for b in blocks] == [(0, 1), (1, 100)]


def test_unit_and_area_separate() -> None:
    tags = [
        tag("u2ir", "ir:0", unit=2),
        tag("u2hr", "hr:0", unit=2),
        tag("u1ir", "ir:0", unit=1),
        tag("u1hr", "hr:0", unit=1),
    ]
    blocks = plan_blocks(tags, max_registers=125, max_bits=2000, max_gap=0)
    assert [(b.unit_id, b.area) for b in blocks] == [
        (1, Area.HOLDING_REGISTERS),
        (1, Area.INPUT_REGISTERS),
        (2, Area.HOLDING_REGISTERS),
        (2, Area.INPUT_REGISTERS),
    ]


def test_bit_tags_on_same_register_share_block() -> None:
    tags = [
        tag("a", "hr:100.0", data_type="bit"),
        tag("b", "hr:100.7", data_type="bit"),
    ]
    blocks = plan_blocks(tags, max_registers=125, max_bits=2000, max_gap=0)
    assert len(blocks) == 1
    assert (blocks[0].start, blocks[0].count) == (100, 1)
    assert len(blocks[0].members) == 2
