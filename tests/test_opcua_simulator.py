from __future__ import annotations

import asyncio
import random
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from plc_data_collector.app.config import load_collector_config
from plc_data_collector.domain import ConnectionConfig, TagRequest, TagResult, ValueType
from plc_data_collector.protocols import OpcUaDriver, create_opcua_driver
from plc_data_collector.simulators.opcua import (
    ENDPOINT_PATH,
    NAMESPACE_URI,
    run_simulator,
    simulated_values,
)

TEMPERATURE = "ns=2;s=Line1.Temperature"
PRESSURE = "ns=2;s=Line1.Pressure"
PART_COUNT = "ns=2;s=Line1.PartCount"
MACHINE_STATE = "ns=2;s=Line1.MachineState"
DEMO_CONFIG = Path(__file__).parents[1] / "deploy" / "demo" / "collector.config.json"

REQUESTS = (
    TagRequest("temperature", TEMPERATURE, ValueType.NUMERIC),
    TagRequest("pressure", PRESSURE, ValueType.NUMERIC),
    TagRequest("part_count", PART_COUNT, ValueType.INTEGER),
    TagRequest("machine_state", MACHINE_STATE, ValueType.TEXT),
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def driver() -> AsyncIterator[OpcUaDriver]:
    port = _free_port()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        run_simulator(
            host="127.0.0.1",
            port=port,
            update_interval_s=0.05,
            stop_event=stop_event,
        )
    )
    opcua_driver = create_opcua_driver(
        ConnectionConfig(
            id="sim",
            protocol="opcua",
            endpoint=f"opc.tcp://127.0.0.1:{port}{ENDPOINT_PATH}",
            timeout_ms=3000,
        )
    )
    try:
        for _ in range(100):
            try:
                await opcua_driver.connect()
            except Exception:
                await asyncio.sleep(0.05)
            else:
                break
        else:
            pytest.fail("simulator did not start")
        yield opcua_driver
    finally:
        await opcua_driver.disconnect()
        stop_event.set()
        await asyncio.wait_for(task, timeout=5)


def _ok(result: TagResult) -> TagResult:
    assert result.error_code is None, result.error_message
    assert result.value is not None
    return result


async def _read(driver: OpcUaDriver) -> list[TagResult]:
    return [_ok(r) for r in await driver.read(REQUESTS)]


async def test_simulator_serves_all_demo_tags(driver: OpcUaDriver) -> None:
    temperature, pressure, part_count, state = await _read(driver)

    assert temperature.value is not None
    assert isinstance(temperature.value.numeric_value, float)
    assert pressure.value is not None
    assert isinstance(pressure.value.numeric_value, float)
    assert part_count.value is not None
    assert isinstance(part_count.value.integer_value, int)
    assert state.value is not None
    assert isinstance(state.value.text_value, str)


async def test_simulator_values_stay_in_range(driver: OpcUaDriver) -> None:
    for _ in range(20):
        temperature, pressure, _count, _state = await _read(driver)
        assert temperature.value is not None
        assert pressure.value is not None
        assert 59.5 <= (temperature.value.numeric_value or 0.0) <= 80.5
        assert 3.95 <= (pressure.value.numeric_value or 0.0) <= 6.05
        await asyncio.sleep(0.02)


async def test_part_count_increases_while_running(driver: OpcUaDriver) -> None:
    counts: list[int] = []
    for _ in range(3):
        _t, _p, part_count, state = await _read(driver)
        assert state.value is not None
        assert state.value.text_value == "RUNNING"
        assert part_count.value is not None
        assert part_count.value.integer_value is not None
        counts.append(part_count.value.integer_value)
        await asyncio.sleep(0.3)

    assert counts[0] < counts[1] < counts[2]


async def test_simulator_stops_cleanly_on_stop_event() -> None:
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        run_simulator(
            host="127.0.0.1",
            port=_free_port(),
            update_interval_s=0.05,
            stop_event=stop_event,
        )
    )
    await asyncio.sleep(0.5)
    stop_event.set()
    await asyncio.wait_for(task, timeout=5)
    assert task.exception() is None


def test_namespace_constants() -> None:
    assert NAMESPACE_URI == "urn:plc-data-collector:sim"
    assert ENDPOINT_PATH == "/plc-data-collector/sim/"


def test_demo_config_loads() -> None:
    config = load_collector_config(DEMO_CONFIG, environ={})

    assert {tag.address for tag in config.tags} == {
        TEMPERATURE,
        PRESSURE,
        PART_COUNT,
        MACHINE_STATE,
    }
    assert config.connections[0].id == "line1_opcua"
    assert config.connections[0].endpoint == (
        "opc.tcp://opcua-sim:4840/plc-data-collector/sim/"
    )
    assert config.tag_groups[0].interval_ms == 1000


@pytest.mark.parametrize(
    ("elapsed_s", "state"),
    [
        (0.0, "RUNNING"),
        (39.9, "RUNNING"),
        (40.0, "IDLE"),
        (49.9, "IDLE"),
        (50.0, "FAULT"),
        (54.9, "FAULT"),
        (55.0, "RUNNING"),
        (55.0 * 7 + 41.0, "IDLE"),
    ],
)
def test_state_cycle(elapsed_s: float, state: str) -> None:
    assert simulated_values(elapsed_s, random.Random(1)).machine_state == state


def test_simulated_values_are_in_range_and_deterministic() -> None:
    for elapsed in range(0, 600):
        values = simulated_values(float(elapsed), random.Random(elapsed))
        assert 59.5 <= values.temperature <= 80.5
        assert 3.95 <= values.pressure <= 6.05
        assert values == simulated_values(float(elapsed), random.Random(elapsed))


def test_simulated_values_follow_sine_periods() -> None:
    rng_a, rng_b = random.Random(3), random.Random(3)
    first = simulated_values(30.0, rng_a)  # quarter of the 120 s period: peak
    second = simulated_values(30.0 + 120.0, rng_b)
    assert first.temperature == pytest.approx(second.temperature)
    assert first.temperature == pytest.approx(80.0, abs=0.5)
