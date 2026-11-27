"""Bus access for Panel actions: the running worker's bus, or a temporary one."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from mbus_support import (
    MemoryTransport,
    counting_registry,
    mbus_connection,
    memory_host,
    memory_transport,
    running_host,
)

from plc_data_collector.app.mbus_access import (
    BusAccessRefused,
    BusSettingsDiffer,
    borrow_bus,
)
from plc_data_collector.app.runtime import CollectorRuntime
from plc_data_collector.app.settings import RuntimeSettings
from plc_data_collector.app.supervisor import RuntimeSupervisor
from plc_data_collector.domain import ConnectionConfig
from plc_data_collector.protocols import build_driver_registry
from plc_data_collector.protocols.mbus import MBusDriver
from plc_data_collector.protocols.mbus_bus import BusTransportError
from plc_data_collector.simulators import mbus as simulator

AWAIT_LIMIT_S = 10
DIFFER = (
    "This Connection is running with other settings. Apply your changes first, "
    "or disable the Connection."
)


async def test_running_connection_lends_its_bus() -> None:
    transport = memory_transport(simulator.demo_meters())
    running = mbus_connection(scan_timeout_ms=500, max_telegrams=8)
    async with running_host(transport, running) as (host, driver):
        # Other timeouts and telegram limits are not "other settings".
        form = mbus_connection("tcp://127.0.0.1:10001", scan_timeout_ms=100)
        async with borrow_bus(host, form) as bus:
            assert bus is driver.bus

    assert transport.opens == 1


async def test_running_serial_connection_lends_its_bus_at_the_same_baud_rate() -> None:
    transport = memory_transport(simulator.demo_meters())
    running = mbus_connection("serial:///dev/ttyUSB0", baud_rate=2400)
    async with running_host(transport, running) as (host, driver):
        same = mbus_connection("serial:///dev/ttyUSB0", baud_rate=2400)
        async with borrow_bus(host, same) as bus:
            assert bus is driver.bus


@pytest.mark.parametrize(
    "form",
    [
        mbus_connection("serial:///dev/ttyUSB0", baud_rate=9600),
        mbus_connection("serial:///dev/ttyUSB1", baud_rate=2400),
    ],
)
async def test_running_connection_with_other_settings_refuses(
    form: ConnectionConfig,
) -> None:
    transport = memory_transport(simulator.demo_meters())
    running = mbus_connection("serial:///dev/ttyUSB0", baud_rate=2400)
    async with running_host(transport, running) as (host, _):
        with pytest.raises(BusSettingsDiffer) as caught:
            async with borrow_bus(host, form):
                pass

    assert caught.value.message == DIFFER
    assert caught.value.code == "mbus_bus_settings_differ"
    assert transport.opens == 1


async def test_not_running_opens_and_closes_a_temporary_bus() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    async with borrow_bus(host, mbus_connection()) as bus:
        assert bus.is_open
        assert transport.is_open

    assert not bus.is_open
    assert not transport.is_open
    assert transport.opens == 1


async def test_actions_on_a_stopped_connection_share_one_temporary_bus() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    async with borrow_bus(host, mbus_connection()) as first:
        async with borrow_bus(host, mbus_connection()) as second:
            assert second is first
        assert first.is_open  # still borrowed by the first action

    assert not first.is_open
    assert transport.opens == 1


async def test_temporary_bus_with_other_settings_refuses() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)
    serial = mbus_connection("serial:///dev/ttyUSB0", baud_rate=2400)

    async with borrow_bus(host, serial):
        with pytest.raises(BusSettingsDiffer) as caught:
            other = mbus_connection("serial:///dev/ttyUSB0", baud_rate=9600)
            async with borrow_bus(host, other):
                pass

    assert caught.value.message == (
        "Another action is using this Connection with other settings. Try again "
        "when it has finished."
    )


async def test_temporary_bus_that_cannot_open_is_closed() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    async def refuse() -> None:
        raise BusTransportError("mbus_connect_failed")

    transport.open = refuse  # type: ignore[method-assign]
    with pytest.raises(BusTransportError):
        async with borrow_bus(host, mbus_connection()):
            pass

    # The next action tries a new bus rather than a broken one.
    del transport.open
    async with borrow_bus(host, mbus_connection()) as bus:
        assert bus.is_open


async def test_no_bus_is_lent_while_changes_are_applied() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)
    host.applying = True

    with pytest.raises(BusAccessRefused) as caught:
        async with borrow_bus(host, mbus_connection()):
            pass
    with pytest.raises(BusAccessRefused):
        host.mbus_scans.start(mbus_connection(), "primary")

    assert caught.value.code == "apply_in_progress"
    assert transport.opens == 0


async def test_cancel_all_closes_temporary_buses_of_other_actions() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    async with borrow_bus(host, mbus_connection()) as bus:
        await host.mbus_scans.cancel_all("Scan stopped: the configuration changed.")
        assert not bus.is_open
        with pytest.raises(BusTransportError):
            async with bus.turn():
                pass


# The supervisor: an apply stops scans on temporary buses first


class _ExclusivePort:
    """A port only one transport can hold open at a time (like a serial port)."""

    def __init__(self) -> None:
        self.bus = simulator.SimulatedBus(simulator.demo_meters(), clock=lambda: 0.0)
        self.holder: MemoryTransport | None = None
        self.gate = asyncio.Event()  # scans wait here, so one is always running

    def transport(self) -> MemoryTransport:
        port = self
        gate = self.gate

        class _Exclusive(MemoryTransport):
            async def open(self) -> None:
                if port.holder is not None:
                    raise BusTransportError("mbus_connect_failed")
                port.holder = self
                await super().open()

            async def close(self) -> None:
                if port.holder is self:
                    port.holder = None
                await super().close()

        transport = _Exclusive(self.bus)

        async def hold_scans(frame: object) -> None:
            await gate.wait()

        transport.on_write = hold_scans
        return transport


def _mbus_config(*, enabled: bool) -> dict[str, object]:
    return {
        "connections": [
            {
                "id": "heat",
                "protocol": "mbus",
                "endpoint": "serial:///dev/ttyUSB0",
                "enabled": enabled,
                "timeout_ms": 200,
                "protocol_options": {"scan_timeout_ms": 50},
            }
        ],
        "tag_groups": [
            {
                "id": "meters",
                "connection_id": "heat",
                "interval_ms": 50,
                "timeout_ms": 1000,
            }
        ],
        "tags": [
            {
                "id": "heat_volume",
                "tag_group_id": "meters",
                "name": "Volume",
                "address": "2",
                "value_type": "numeric",
                "protocol_options": {"quantity": "volume", "unit": "m3"},
            }
        ],
    }


async def test_apply_stops_a_temporary_bus_scan_and_the_worker_connects(
    tmp_path: Path,
) -> None:
    port = _ExclusivePort()
    workers: list[MBusDriver] = []

    def create(connection: ConnectionConfig) -> MBusDriver:
        transport = port.transport()
        driver = MBusDriver(connection, transport_factory=lambda: transport)
        workers.append(driver)
        return driver

    config_file = tmp_path / "collector.config.json"
    config_file.write_text(json.dumps(_mbus_config(enabled=False)), encoding="utf-8")
    settings = RuntimeSettings.from_env(
        {},
        data_dir=tmp_path / "data",
        config_file=config_file,
        database_wait_timeout_s=2.0,
        storage_timeout_s=1.0,
    )
    supervisor = RuntimeSupervisor(
        settings, registry=build_driver_registry([("mbus", create)])
    )
    await supervisor.start()
    try:
        stores = supervisor.stores
        assert stores is not None
        saved = stores.config.get_connection("heat")
        assert saved is not None
        assert supervisor.mbus_bus("heat") is None  # disabled: no worker

        job = supervisor.mbus_scans.start(saved, "primary")
        await asyncio.sleep(0.05)
        assert port.holder is not None  # the scan's temporary bus holds the port
        assert not job.progress.finished

        enabled = ConnectionConfig(
            id=saved.id,
            protocol=saved.protocol,
            endpoint=saved.endpoint,
            enabled=True,
            timeout_ms=saved.timeout_ms,
            protocol_options=saved.protocol_options,
        )
        stores.config.upsert_connection(enabled)
        workers.clear()
        port.gate.set()  # the worker's polls must not wait
        await asyncio.wait_for(supervisor.apply(), timeout=AWAIT_LIMIT_S)

        assert job.progress.finished
        assert job.progress.message == "Scan stopped: the configuration changed."
        bus = supervisor.mbus_bus("heat")
        assert bus is not None
        assert bus.is_open  # the new worker opened the port
        (driver,) = workers
        assert bus is driver.bus
    finally:
        await supervisor.stop()


# Another Connection's port

PORT_IN_USE = (
    "This port is in use by Connection 'heat'. Use that Connection, or disable "
    "it first."
)


def _other(endpoint: str, **options: object) -> ConnectionConfig:
    """Connection ``cold`` (not running) on ``endpoint``."""
    return dataclasses.replace(mbus_connection(endpoint, **options), id="cold")


@pytest.mark.parametrize(
    ("running_endpoint", "other_endpoint"),
    [
        ("serial:///dev/ttyUSB0", "serial:///dev/ttyUSB0"),
        ("tcp://Gateway.local:10001", "tcp://gateway.local:10001"),
    ],
)
async def test_port_of_a_running_connection_is_not_opened_again(
    running_endpoint: str, other_endpoint: str
) -> None:
    transport = memory_transport(simulator.demo_meters())
    async with running_host(transport, mbus_connection(running_endpoint)) as (
        host,
        _,
    ):
        built = counting_registry(host, transport)
        with pytest.raises(BusAccessRefused) as caught:
            async with borrow_bus(host, _other(other_endpoint, baud_rate=9600)):
                pass
        with pytest.raises(BusAccessRefused) as scan:
            host.mbus_scans.start(_other(other_endpoint), "primary")

        assert built == []  # no transport factory was asked for
        assert transport.opens == 1

    for error in (caught.value, scan.value):
        assert error.message == PORT_IN_USE
        assert error.code == "bus_in_use"


async def test_port_of_another_temporary_bus_is_not_opened_again() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)
    built = counting_registry(host, transport)

    async with borrow_bus(host, mbus_connection("serial:///dev/ttyUSB0")):
        with pytest.raises(BusAccessRefused) as caught:
            async with borrow_bus(host, _other("serial:///dev/ttyUSB0")):
                pass
        with pytest.raises(BusAccessRefused):
            host.mbus_scans.start(_other("serial:///dev/ttyUSB0"), "secondary")

    assert caught.value.message == PORT_IN_USE
    assert caught.value.code == "bus_in_use"
    assert built == ["heat"]
    assert transport.opens == 1


async def test_another_port_gets_its_own_temporary_bus() -> None:
    transport = memory_transport(simulator.demo_meters())
    running = mbus_connection("serial:///dev/ttyUSB0")
    async with running_host(transport, running) as (host, _):
        other = _other("serial:///dev/ttyUSB1")
        async with borrow_bus(host, other) as bus:
            assert bus.is_open


# The collector starting or stopping

NOT_READY = "The collector is starting or stopping. Try again in a moment."


async def test_no_bus_is_lent_while_the_collector_is_not_running() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)
    host.accepting_bus_actions = False

    with pytest.raises(BusAccessRefused) as caught:
        async with borrow_bus(host, mbus_connection()):
            pass
    with pytest.raises(BusAccessRefused) as scan:
        host.mbus_scans.start(mbus_connection(), "primary")

    for error in (caught.value, scan.value):
        assert error.message == NOT_READY
        assert error.code == "collector_not_ready"
    assert transport.opens == 0


def _settings(tmp_path: Path, *, enabled: bool) -> RuntimeSettings:
    config_file = tmp_path / "collector.config.json"
    config_file.write_text(json.dumps(_mbus_config(enabled=enabled)), encoding="utf-8")
    return RuntimeSettings.from_env(
        {},
        data_dir=tmp_path / "data",
        config_file=config_file,
        database_wait_timeout_s=2.0,
        storage_timeout_s=1.0,
    )


async def test_supervisor_lends_no_bus_while_starting_or_after_stop(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    starting = asyncio.Event()

    class _SlowRuntime(CollectorRuntime):
        async def start(self) -> None:
            starting.set()
            await gate.wait()  # the first workers are still connecting
            await super().start()

    transport = memory_transport(simulator.demo_meters())
    registry = build_driver_registry(
        [("mbus", lambda c: MBusDriver(c, transport_factory=lambda: transport))]
    )
    supervisor = RuntimeSupervisor(
        _settings(tmp_path, enabled=False),
        registry=registry,
        runtime_factory=_SlowRuntime,
    )
    connection = mbus_connection("serial:///dev/ttyUSB0")
    assert supervisor.accepting_bus_actions is False  # not started yet

    start = asyncio.create_task(supervisor.start())
    try:
        await asyncio.wait_for(starting.wait(), timeout=AWAIT_LIMIT_S)
        assert supervisor.accepting_bus_actions is False
        with pytest.raises(BusAccessRefused) as caught:
            async with borrow_bus(supervisor, connection):
                pass
        assert caught.value.code == "collector_not_ready"
        with pytest.raises(BusAccessRefused):
            supervisor.mbus_scans.start(connection, "primary")

        gate.set()
        await asyncio.wait_for(start, timeout=AWAIT_LIMIT_S)
        assert supervisor.accepting_bus_actions is True
        async with borrow_bus(supervisor, connection) as bus:
            assert bus.is_open  # a temporary bus: the Connection is disabled
    finally:
        gate.set()
        await supervisor.stop()
        await asyncio.gather(start, return_exceptions=True)

    assert supervisor.accepting_bus_actions is False
    with pytest.raises(BusAccessRefused) as after:
        supervisor.mbus_scans.start(connection, "primary")
    assert after.value.code == "collector_not_ready"
    assert transport.opens == 1  # only the temporary bus while running
