"""Tests for the Panel's M-Bus pages: Scan bus and Read meter."""

from __future__ import annotations

import asyncio
import dataclasses
import html
import logging
import re
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from mbus_support import MemoryTransport, memory_transport
from panel_support import FakeHost

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import (
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.panel.mbus_read import (
    MAX_SAVE_ROWS,
    check_save,
    record_rows,
    suggested_name,
)
from plc_data_collector.protocols import build_driver_registry
from plc_data_collector.protocols.mbus import MBusDriver
from plc_data_collector.protocols.mbus_bus import MBusBus, MeterReading
from plc_data_collector.protocols.mbus_codec import MBusRecord, MeterHeader
from plc_data_collector.protocols.mbus_tags import (
    UNIT_FAMILIES,
    Function,
    Quantity,
    RecordKey,
)
from plc_data_collector.simulators import mbus as simulator

HEAT = ConnectionConfig(
    id="heat",
    name="Heat meters",
    protocol="mbus",
    endpoint="tcp://gw.example:10001",
    timeout_ms=200,
    protocol_options={"scan_timeout_ms": 50},
)
OTHER = ConnectionConfig(
    id="water", name="Water", protocol="mbus", endpoint="tcp://gw2.example:10001"
)
BASE = "/panel/connections/heat/mbus"
SCAN_URL = f"{BASE}/scan"
READ_URL = f"{BASE}/read"
SAVE_URL = f"{BASE}/read/save"
WAIT_S = 10.0
SETTINGS_DIFFER = (
    "This Connection is running with other settings. Apply your changes first, "
    "or disable the Connection."
)


def text(page: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)))


def _stores(host: FakeHost) -> Stores:
    assert host.stores is not None
    return host.stores


def _seed(host: FakeHost, *, interval_ms: int = 900_000) -> Stores:
    stores = _stores(host)
    stores.config.upsert_connection(HEAT)
    stores.config.upsert_tag_group(
        TagGroupConfig(
            id="meters",
            connection_id="heat",
            interval_ms=interval_ms,
            timeout_ms=min(interval_ms, 30_000),
        )
    )
    return stores


def _memory_bus(host: FakeHost) -> MemoryTransport:
    """Make every M-Bus driver (and so every temporary bus) use one memory bus."""
    transport = memory_transport(simulator.demo_meters())
    host.registry = build_driver_registry(
        [("mbus", lambda c: MBusDriver(c, transport_factory=lambda: transport))]
    )
    return transport


def _mbus_tag(
    tag_id: str,
    *,
    address: str = "12345678",
    quantity: str = "energy",
    unit: str | None = "MWh",
    storage: int = 0,
    meter_id: str | None = None,
    group: str = "meters",
) -> TagConfig:
    options: dict[str, object] = {"quantity": quantity}
    if unit is not None:
        options["unit"] = unit
    if storage:
        options["storage"] = storage
    if meter_id is not None:
        options["meter_id"] = meter_id
    return TagConfig(
        id=tag_id,
        tag_group_id=group,
        name=tag_id,
        address=address,
        value_type=ValueType.NUMERIC,
        metadata={"unit": unit} if unit else {},
        protocol_options=options,
    )


def _token(page: str) -> str:
    match = re.search(r'name="token" value="([^"]+)"', page)
    assert match is not None, page
    return match.group(1)


def _shown_ids(page: str) -> str:
    match = re.search(r'name="shown_ids" value="([^"]*)"', page)
    assert match is not None
    return html.unescape(match.group(1))


def _rows(page: str) -> dict[str, str]:
    """Row index -> the row's HTML, from the records table."""
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r'<tr data-row="(\d+)"[^>]*>(.*?)</tr>', page, re.S)
    }


def _checked(page: str) -> set[str]:
    return set(re.findall(r'name="select" value="(\d+)" checked', page))


def _read(client: TestClient, meter: str) -> tuple[int, str]:
    response = client.post(READ_URL, data={"meter": meter})
    return response.status_code, response.text


def _save(
    client: TestClient,
    page: str,
    selected: list[str],
    *,
    action: str = "save",
    prefix: str | None = None,
    group: str = "meters",
    units: dict[str, str] | None = None,
) -> tuple[int, str, str | None]:
    fields: list[tuple[str, str]] = [
        ("token", _token(page)),
        ("shown_ids", _shown_ids(page)),
        ("prefix", _prefix(page) if prefix is None else prefix),
        ("tag_group", group),
        ("action", action),
    ]
    fields += [("select", index) for index in selected]
    for index, unit in (units or {}).items():
        fields.append((f"unit_{index}", unit))
    data: dict[str, list[str]] = {}
    for name, value in fields:
        data.setdefault(name, []).append(value)
    response = client.post(SAVE_URL, data=data)
    return response.status_code, response.text, response.headers.get("location")


def _prefix(page: str) -> str:
    match = re.search(r'name="prefix" id="read-prefix" value="([^"]*)"', page)
    assert match is not None
    return html.unescape(match.group(1))


def _active_tags(stores: Stores) -> dict[str, TagConfig]:
    return {tag.id: tag for tag, _ in stores.config.active_tags()}


# Scan bus


def _wait_finished(client: TestClient) -> str:
    deadline = time.monotonic() + WAIT_S
    while True:
        fragment: str = client.get(f"{SCAN_URL}/progress").text
        if 'hx-trigger="every 1s"' not in fragment:
            return fragment
        assert time.monotonic() < deadline, fragment
        time.sleep(0.02)


def test_scan_page_offers_primary_and_secondary(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    page = signed_in_client.get(SCAN_URL)

    assert page.status_code == 200
    assert 'name="kind" value="primary"' in page.text
    assert 'name="kind" value="secondary"' in page.text
    assert f'action="{SCAN_URL}"' in page.text


def test_secondary_scan_progress_then_results_with_read_links(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    _memory_bus(fake_host)
    # Meter 3 already has a Tag on this Connection (read by primary address).
    stores.config.create_tag(
        _mbus_tag(
            "heat_power", address="3", quantity="power", unit="W", meter_id="22223333"
        )
    )

    started = signed_in_client.post(SCAN_URL, data={"kind": "secondary"})
    assert started.status_code == 303
    assert started.headers["location"] == SCAN_URL
    page = signed_in_client.get(SCAN_URL).text
    assert 'id="scan-progress"' in page

    fragment = _wait_finished(signed_in_client)

    assert "hx-get" not in fragment
    found = dict(
        re.findall(r'<tr data-meter="(\d{8})"[^>]*>(.*?)</tr>', fragment, re.S)
    )
    assert sorted(found) == [
        "12345678",
        "12399999",
        "22223333",
        "55667788",
        "87654321",
    ]
    for meter_id, row in found.items():
        assert f'href="{READ_URL}?meter={meter_id}"' in row
        assert "PDC" in row
    assert "heat" in text(found["12345678"])
    assert "Has tags" in text(found["22223333"])
    assert "Has tags" not in text(found["12345678"])
    assert "5 meters found" in text(fragment)


def test_primary_scan_shows_primary_addresses(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    _memory_bus(fake_host)
    stores.config.create_tag(
        _mbus_tag("heat_m5", address="5", quantity="volume", unit="L")
    )

    signed_in_client.post(SCAN_URL, data={"kind": "primary"})
    fragment = _wait_finished(signed_in_client)

    rows = dict(re.findall(r'<tr data-meter="(\d{8})"[^>]*>(.*?)</tr>', fragment, re.S))
    assert sorted(rows) == ["12345678", "12399999", "22223333", "55667788"]
    assert re.search(r"<td[^>]*>\s*5\s*</td>", rows["55667788"])
    assert "251 of 251" in text(fragment)
    assert "Has tags" in text(rows["55667788"])
    assert "Has tags" not in text(rows["12345678"])


def test_scan_notes_are_shown(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    meters = simulator.demo_meters()
    meters[0] = dataclasses.replace(meters[0], primary=7)
    meters[1] = dataclasses.replace(meters[1], primary=7)
    transport = memory_transport(meters)
    fake_host.registry = build_driver_registry(
        [("mbus", lambda c: MBusDriver(c, transport_factory=lambda: transport))]
    )

    signed_in_client.post(SCAN_URL, data={"kind": "primary"})
    fragment = _wait_finished(signed_in_client)

    assert (
        "Two or more meters share primary address 7; use secondary addresses."
        in text(fragment)
    )


def test_second_scan_is_refused_and_cancel_stops_the_first(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    transport = _memory_bus(fake_host)
    gate_open = False

    async def slow(frame: object) -> None:
        # Keep the scan busy until the test has tried a second one.
        while not gate_open:
            await asyncio.sleep(0.005)

    transport.on_write = slow
    assert signed_in_client.post(SCAN_URL, data={"kind": "primary"}).status_code == (
        303
    )

    second = signed_in_client.post(SCAN_URL, data={"kind": "secondary"})

    assert second.status_code == 409
    assert "A scan is already running on this Connection." in text(second.text)
    running = signed_in_client.get(f"{SCAN_URL}/progress").text
    assert 'hx-trigger="every 1s"' in running
    assert f'action="{SCAN_URL}/cancel"' in running

    cancelled = signed_in_client.post(f"{SCAN_URL}/cancel")
    assert cancelled.status_code == 303
    gate_open = True
    fragment = _wait_finished(signed_in_client)
    assert "Scan cancelled." in text(fragment)


def test_scan_with_other_running_settings_is_refused(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    _run_with_other_settings(fake_host)

    response = signed_in_client.post(SCAN_URL, data={"kind": "primary"})

    assert response.status_code == 409
    assert SETTINGS_DIFFER in text(response.text)


def test_scan_rejects_unknown_kind(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    response = signed_in_client.post(SCAN_URL, data={"kind": "tertiary"})

    assert response.status_code == 400
    assert fake_host.mbus_scans.get("heat") is None


def test_progress_without_a_scan(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    response = signed_in_client.get(f"{SCAN_URL}/progress")

    assert response.status_code == 200
    assert "hx-trigger" not in response.text
    assert "No scan has run" in text(response.text)


def _run_with_other_settings(host: FakeHost) -> None:
    running = ConnectionConfig(
        id="heat",
        name="Heat meters",
        protocol="mbus",
        endpoint="tcp://other-gw.example:10001",
    )
    host.running_config = CollectorConfig(
        connections=(running,), tag_groups=(), tags=()
    )
    transport = memory_transport(simulator.demo_meters())
    host.mbus_buses["heat"] = MBusBus(lambda: transport, timeout_s=0.2, max_telegrams=8)


# Read meter


def test_read_meter_page_prefills_the_meter(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    page = signed_in_client.get(f"{READ_URL}?meter=12345678")

    assert page.status_code == 200
    assert 'name="meter" id="read-meter" value="12345678"' in page.text
    assert 'name="token"' not in page.text


def test_read_meter_lists_every_record_with_the_date_greyed(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    _memory_bus(fake_host)

    status, page = _read(signed_in_client, "12345678")

    assert status == 200, text(page)
    plain = text(page)
    assert "12345678" in plain and "PDC" in plain and "heat" in plain
    rows = _rows(page)
    assert len(rows) == 10
    names = [re.search(r'class="addr name">([^<]*)<', row) for row in rows.values()]
    shown = [m.group(1) for m in names if m is not None]
    assert shown[:5] == [
        "m12345678_energy",
        "m12345678_energy_s1",
        "m12345678_volume",
        "m12345678_power",
        "m12345678_volume_flow",
    ]
    date_row = rows["9"]
    assert "row-muted" in re.search(r'<tr data-row="9"[^>]*>', page).group(0)  # type: ignore[union-attr]
    assert "date/time records are not supported" in text(date_row)
    assert 'name="select"' not in date_row
    # Supported rows are ticked, with the unit select on the meter's unit.
    assert _checked(page) == {str(i) for i in range(9)}
    energy = rows["0"]
    assert re.search(r'<option value="MWh" selected>', energy)
    assert '<option value="GJ"' not in energy  # another family
    operating = rows["8"]
    assert re.search(r'<option value="s" selected>', operating)
    assert _prefix(page) == "m12345678"


def test_read_meter_by_primary_uses_a_primary_prefix(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    _memory_bus(fake_host)

    status, page = _read(signed_in_client, "2")

    assert status == 200, text(page)
    assert _prefix(page) == "m2"
    assert "m2_volume" in text(page)


def test_save_three_ticked_rows_creates_three_tags(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    stores = _seed(fake_host)
    _memory_bus(fake_host)
    _, page = _read(signed_in_client, "12345678")

    status, body, location = _save(
        signed_in_client, page, ["0", "2", "8"], units={"0": "kWh", "2": "m3"}
    )

    assert status == 303, text(body)
    assert location == "/panel/connections/heat"
    tags = _active_tags(stores)
    assert sorted(tags) == [
        "heat_m12345678_energy",
        "heat_m12345678_operating_time",
        "heat_m12345678_volume",
    ]
    energy = tags["heat_m12345678_energy"]
    assert energy.name == "m12345678_energy"
    assert energy.address == "12345678"
    assert energy.tag_group_id == "meters"
    assert energy.value_type is ValueType.NUMERIC
    assert dict(energy.protocol_options) == {
        "quantity": "energy",
        "unit": "kWh",
        "meter_id": "12345678",
    }
    assert energy.metadata["unit"] == "kWh"
    volume = tags["heat_m12345678_volume"]
    assert volume.protocol_options["unit"] == "m3"
    operating = tags["heat_m12345678_operating_time"]
    assert operating.value_type is ValueType.INTEGER
    assert operating.protocol_options["unit"] == "s"
    audits = [
        record
        for record in caplog.records
        if record.getMessage() == "configuration_changed"
        and getattr(record, "kind", None) == "tag"
    ]
    assert sorted(getattr(r, "id", "") for r in audits) == sorted(tags)
    assert "Added 3 tags." in text(signed_in_client.get(location).text)


def test_read_by_primary_stores_primary_address_and_meter_id(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    _memory_bus(fake_host)
    _, page = _read(signed_in_client, "1")

    status, body, _ = _save(signed_in_client, page, ["0"])

    assert status == 303, text(body)
    (tag,) = _active_tags(stores).values()
    assert tag.address == "1"
    assert tag.protocol_options["meter_id"] == "12345678"


def test_row_already_used_by_a_tag_is_marked_and_unticked(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    _memory_bus(fake_host)
    stores.config.create_tag(_mbus_tag("old_energy", unit="kWh"))
    _, page = _read(signed_in_client, "12345678")

    row = _rows(page)["0"]
    assert "Already read by tag old_energy" in text(row)
    assert "0" not in _checked(page)
    assert "1" in _checked(page)

    status, body, _ = _save(signed_in_client, page, ["0", "1"])

    assert status == 400
    assert "Already read by tag old_energy" in text(body)
    assert sorted(_active_tags(stores)) == ["old_energy"]


def test_names_never_overwrite_existing_tags(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    """Review Focus 5: same prefix on two meters, and a name equal to an ID."""
    stores = _seed(fake_host)
    _memory_bus(fake_host)
    # A Tag (on another Connection) whose ID equals a suggested name.
    stores.config.upsert_connection(OTHER)
    stores.config.upsert_tag_group(
        TagGroupConfig(
            id="water_g", connection_id="water", interval_ms=60_000, timeout_ms=1000
        )
    )
    stores.config.create_tag(
        _mbus_tag(
            "meter_volume_flow",
            address="9",
            quantity="volume_flow",
            unit="L/h",
            group="water_g",
        )
    )

    _, first = _read(signed_in_client, "12345678")
    status, first, _ = _save(
        signed_in_client, first, ["2", "4"], prefix="meter", action="names"
    )
    assert status == 200
    status, body, _ = _save(signed_in_client, first, ["2", "4"])
    assert status == 303, text(body)
    _, second = _read(signed_in_client, "12399999")
    status, body, _ = _save(
        signed_in_client, second, ["0", "1", "2"], prefix="meter", action="names"
    )
    assert status == 200
    status, body, _ = _save(signed_in_client, body, ["0", "1", "2"])
    assert status == 303, text(body)

    tags = _active_tags(stores)
    assert sorted((t.id, t.name, t.address) for t in tags.values()) == [
        ("heat_meter_volume", "meter_volume", "12345678"),
        ("heat_meter_volume_2", "meter_volume_2", "12399999"),
        ("heat_meter_volume_flow_2", "meter_volume_flow_2", "12345678"),
        ("heat_meter_volume_flow_3", "meter_volume_flow_3", "12399999"),
        ("heat_meter_volume_s1", "meter_volume_s1", "12399999"),
        ("meter_volume_flow", "meter_volume_flow", "9"),
    ]


def test_save_refuses_a_name_taken_since_the_preview(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    _memory_bus(fake_host)
    _, page = _read(signed_in_client, "12345678")
    # Another tab takes the ID the preview showed.
    stores.config.create_tag(_mbus_tag("heat_m12345678_volume", address="99"))

    status, body, _ = _save(signed_in_client, page, ["2"])

    assert status == 409
    assert "changed since" in text(body)
    assert stores.config.get_tag("heat_m12345678_volume").address == "99"  # type: ignore[union-attr]
    assert "heat_m12345678_volume_2" in body
    status, body, _ = _save(signed_in_client, body, ["2"])
    assert status == 303, text(body)
    assert "heat_m12345678_volume_2" in _active_tags(stores)


def test_changed_prefix_without_update_is_shown_first(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    _memory_bus(fake_host)
    _, page = _read(signed_in_client, "12345678")

    status, body, _ = _save(signed_in_client, page, ["0"], prefix="boiler")

    assert status == 409
    assert "boiler_energy" in text(body)
    assert _active_tags(stores) == {}


def test_save_battery_warning_for_a_fast_group(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host, interval_ms=10_000)
    _memory_bus(fake_host)
    _, page = _read(signed_in_client, "12345678")

    status, _, location = _save(signed_in_client, page, ["0"])

    assert status == 303
    assert location is not None
    shown = text(signed_in_client.get(location).text)
    assert "This group polls M-Bus meters every 10 s." in shown


def test_save_needs_a_group_and_a_selection(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    _memory_bus(fake_host)
    _, page = _read(signed_in_client, "12345678")

    no_group = _save(signed_in_client, page, ["0"], group="nope")
    nothing = _save(signed_in_client, page, [])

    assert no_group[0] == 400 and "Choose a tag group." in text(no_group[1])
    assert nothing[0] == 400 and "Select at least one record." in text(nothing[1])


def test_expired_reading(signed_in_client: TestClient, fake_host: FakeHost) -> None:
    _seed(fake_host)

    response = signed_in_client.post(
        SAVE_URL, data={"token": "A" * 22, "action": "save", "select": "0"}
    )

    assert response.status_code == 400
    assert "This reading expired. Read the meter again." in text(response.text)


def test_read_meter_errors(signed_in_client: TestClient, fake_host: FakeHost) -> None:
    _seed(fake_host)
    _memory_bus(fake_host)

    invalid = _read(signed_in_client, "P5")
    silent = _read(signed_in_client, "77")

    assert invalid[0] == 400
    assert "Invalid M-Bus meter address" in text(invalid[1])
    assert silent[0] == 200
    assert "(mbus_no_reply)" in text(silent[1])
    assert 'name="token"' not in silent[1]


def test_read_meter_with_other_running_settings_is_refused(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    _run_with_other_settings(fake_host)

    status, page = _read(signed_in_client, "12345678")

    assert status == 409
    assert SETTINGS_DIFFER in text(page)


def test_read_meter_refused_while_applying(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    fake_host.applying = True

    status, page = _read(signed_in_client, "12345678")

    assert status == 409
    assert "Changes are being applied. Try again when they are." in text(page)


def test_pages_of_unknown_or_other_protocol_connections_are_not_found(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(
        ConnectionConfig(
            id="press", name="Press", protocol="modbus", endpoint="tcp://p:502"
        )
    )

    for url in (
        "/panel/connections/press/mbus/scan",
        "/panel/connections/press/mbus/read",
        "/panel/connections/nope/mbus/scan",
    ):
        assert signed_in_client.get(url).status_code == 404


def test_connection_page_links_scan_and_read(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    page = signed_in_client.get("/panel/connections/heat").text

    assert f'href="{SCAN_URL}"' in page
    assert f'href="{READ_URL}"' in page


def test_tag_form_links_read_meter(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.create_tag(_mbus_tag("heat_e", address="12345678"))

    page = signed_in_client.get("/panel/tags/heat_e/edit").text

    assert f'href="{READ_URL}?meter=12345678"' in page


# Access


def test_routes_require_sign_in(panel_client: TestClient) -> None:
    for response in (
        panel_client.get(SCAN_URL),
        panel_client.post(SCAN_URL, data={"kind": "primary"}),
        panel_client.get(f"{SCAN_URL}/progress"),
        panel_client.post(f"{SCAN_URL}/cancel"),
        panel_client.get(READ_URL),
        panel_client.post(READ_URL, data={"meter": "1"}),
        panel_client.post(SAVE_URL, data={"token": "x"}),
    ):
        assert response.status_code == 303
        assert response.headers["location"].startswith("/panel/login")


def test_posts_refuse_cross_site(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    evil = {"Origin": "https://evil.example"}

    responses = [
        signed_in_client.post(SCAN_URL, data={"kind": "primary"}, headers=evil),
        signed_in_client.post(f"{SCAN_URL}/cancel", headers=evil),
        signed_in_client.post(READ_URL, data={"meter": "1"}, headers=evil),
        signed_in_client.post(SAVE_URL, data={"token": "x"}, headers=evil),
    ]

    assert [r.status_code for r in responses] == [403, 403, 403, 403]
    assert fake_host.mbus_scans.get("heat") is None


# record_rows and check_save


def _header(id_digits: str = "12345678") -> MeterHeader:
    return MeterHeader(
        id_digits=id_digits,
        manufacturer="PDC",
        manufacturer_code=0x4064,
        version=1,
        medium=4,
        medium_name="heat",
        access_no=1,
        status=0,
    )


def _record(
    quantity: Quantity = Quantity.ENERGY,
    unit: str = "kWh",
    family: str = "energy_wh",
    *,
    storage: int = 0,
    tariff: int = 0,
    subunit: int = 0,
    function: Function = Function.INSTANTANEOUS,
    qualifiers: tuple[str, ...] = (),
) -> MBusRecord:
    return MBusRecord(
        key=RecordKey(quantity, storage, tariff, subunit, function, qualifiers),
        unit=unit,
        family=UNIT_FAMILIES[family],
        exact_value=Decimal("1.5"),
        unsupported_reason=None,
        raw=b"",
        description="",
    )


def test_suggested_name_rule() -> None:
    key = RecordKey(Quantity.ENERGY, 1, 2, 3, Function.MAXIMUM, ("backward",))
    assert suggested_name("m1", key) == "m1_energy_s1_t2_u3_maximum_backward"
    plain = RecordKey(Quantity.VOLUME, 0, 0, 0, Function.INSTANTANEOUS, ())
    assert suggested_name("m1", plain) == "m1_volume"


def test_repeated_key_gets_an_occurrence_and_a_unique_name() -> None:
    reading = MeterReading(_header(), (_record(), _record()))

    rows = record_rows(reading, [], "m1", meter="1", connection_id="heat")

    assert [(r.name, r.tag_id, r.occurrence) for r in rows] == [
        ("m1_energy", "heat_m1_energy", 1),
        ("m1_energy_2", "heat_m1_energy_2", 2),
    ]


def test_record_rows_units_follow_the_family() -> None:
    reading = MeterReading(
        _header(),
        (
            _record(unit="GJ", family="energy_j"),
            _record(Quantity.HCA_UNITS, unit="", family="hca_units"),
        ),
    )

    energy, hca = record_rows(reading, [], "m1", meter="1", connection_id="heat")

    assert energy.units == ("J", "kJ", "MJ", "GJ")
    assert energy.unit == "GJ"
    assert hca.units == ()
    assert hca.unit == ""


def test_more_than_1000_ticked_rows_is_refused() -> None:
    count = MAX_SAVE_ROWS + 1
    reading = MeterReading(
        _header(), tuple(_record(storage=index) for index in range(count))
    )
    rows = record_rows(reading, [], "m1", meter="1", connection_id="heat")

    result = check_save(
        rows,
        selected=[str(index) for index in range(count)],
        units={},
        shown_ids={row.index: row.tag_id for row in rows},
        group_id="meters",
        group_ids=["meters"],
        meter="1",
        meter_id="12345678",
    )

    assert result.tags is None
    assert result.error == f"Save at most {MAX_SAVE_ROWS} tags at a time."
