from __future__ import annotations

import asyncio
import random
import struct
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from modbus_support import free_port

from plc_data_collector.app.config import load_collector_config
from plc_data_collector.domain import (
    ConnectionConfig,
    Quality,
    TagConfig,
    TagGroupConfig,
    TagRequest,
    TagResult,
    ValueType,
)
from plc_data_collector.protocols import create_modbus_driver
from plc_data_collector.protocols.modbus import ModbusDriver
from plc_data_collector.simulators.modbus import run_simulator, simulated_registers

DEMO_CONFIG = Path(__file__).parents[1] / "deploy" / "demo" / "collector.config.json"
CONNECTION_ID = "press2_modbus"


def _float32(words: list[int] | list[bool], *, low_first: bool = False) -> float:
    pair = [int(w) for w in words]
    if low_first:
        pair.reverse()
    value: float = struct.unpack(">f", struct.pack(">HH", *pair))[0]
    return value


def test_simulated_registers_ranges() -> None:
    for elapsed in range(0, 301):
        regs = simulated_registers(float(elapsed), random.Random(elapsed))
        assert 129.5 <= _float32(regs[(1, "hr", 100)]) <= 170.5
        assert 39.5 <= _float32(regs[(1, "hr", 102)]) <= 50.5
        assert regs[(1, "hr", 104)] == [0, elapsed // 2]
        ram = regs[(1, "hr", 106)][0]
        assert isinstance(ram, int)
        assert 0 <= ram <= 1000
        status = regs[(1, "hr", 107)][0]
        assert isinstance(status, int)
        assert status & 0b1000
        assert bool(status & 0b01) != bool(status & 0b10)
        assert 0.0 <= _float32(regs[(1, "hr", 110)], low_first=True) <= 20.0
        assert regs[(1, "hr", 120)] == [0x5243, 0x502D, 0x3030, 0x3432]
        assert regs[(1, "co", 0)][0] is True
        assert regs[(1, "di", 0)][0] is True
        assert regs[(1, "di", 7)][0] is True
        assert 227.5 <= _float32(regs[(2, "ir", 0)]) <= 232.5
        assert 0.0 <= _float32(regs[(2, "ir", 2)]) <= 30.0
        energy = regs[(2, "ir", 100)]
        assert len(energy) == 4 and energy[2:] == [0, 0]


def test_simulated_registers_change_over_time() -> None:
    first = simulated_registers(0.0, random.Random(1))
    later = simulated_registers(20.0, random.Random(1))
    assert first[(1, "hr", 104)] != later[(1, "hr", 104)]
    assert first[(2, "ir", 100)] != later[(2, "ir", 100)]


def test_demo_config_valid() -> None:
    config = load_collector_config(DEMO_CONFIG, environ={})

    connection = next(c for c in config.connections if c.id == CONNECTION_ID)
    assert connection.protocol == "modbus"
    assert connection.endpoint == "tcp://modbus-sim:5020"
    assert any(c.protocol == "opcua" for c in config.connections)
    tags = _modbus_tags(config.tags, config.tag_groups)
    assert {t.address for t in tags} >= {
        "hr:100",
        "hr:102",
        "hr:104",
        "hr:106",
        "hr:107.1",
        "hr:110",
        "hr:120",
        "co:0",
        "di:0",
        "ir:0",
        "ir:100",
    }


def _modbus_tags(
    tags: tuple[TagConfig, ...], groups: tuple[TagGroupConfig, ...]
) -> list[TagConfig]:
    group_ids = {g.id for g in groups if g.connection_id == CONNECTION_ID}
    return [t for t in tags if t.tag_group_id in group_ids]


@pytest.fixture
async def driver() -> AsyncIterator[ModbusDriver]:
    config = load_collector_config(DEMO_CONFIG, environ={})
    base = next(c for c in config.connections if c.id == CONNECTION_ID)
    port = free_port()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        run_simulator(
            host="127.0.0.1",
            port=port,
            update_interval_s=0.05,
            stop_event=stop_event,
        )
    )
    modbus_driver = create_modbus_driver(
        ConnectionConfig(
            id=base.id,
            protocol="modbus",
            endpoint=f"tcp://127.0.0.1:{port}",
            timeout_ms=2000,
            protocol_options=base.protocol_options,
        )
    )
    try:
        for _ in range(100):
            try:
                await modbus_driver.connect()
            except Exception:
                await asyncio.sleep(0.05)
            else:
                break
        else:
            pytest.fail("simulator did not start")
        yield modbus_driver
    finally:
        await modbus_driver.disconnect()
        stop_event.set()
        await asyncio.wait_for(task, timeout=5)


async def _read_demo_tags(driver: ModbusDriver) -> dict[str, TagResult]:
    config = load_collector_config(DEMO_CONFIG, environ={})
    tags = _modbus_tags(config.tags, config.tag_groups)
    requests = [
        TagRequest(
            tag_id=t.id,
            address=t.address,
            value_type=t.value_type,
            protocol_options=t.protocol_options,
        )
        for t in tags
    ]
    return {r.tag_id: r for r in await driver.read(requests)}


async def test_simulator_serves_map_to_driver(driver: ModbusDriver) -> None:
    results = await _read_demo_tags(driver)

    assert results
    for result in results.values():
        assert result.error_code is None, result.error_message
        assert result.quality is Quality.GOOD
        assert result.value is not None
    recipe = results["press2_recipe"].value
    assert recipe is not None and recipe.text_value == "RCP-0042"


async def test_simulator_values_change_while_running(driver: ModbusDriver) -> None:
    first = (await _read_demo_tags(driver))["press2_cycle_counter"]
    await asyncio.sleep(2.2)
    second = (await _read_demo_tags(driver))["press2_cycle_counter"]
    assert first.value is not None and second.value is not None
    assert (second.value.integer_value or 0) > (first.value.integer_value or 0)


async def test_simulator_rejects_undefined_addresses(driver: ModbusDriver) -> None:
    result = (
        await driver.read(
            [
                TagRequest(
                    tag_id="outside",
                    address="hr:128",
                    value_type=ValueType.INTEGER,
                    protocol_options={"data_type": "uint16"},
                )
            ]
        )
    )[0]
    assert result.error_code is not None
