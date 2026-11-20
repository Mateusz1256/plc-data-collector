"""Plan block reads: merge nearby Modbus tags into as few requests as possible."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from plc_data_collector.protocols.modbus_tags import Area, ModbusTagPlan


@dataclass(frozen=True, slots=True)
class ReadBlock:
    """One read request covering every member fully."""

    unit_id: int
    area: Area
    start: int
    count: int
    members: tuple[tuple[str, ModbusTagPlan], ...]


def plan_blocks(
    tags: Sequence[tuple[str, ModbusTagPlan]],
    *,
    max_registers: int,
    max_bits: int,
    max_gap: int,
) -> list[ReadBlock]:
    """Group tags by (unit, area) and merge them into bounded, gap-limited blocks."""
    groups: dict[tuple[int, Area], list[tuple[str, ModbusTagPlan]]] = {}
    for item in tags:
        groups.setdefault((item[1].unit_id, item[1].area), []).append(item)

    blocks: list[ReadBlock] = []
    for (unit_id, area), items in sorted(
        groups.items(), key=lambda entry: (entry[0][0], entry[0][1].function_code)
    ):
        limit = max_bits if area.is_bit_area else max_registers
        ordered = sorted(items, key=lambda item: (item[1].address, -item[1].span))
        current: list[tuple[str, ModbusTagPlan]] = []
        start = end = 0
        for item in ordered:
            plan = item[1]
            plan_end = plan.address + plan.span
            if current and (
                plan.address <= end + max_gap and max(end, plan_end) - start <= limit
            ):
                current.append(item)
                end = max(end, plan_end)
                continue
            if current:
                blocks.append(
                    ReadBlock(unit_id, area, start, end - start, tuple(current))
                )
            current = [item]
            start, end = plan.address, plan_end
        if current:
            blocks.append(ReadBlock(unit_id, area, start, end - start, tuple(current)))
    return blocks
