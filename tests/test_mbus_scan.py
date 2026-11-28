"""Scan bus: the primary and secondary scan jobs, taking turns with polling."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from mbus_support import (
    MemoryTransport,
    mbus_connection,
    memory_host,
    memory_transport,
    running_host,
)

from plc_data_collector.app.mbus_access import BusSettingsDiffer
from plc_data_collector.app.mbus_scan import FoundMeter, ScanAlreadyRunning, ScanJob
from plc_data_collector.domain import TagRequest, ValueType
from plc_data_collector.protocols.mbus_codec import LongFrame, ShortFrame
from plc_data_collector.simulators import mbus as simulator

AWAIT_LIMIT_S = 10
SND_NKE = 0x40


async def _finish(job: ScanJob) -> ScanJob:
    await asyncio.wait_for(job.wait(), timeout=AWAIT_LIMIT_S)
    assert job.progress.finished
    return job


def _ids(meters: tuple[FoundMeter, ...]) -> list[str]:
    return sorted(meter.header.id_digits for meter in meters)


def _patterns(transport: MemoryTransport) -> list[str]:
    """The ID digits of every secondary selection, in order."""
    return [
        frame.payload[:4][::-1].hex().upper()
        for frame in transport.frames
        if isinstance(frame, LongFrame) and frame.ci == 0x52
    ]


# Primary scan


async def test_primary_scan_finds_the_primary_meters() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    job = await _finish(host.mbus_scans.start(mbus_connection(), "primary"))

    progress = job.progress
    assert progress.total == 251
    assert progress.done == 251
    assert progress.found == 4
    assert progress.message is None
    assert [(m.primary, m.header.id_digits) for m in job.meters] == [
        (1, "12345678"),
        (2, "12399999"),
        (3, "22223333"),
        (5, "55667788"),
    ]
    assert job.notes == ()
    # Every address was probed with the scan reply timeout's SND_NKE.
    probed = [
        f.address
        for f in transport.frames
        if isinstance(f, ShortFrame) and f.control == SND_NKE
    ]
    assert probed == list(range(251))
    # The temporary bus was opened once and closed at the end.
    assert transport.opens == 1
    assert not transport.is_open


async def test_primary_scan_reports_a_shared_primary_address() -> None:
    meters = simulator.demo_meters()
    meters[0] = dataclasses.replace(meters[0], primary=7)
    meters[1] = dataclasses.replace(meters[1], primary=7)
    host = memory_host(memory_transport(meters))

    job = await _finish(host.mbus_scans.start(mbus_connection(), "primary"))

    assert job.notes == (
        "Two or more meters share primary address 7; use secondary addresses.",
    )
    assert [m.primary for m in job.meters] == [3, 5]


# Secondary scan


async def test_secondary_scan_finds_every_meter() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    job = await _finish(host.mbus_scans.start(mbus_connection(), "secondary"))

    assert job.progress.total is None
    assert job.progress.message is None
    assert _ids(job.meters) == [
        "12345678",
        "12399999",
        "22223333",
        "55667788",
        "87654321",  # meter 4: primary 0, only a secondary scan finds it
    ]
    assert all(meter.primary is None for meter in job.meters)
    meter_4 = next(m for m in job.meters if m.header.id_digits == "87654321")
    assert meter_4.header.manufacturer == "PDC"
    patterns = _patterns(transport)
    # Meters 1 and 2 collide on 1, 12 and 123; 1234 and 1239 are single.
    for pattern in ("1FFFFFFF", "12FFFFFF", "123FFFFF", "1234FFFF", "1239FFFF"):
        assert pattern in patterns
    assert "1235FFFF" in patterns  # silent digits of the collision are probed
    assert "13FFFFFF" in patterns
    assert "2FFFFFFF" in patterns
    assert "22FFFFFF" not in patterns  # a single meter is not searched further
    assert job.progress.done == len(patterns)


async def test_secondary_scan_notes_a_shared_id() -> None:
    meters = simulator.demo_meters()[2:4]  # IDs 22223333 and 87654321
    meters.append(dataclasses.replace(meters[0], primary=9, manufacturer="ABC"))
    host = memory_host(memory_transport(meters))

    job = await _finish(host.mbus_scans.start(mbus_connection(), "secondary"))

    assert _ids(job.meters) == ["87654321"]
    assert job.notes == (
        "Two or more meters share the ID 22223333; give their Tags the full "
        "secondary address (ID, manufacturer, version and medium).",
    )


async def test_secondary_scan_deselects_after_each_meter() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)

    await _finish(host.mbus_scans.start(mbus_connection(), "secondary"))

    frames = transport.frames
    for index, frame in enumerate(frames):
        if isinstance(frame, ShortFrame) and frame.control & 0x4F == 0x4B:
            # The header read of a found meter is followed by a deselect.
            following = frames[index + 1]
            assert isinstance(following, ShortFrame)
            assert (following.control, following.address) == (SND_NKE, 253)


# Cancel, refusals, errors


async def test_cancel_stops_after_the_current_probe() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)
    job: ScanJob | None = None

    async def cancel_while_probing_5(frame: object) -> None:
        if isinstance(frame, ShortFrame) and (frame.control, frame.address) == (
            SND_NKE,
            5,
        ):
            assert job is not None
            job.cancel()

    transport.on_write = cancel_while_probing_5
    job = host.mbus_scans.start(mbus_connection(), "primary")
    await _finish(job)

    progress = job.progress
    # The probe of address 5 (and its header read) finished; nothing after.
    assert progress.done == 6
    assert progress.message == "Scan cancelled."
    assert [m.primary for m in job.meters] == [1, 2, 3, 5]
    probed = [
        f.address
        for f in transport.frames
        if isinstance(f, ShortFrame) and f.control == SND_NKE
    ]
    assert probed == [0, 1, 2, 3, 4, 5]
    assert not transport.is_open


async def test_a_second_scan_on_one_connection_is_refused() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)
    first = host.mbus_scans.start(mbus_connection(), "primary")

    with pytest.raises(ScanAlreadyRunning):
        host.mbus_scans.start(mbus_connection(), "secondary")

    assert host.mbus_scans.get("heat") is first
    await _finish(first)
    # A finished scan makes room for the next one.
    second = await _finish(host.mbus_scans.start(mbus_connection(), "secondary"))
    assert host.mbus_scans.get("heat") is second


async def test_scan_refused_when_running_with_other_settings() -> None:
    transport = memory_transport(simulator.demo_meters())
    running = mbus_connection("serial:///dev/ttyUSB0", baud_rate=2400)
    async with running_host(transport, running) as (host, _):
        with pytest.raises(BusSettingsDiffer) as caught:
            host.mbus_scans.start(
                mbus_connection("serial:///dev/ttyUSB0", baud_rate=9600), "primary"
            )

    assert caught.value.message == (
        "This Connection is running with other settings. Apply your changes "
        "first, or disable the Connection."
    )


async def test_transport_error_stops_the_scan_with_its_code() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)
    writes = 0

    async def lose_after_five(frame: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 5:
            transport.lose = True

    transport.on_write = lose_after_five
    job = await _finish(host.mbus_scans.start(mbus_connection(), "primary"))

    assert job.progress.message == (
        "Scan stopped: The M-Bus connection was lost. (mbus_connection_lost)"
    )
    assert not transport.is_open


async def test_cancel_all_stops_scans_with_the_message() -> None:
    transport = memory_transport(simulator.demo_meters())
    host = memory_host(transport)
    gate = asyncio.Event()

    async def hold(frame: object) -> None:
        await gate.wait()  # never set: only cancel_all ends the scan

    transport.on_write = hold
    job = host.mbus_scans.start(mbus_connection(), "primary")
    await asyncio.sleep(0.01)
    assert not job.progress.finished

    await asyncio.wait_for(
        host.mbus_scans.cancel_all("Scan stopped: the configuration changed."),
        timeout=AWAIT_LIMIT_S,
    )

    assert job.progress.finished
    assert job.progress.message == "Scan stopped: the configuration changed."
    assert not transport.is_open  # the temporary bus is closed


# Sharing the running bus


async def test_scan_takes_turns_with_polling() -> None:
    transport = memory_transport(simulator.demo_meters())
    connection = mbus_connection()
    async with running_host(transport, connection) as (host, driver):
        job = host.mbus_scans.start(connection, "primary")
        tag = TagRequest(
            tag_id="volume",
            address="2",
            value_type=ValueType.NUMERIC,
            protocol_options={"quantity": "volume", "unit": "m3"},
        )
        delays: list[int] = []
        polls = 0
        while not job.progress.finished:
            before = job.progress.done
            (result,) = await asyncio.wait_for(driver.read([tag]), AWAIT_LIMIT_S)
            assert result.value is not None, result.error_message
            delays.append(job.progress.done - before)
            polls += 1

        await _finish(job)

    # Polling went on during the scan, waiting for one probe at most.
    assert polls >= 10
    assert max(delays) <= 1
    assert job.progress.done == 251
    assert _ids(job.meters) == ["12345678", "12399999", "22223333", "55667788"]
    # The scan used the worker's bus: no second link was opened.
    assert transport.opens == 1
