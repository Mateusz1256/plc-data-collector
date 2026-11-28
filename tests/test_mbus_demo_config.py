"""The demo M-Bus Connection against the simulator's documented records."""

from __future__ import annotations

from pathlib import Path

from mbus_support import MBusSimServer, documented_meters, documented_records

from plc_data_collector.app.config import load_collector_config
from plc_data_collector.domain import ConnectionConfig, Quality, TagRequest
from plc_data_collector.protocols.mbus import MBusDriver
from plc_data_collector.protocols.mbus_tags import MBusTagOptions, resolve_tag

DEMO_CONFIG = Path(__file__).parents[1] / "deploy" / "demo" / "collector.config.json"
CONNECTION_ID = "heating_mbus"
DEMO_METERS = {1, 2, 3, 5}


def test_demo_connection_and_groups() -> None:
    config = load_collector_config(DEMO_CONFIG, environ={})
    connection = next(c for c in config.connections if c.id == CONNECTION_ID)
    assert connection.protocol == "mbus"
    assert connection.endpoint == "tcp://mbus-sim:10001"
    groups = {g.id: g for g in config.tag_groups if g.connection_id == CONNECTION_ID}
    assert {g: groups[g].interval_ms for g in groups} == {
        "billing": 60000,
        "monitoring": 10000,
    }


def test_every_demo_tag_matches_a_documented_record() -> None:
    config = load_collector_config(DEMO_CONFIG, environ={})
    connection = next(c for c in config.connections if c.id == CONNECTION_ID)
    group_ids = {g.id for g in config.tag_groups if g.connection_id == CONNECTION_ID}
    tags = [t for t in config.tags if t.tag_group_id in group_ids]
    assert tags
    meters = {m.number: m for m in documented_meters()}
    by_primary = {m.primary: m for m in meters.values()}
    used = set()
    for tag in tags:
        plan = resolve_tag(tag, connection)
        assert plan.meter.primary in DEMO_METERS
        meter = by_primary[plan.meter.primary or 0]
        assert plan.meter_id == meter.id_digits
        options = MBusTagOptions.model_validate(tag.protocol_options)
        rows = [
            r
            for r in documented_records()
            if r.meter == meter.number
            and r.quantity == options.quantity
            and r.storage == options.storage
            and r.tariff == options.tariff
            and r.subunit == options.subunit
            and r.function == options.function
            and r.qualifiers == options.qualifiers
        ]
        assert len(rows) == 1, tag.id
        used.add(tag.id)
    assert len(used) == len(tags)


async def test_every_demo_tag_reads_good(mbus_sim: MBusSimServer) -> None:
    config = load_collector_config(DEMO_CONFIG, environ={})
    base = next(c for c in config.connections if c.id == CONNECTION_ID)
    group_ids = {g.id for g in config.tag_groups if g.connection_id == CONNECTION_ID}
    tags = [t for t in config.tags if t.tag_group_id in group_ids]
    driver = MBusDriver(
        ConnectionConfig(
            id=base.id,
            protocol="mbus",
            endpoint=mbus_sim.endpoint,
            timeout_ms=2000,
            protocol_options=base.protocol_options,
        )
    )
    await driver.connect()
    try:
        results = await driver.read(
            [
                TagRequest(
                    tag_id=t.id,
                    address=t.address,
                    value_type=t.value_type,
                    protocol_options=t.protocol_options,
                )
                for t in tags
            ]
        )
    finally:
        await driver.disconnect()
    assert len(results) == len(tags)
    for result in results:
        assert result.quality is Quality.GOOD, (result.tag_id, result.error_message)
        assert result.value is not None
