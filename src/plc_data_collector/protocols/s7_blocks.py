"""Plan S7 reads: merge Tags into blocks, split blocks into items, pack requests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from plc_data_collector.protocols.s7_codec import (
    MAX_ITEM_PAYLOAD,
    ReadItem,
    request_size,
    response_size,
)
from plc_data_collector.protocols.s7_tags import Area, S7TagPlan

# Items one Read Var request may carry. S7 CPUs refuse more, whatever the PDU
# size allows; python-snap7 uses the same limit (MaxVars = 20).
MAX_ITEMS = 20


@dataclass(frozen=True)
class S7Block:
    """A contiguous byte range of one area/DB covering one or more Tags."""

    area: Area
    db: int
    start: int
    length: int
    members: tuple[tuple[str, S7TagPlan], ...]


def plan_blocks(
    tags: Sequence[tuple[str, S7TagPlan]], *, max_gap: int, max_block: int
) -> list[S7Block]:
    """Merge Tags of the same area/DB while the gap and block size allow it.

    A single Tag longer than ``max_block`` gets a block of its own.
    """
    groups: dict[tuple[Area, int], list[tuple[str, S7TagPlan]]] = {}
    for name, plan in tags:
        groups.setdefault((plan.area, plan.db or 0), []).append((name, plan))
    blocks: list[S7Block] = []
    for (area, db), members in groups.items():
        members.sort(key=lambda member: member[1].byte)
        current: list[tuple[str, S7TagPlan]] = []
        start = end = 0
        for name, plan in members:
            plan_end = plan.byte + plan.span
            if current and (
                plan.byte - end <= max_gap and max(end, plan_end) - start <= max_block
            ):
                current.append((name, plan))
                end = max(end, plan_end)
                continue
            if current:
                blocks.append(S7Block(area, db, start, end - start, tuple(current)))
            current = [(name, plan)]
            start, end = plan.byte, plan_end
        blocks.append(S7Block(area, db, start, end - start, tuple(current)))
    return blocks


def block_items(block: S7Block, max_item: int) -> list[ReadItem]:
    """Split a block into contiguous items of at most ``max_item`` bytes."""
    items: list[ReadItem] = []
    offset = 0
    while offset < block.length:
        length = min(max_item, block.length - offset)
        items.append(
            ReadItem(block.area.wire_code, block.db, block.start + offset, length)
        )
        offset += length
    return items


def pack_requests(
    blocks: Sequence[S7Block], *, pdu_length: int, max_items: int = MAX_ITEMS
) -> list[list[tuple[S7Block, list[ReadItem]]]]:
    """Pack block items greedily into requests of at most ``max_items`` items
    that fit ``pdu_length``.

    A block whose items do not fit one request appears in several requests,
    each time with a subset of its items.
    """
    max_item = MAX_ITEM_PAYLOAD(pdu_length)
    requests: list[list[tuple[S7Block, list[ReadItem]]]] = []
    current: list[tuple[S7Block, list[ReadItem]]] = []
    lengths: list[int] = []
    for block in blocks:
        for item in block_items(block, max_item):
            candidate = [*lengths, item.length]
            if current and (
                len(candidate) > max_items
                or request_size(len(candidate)) > pdu_length
                or response_size(candidate) > pdu_length
            ):
                requests.append(current)
                current, lengths = [], []
                candidate = [item.length]
            lengths = candidate
            if current and current[-1][0] is block:
                current[-1][1].append(item)
            else:
                current.append((block, [item]))
    if current:
        requests.append(current)
    return requests
