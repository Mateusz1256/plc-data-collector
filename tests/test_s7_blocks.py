from __future__ import annotations

from plc_data_collector.protocols.s7_blocks import (
    S7Block,
    block_items,
    pack_requests,
    plan_blocks,
)
from plc_data_collector.protocols.s7_codec import (
    MAX_ITEM_PAYLOAD,
    request_size,
    response_size,
)
from plc_data_collector.protocols.s7_tags import S7TagPlan, resolve_tag


def tag(address: str, data_type: str, **options: object) -> tuple[str, S7TagPlan]:
    return address, resolve_tag(address, {"data_type": data_type, **options})


def test_close_tags_merge_within_gap_only() -> None:
    tags = [tag("DB1.0", "int"), tag("DB1.10", "int")]

    merged = plan_blocks(tags, max_gap=16, max_block=222)
    split = plan_blocks(tags, max_gap=4, max_block=222)

    assert len(merged) == 1
    assert (merged[0].start, merged[0].length) == (0, 12)
    assert [name for name, _ in merged[0].members] == ["DB1.0", "DB1.10"]
    assert [(b.start, b.length) for b in split] == [(0, 2), (10, 2)]


def test_areas_and_data_blocks_never_merge() -> None:
    tags = [tag("DB1.0", "int"), tag("M0", "byte"), tag("DB2.0", "int")]

    blocks = plan_blocks(tags, max_gap=16, max_block=222)

    assert len(blocks) == 3
    assert {(b.area.value, b.db) for b in blocks} == {("db", 1), ("db", 2), ("m", 0)}


def test_bits_of_one_byte_share_it() -> None:
    tags = [tag("DB1.4.0", "bool"), tag("DB1.4.7", "bool")]

    (block,) = plan_blocks(tags, max_gap=0, max_block=222)

    assert (block.start, block.length) == (4, 1)
    assert len(block.members) == 2


def test_max_block_is_respected() -> None:
    tags = [tag(f"DB1.{i}", "byte") for i in range(0, 300)]

    blocks = plan_blocks(tags, max_gap=0, max_block=222)

    assert [b.length for b in blocks] == [222, 78]
    assert blocks[1].start == 222


def test_oversized_tag_gets_its_own_block_and_is_split_into_items() -> None:
    tags = [
        tag("DB1.0", "byte"),
        tag("DB1.2", "string", length=254),
        tag("DB1.300", "byte"),
    ]
    max_block = MAX_ITEM_PAYLOAD(240)

    blocks = plan_blocks(tags, max_gap=16, max_block=max_block)
    big = [b for b in blocks if b.length == 256]
    items = block_items(big[0], max_block)

    assert len(big) == 1 and len(big[0].members) == 1
    assert len(blocks) == 3
    assert [i.length for i in items] == [222, 34]
    assert [i.start for i in items] == [2, 224]
    assert all(i.db == 1 and i.area_code == 0x84 for i in items)


def test_non_db_block_uses_db_zero() -> None:
    (block,) = plan_blocks([tag("M5", "byte")], max_gap=0, max_block=222)

    assert block.db == 0
    assert block_items(block, 222)[0].db == 0


def test_pack_requests_fit_the_pdu() -> None:
    for pdu in (240, 480):
        blocks = [
            S7Block(plan.area, 1, plan.byte, 1, ((n, plan),))
            for n, plan in (tag(f"DB1.{i * 3}", "byte") for i in range(30))
        ]

        requests = pack_requests(blocks, pdu_length=pdu)

        for request in requests:
            items = [item for _, group in request for item in group]
            assert request_size(len(items)) <= pdu
            assert response_size([i.length for i in items]) <= pdu
        assert sum(len(r) for r in requests) == 30
        assert len(requests) > 1 if pdu == 240 else len(requests) == 1


def test_pack_requests_spreads_one_big_block_over_requests() -> None:
    max_block = MAX_ITEM_PAYLOAD(240)
    (block,) = plan_blocks(
        [tag("DB1.0", "string", length=254)], max_gap=16, max_block=max_block
    )

    requests = pack_requests([block], pdu_length=240)

    assert len(requests) == 2
    items = []
    for request in requests:
        assert [b for b, _ in request] == [block]
        group = request[0][1]
        assert response_size([i.length for i in group]) <= 240
        items += group
    assert [(i.start, i.length) for i in items] == [(0, 222), (222, 34)]
