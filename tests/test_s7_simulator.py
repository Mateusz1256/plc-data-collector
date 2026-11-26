"""S7 simulator tests: the documented DB1 layout, read with the S7 driver."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from net_support import free_port
from s7_layout_support import Row, documented_rows

from plc_data_collector.app.config import load_collector_config
from plc_data_collector.domain import (
    ConnectionConfig,
    Quality,
    TagRequest,
    TagResult,
    ValueType,
)
from plc_data_collector.protocols import create_s7_driver
from plc_data_collector.protocols.s7 import S7Driver
from plc_data_collector.simulators import s7 as simulator

ROOT = Path(__file__).parents[1]
DEMO_CONFIG = ROOT / "deploy" / "demo" / "collector.config.json"
CONNECTION_ID = "press3_s7"

_VALUE_TYPES = {
    "bool": ValueType.BOOLEAN,
    "real": ValueType.NUMERIC,
    "lreal": ValueType.NUMERIC,
    "string": ValueType.TEXT,
}


def request_for(row: Row, tag_id: str | None = None) -> TagRequest:
    options: dict[str, object] = {"data_type": row.data_type}
    if row.length is not None:
        options["length"] = row.length
    return TagRequest(
        tag_id=tag_id or row.name,
        address=row.address,
        value_type=_VALUE_TYPES.get(row.data_type, ValueType.INTEGER),
        protocol_options=options,
    )


def test_documented_table_is_parseable_and_ordered() -> None:
    rows = documented_rows()
    assert len(rows) == 18
    assert {r.name for r in rows} >= {
        "Pressure",
        "Status.Running",
        "Status.Mode",
        "Temps[0]",
        "Temps[3]",
        "Recipe",
        "Motor.Current",
        "Motor.Running",
    }
    keys = []
    for row in rows:
        byte, _, bit = row.offset.partition(".")
        assert bool(bit) == (row.data_type == "bool"), row
        keys.append((int(byte), int(bit or 0)))
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_image_sizes_and_changes() -> None:
    first = simulator.simulated_image(0.0)
    later = simulator.simulated_image(20.0)
    assert len(first.db1) == simulator.DB1_SIZE < 200
    assert len(first.merkers) == simulator.MERKER_SIZE
    assert first.db1 != later.db1
    assert first.merkers != later.merkers


def test_demo_sources_describe_the_same_db() -> None:
    source = (ROOT / "deploy" / "demo" / "s7" / "PressData.db").read_text("utf-8")
    types = (ROOT / "deploy" / "demo" / "s7" / "PressTypes.udt").read_text("utf-8")
    assert "S7_Optimized_Access := 'FALSE'" in source
    assert 'DATA_BLOCK "PressData"' in source
    assert "Array[0..3] of Real" in source
    assert "String[20]" in source
    assert "Struct" in source
    assert "Öltemperatur" in source
    assert 'TYPE "typeMotor"' in types
    assert '"typeMotor"' in source


@pytest.fixture
async def endpoint() -> AsyncIterator[str]:
    port = free_port()
    stop = asyncio.Event()
    task = asyncio.create_task(
        simulator.run_simulator(
            host="127.0.0.1", port=port, update_interval_s=0.05, stop_event=stop
        )
    )
    try:
        yield f"tcp://127.0.0.1:{port}"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def connected(endpoint: str) -> S7Driver:
    driver = create_s7_driver(
        ConnectionConfig(
            id=CONNECTION_ID,
            protocol="s7",
            endpoint=endpoint,
            timeout_ms=2000,
            protocol_options={"rack": 0, "slot": 1},
        )
    )
    for _ in range(100):
        try:
            await driver.connect()
        except Exception:
            await asyncio.sleep(0.05)
        else:
            return driver
    pytest.fail("simulator did not start")


async def read_rows(driver: S7Driver, rows: list[Row]) -> dict[str, TagResult]:
    results = await driver.read([request_for(row) for row in rows])
    return {result.tag_id: result for result in results}


def raw(results: dict[str, TagResult], name: str) -> object:
    result = results[name]
    assert result.quality is Quality.GOOD, (name, result.error_message)
    assert result.value is not None
    return result.value.raw


async def test_simulator_serves_documented_layout(endpoint: str) -> None:
    rows = documented_rows()
    driver = await connected(endpoint)
    try:
        first = await read_rows(driver, rows)
        for row in rows:
            raw(first, row.name)  # every documented offset reads good
        assert 130.0 <= float(raw(first, "Pressure")) <= 170.0  # type: ignore[arg-type]
        assert raw(first, "Recipe") == "RCP-0042"
        assert raw(first, "Status.DoorClosed") is True
        assert raw(first, "Status.Running") is not raw(first, "Status.Fault")
        temps = [float(raw(first, f"Temps[{i}]")) for i in range(4)]  # type: ignore[arg-type]
        assert all(30.0 <= t <= 90.0 for t in temps)
        assert raw(first, "Motor.Overheat") is False
        merker = [
            TagRequest(
                tag_id="m_bit",
                address="M0.0",
                value_type=ValueType.BOOLEAN,
                protocol_options={"data_type": "bool"},
            ),
            TagRequest(
                tag_id="m_int",
                address="M2",
                value_type=ValueType.INTEGER,
                protocol_options={"data_type": "int"},
            ),
        ]
        merker_results = {r.tag_id: r for r in await driver.read(merker)}
        assert all(r.quality is Quality.GOOD for r in merker_results.values())

        await asyncio.sleep(1.2)
        second = await read_rows(driver, rows)
        assert raw(second, "Cycles") != raw(first, "Cycles")
        assert raw(second, "Pressure") != raw(first, "Pressure")
    finally:
        await driver.disconnect()


async def test_multi_byte_values_are_never_half_written(endpoint: str) -> None:
    """The coherent Cycles DInt only ever grows while the simulator updates."""
    cycles = next(r for r in documented_rows() if r.name == "Cycles")
    driver = await connected(endpoint)
    try:
        last = -1
        for _ in range(60):
            results = await read_rows(driver, [cycles])
            value = raw(results, "Cycles")
            assert isinstance(value, int)
            assert value >= last
            last = value
    finally:
        await driver.disconnect()


def test_demo_config_tags_match_documented_offsets() -> None:
    config = load_collector_config(DEMO_CONFIG, environ={})
    connection = next(c for c in config.connections if c.id == CONNECTION_ID)
    assert connection.protocol == "s7"
    assert connection.endpoint == "tcp://s7-sim:1102"
    assert connection.protocol_options == {"rack": 0, "slot": 1}
    groups = {g.id for g in config.tag_groups if g.connection_id == CONNECTION_ID}
    assert len(groups) == 1
    tags = [t for t in config.tags if t.tag_group_id in groups]
    documented = {(r.address, r.data_type) for r in documented_rows()}
    db_tags = [t for t in tags if t.address.startswith("DB1.")]
    for tag in db_tags:
        assert (tag.address, tag.protocol_options["data_type"]) in documented, tag.id
    assert {t.protocol_options["data_type"] for t in db_tags} >= {
        "bool",
        "int",
        "dint",
        "real",
        "lreal",
        "string",
    }
    assert sum(t.address.startswith("M") for t in tags) == 2
