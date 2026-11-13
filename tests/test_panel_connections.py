"""Tests for the Panel's Connections pages: list, add, edit, delete and test."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from panel_support import FakeHost
from sqlalchemy import select

from plc_data_collector.api import RuntimeApiState
from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import (
    ConnectionConfig,
    PollExecution,
    PollStatus,
    RuntimeComponentStatus,
    TagConfig,
    TagGroupConfig,
    TagResult,
    TagValue,
    ValueType,
    WorkerState,
)
from plc_data_collector.persistence.schema import connections
from plc_data_collector.runtime.connection_worker import WorkerPollResult

SAVED_FLASH = "Saved changes take effect after you apply them from the banner."
ID_ERROR = "Use letters, digits, dot, dash or underscore (up to 128)."
PASSWORD = "s3cret-Pa55word"
NOW = datetime.now(UTC)


def _stores(host: FakeHost) -> Stores:
    assert host.stores is not None
    return host.stores


def _opcua(
    connection_id: str = "line1_opcua",
    *,
    name: str = "Line 1 filler",
    enabled: bool = True,
    **options: object,
) -> ConnectionConfig:
    return ConnectionConfig(
        id=connection_id,
        name=name,
        protocol="opcua",
        endpoint="opc.tcp://plc-sim:4840",
        enabled=enabled,
        protocol_options=options,
    )


def _create_form(**overrides: str) -> dict[str, str]:
    data = {
        "protocol": "opcua",
        "id": "line1_opcua",
        "name": "Line 1 filler",
        "endpoint": "opc.tcp://plc-sim:4840",
        "username": "operator",
        "password": PASSWORD,
        "security_string": "",
        "timeout_ms": "3000",
        "enabled": "on",
    }
    data.update(overrides)
    return data


def _edit_form(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Line 1 filler",
        "endpoint": "opc.tcp://plc-sim:4840",
        "username": "operator",
        "password": "",
        "security_string": "",
        "timeout_ms": "3000",
        "enabled": "on",
    }
    data.update(overrides)
    return data


def _row(html: str, connection_id: str) -> str:
    match = re.search(
        rf'<tr[^>]*data-connection="{re.escape(connection_id)}"[^>]*>.*?</tr>',
        html,
        re.DOTALL,
    )
    assert match is not None, connection_id
    return match.group(0)


def _worker(connection_id: str, state: WorkerState) -> RuntimeComponentStatus:
    return RuntimeComponentStatus(
        component_id=f"connection-worker:{connection_id}",
        state=state,
        updated_at=NOW,
        error_code="opcua_connect_failed" if state is WorkerState.FAILED else None,
    )


def _add_group_with_tags(
    stores: Stores, connection_id: str, group_id: str, tag_count: int
) -> list[TagConfig]:
    stores.config.upsert_tag_group(
        TagGroupConfig(id=group_id, connection_id=connection_id, interval_ms=1000)
    )
    tags = [
        TagConfig(
            id=f"{group_id}_t{index}",
            tag_group_id=group_id,
            name=f"Tag {index}",
            address=f"ns=2;s={group_id}.T{index}",
            value_type=ValueType.NUMERIC,
        )
        for index in range(tag_count)
    ]
    for tag in tags:
        stores.config.upsert_tag(tag)
    return tags


def _record_poll(
    host: FakeHost,
    connection_id: str,
    group_id: str,
    good: list[str],
    bad: list[str],
    *,
    age_s: float = 0.4,
) -> None:
    finished = datetime.now(UTC) - timedelta(seconds=age_s)
    results = tuple(
        TagResult.success(
            tag_id=tag_id,
            value=TagValue.numeric(1.0),
            source_timestamp=finished,
            received_at=finished,
        )
        for tag_id in good
    ) + tuple(
        TagResult.failure(
            tag_id=tag_id,
            error_code="opcua_read_failed",
            error_message="OPC UA read failed.",
            received_at=finished,
        )
        for tag_id in bad
    )
    host.live_values.update(
        WorkerPollResult(
            execution=PollExecution(
                execution_id=f"poll-{group_id}",
                connection_id=connection_id,
                tag_group_id=group_id,
                status=PollStatus.PARTIAL_FAILURE if bad else PollStatus.SUCCESS,
                started_at=finished - timedelta(milliseconds=10),
                finished_at=finished,
                requested_tags=len(results),
                successful_tags=len(good),
                failed_tags=len(bad),
            ),
            results=results,
        )
    )


# List


def test_list_shows_states_with_words(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    for connection_id in ("run", "deg", "fail", "idle"):
        stores.config.upsert_connection(_opcua(connection_id, name=connection_id))
    stores.config.upsert_connection(_opcua("dis", name="dis", enabled=False))
    fake_host.runtime_state = RuntimeApiState(
        workers=(
            _worker("run", WorkerState.RUNNING),
            _worker("deg", WorkerState.DEGRADED),
            _worker("fail", WorkerState.FAILED),
        )
    )

    response = signed_in_client.get("/panel/connections")

    assert response.status_code == 200
    html = response.text
    expected = {
        "run": ("Running", "state-normal"),
        "deg": ("Degraded", "state-degraded"),
        "fail": ("Failed", "state-failed"),
        "dis": ("Disabled", "state-disabled"),
        "idle": ("Not collecting", "state-disabled"),
    }
    for connection_id, (word, css_class) in expected.items():
        row = _row(html, connection_id)
        assert word in row, connection_id
        assert css_class in row, connection_id
    running = _row(html, "run")
    for colour_class in (
        "row-failed",
        "row-degraded",
        "state-failed",
        "state-degraded",
    ):
        assert colour_class not in running
    assert "row-failed" in _row(html, "fail")
    assert "opcua_connect_failed" in _row(html, "fail")
    assert 'aria-current="page"' in html
    assert "5 connections, 0 tags." in html


def test_list_counts_tags_collected_and_last_poll(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_opcua("line1"))
    stores.config.upsert_connection(_opcua("packer", enabled=False))
    fast = _add_group_with_tags(stores, "line1", "fast", 3)
    slow = _add_group_with_tags(stores, "line1", "slow", 2)
    _add_group_with_tags(stores, "packer", "packing", 4)
    _record_poll(
        fake_host, "line1", "fast", [fast[0].id, fast[1].id], [fast[2].id], age_s=30
    )
    _record_poll(fake_host, "line1", "slow", [slow[0].id, slow[1].id], [], age_s=0.4)

    html = signed_in_client.get("/panel/connections").text

    line1 = _row(html, "line1")
    assert "4 of 5" in line1
    assert "0.4 s ago" in line1
    packer = _row(html, "packer")
    assert "Not collecting (4 tags)" in packer
    assert "None" in packer
    assert "2 connections, 9 tags." in html


def test_list_masks_endpoint_userinfo(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _stores(fake_host).config.upsert_connection(
        ConnectionConfig(
            id="line1",
            protocol="opcua",
            endpoint="opc.tcp://operator:url-secret@plc-sim:4840",
        )
    )

    html = signed_in_client.get("/panel/connections").text

    assert "url-secret" not in html
    assert "opc.tcp://***@plc-sim:4840" in html


def test_connections_require_sign_in(panel_client: TestClient) -> None:
    for path in ("/panel/connections", "/panel/connections/new"):
        response = panel_client.get(path)
        assert response.status_code == 303, path
    response = panel_client.post("/panel/connections", data=_create_form())
    assert response.status_code == 303


# Add


def test_add_connection_picker_hides_mock_and_marks_future_protocols(
    signed_in_client: TestClient,
) -> None:
    response = signed_in_client.get("/panel/connections/new")

    assert response.status_code == 200
    html = response.text
    assert re.search(r'<input[^>]*value="opcua"(?![^>]*disabled)[^>]*>', html)
    assert 'value="mock"' not in html
    assert ">Mock<" not in html
    for label, protocol, version in (
        ("Modbus TCP", "modbus", "0.4.0"),
        ("S7", "s7", "0.5.0"),
        ("M-Bus", "mbus", "0.6.0"),
    ):
        assert label in html
        assert re.search(rf'<input[^>]*value="{protocol}"[^>]*disabled', html)
        assert f"Coming in {version}" in html
    assert "Endpoint URL, optional user and certificate" in html
    assert "Host, port and unit ID" in html
    assert "Continue to settings" in html


def test_new_form_for_unavailable_protocol_shows_picker(
    signed_in_client: TestClient,
) -> None:
    for protocol in ("mock", "modbus", "nonsense"):
        response = signed_in_client.get(f"/panel/connections/new?protocol={protocol}")
        assert response.status_code == 400, protocol
        assert "Choose a protocol" in response.text
        assert 'name="endpoint"' not in response.text


def test_new_form_shows_opcua_fields(signed_in_client: TestClient) -> None:
    response = signed_in_client.get(
        "/panel/connections/new?protocol=opcua&name=Line+2+oven&id=line2_oven"
    )

    assert response.status_code == 200
    html = response.text
    for name in ("endpoint", "username", "password", "security_string"):
        assert f'name="{name}"' in html
    assert 'name="auto_reconnect"' in html
    assert "For example opc.tcp://plc-sim:4840" in html
    assert 'value="line2_oven"' in html
    assert 'value="Line 2 oven"' in html


def test_create_opcua_connection_stores_secret_encrypted(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    response = signed_in_client.post("/panel/connections", data=_create_form())

    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/panel/connections/line1_opcua"
    stored = _stores(fake_host).config.get_connection("line1_opcua")
    assert stored is not None
    assert stored.name == "Line 1 filler"
    assert stored.protocol == "opcua"
    assert stored.endpoint == "opc.tcp://plc-sim:4840"
    assert stored.protocol_options == {
        "username": "operator",
        "password": PASSWORD,
        "auto_reconnect": False,
    }
    with fake_host.engine.connect() as connection:
        row = connection.execute(
            select(connections.c.protocol_options, connections.c.secret_options).where(
                connections.c.id == "line1_opcua"
            )
        ).one()
    assert "password" not in row.protocol_options
    assert row.secret_options is not None
    assert PASSWORD not in row.secret_options

    page = signed_in_client.get(response.headers["location"])
    assert SAVED_FLASH in page.text
    # The flash is shown once.
    assert SAVED_FLASH not in _flash_area(
        signed_in_client.get(response.headers["location"]).text
    )


def _flash_area(html: str) -> str:
    match = re.search(r'<p class="flash"[^>]*>.*?</p>', html, re.DOTALL)
    return match.group(0) if match else ""


def test_invalid_id_shows_field_error(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    for bad_id in ("bad id", "line1/opcua", "x" * 129, ""):
        response = signed_in_client.post(
            "/panel/connections", data=_create_form(id=bad_id)
        )

        assert response.status_code == 400, bad_id
        assert ID_ERROR in response.text
        assert 'aria-invalid="true"' in response.text
        assert PASSWORD not in response.text
    assert _stores(fake_host).config.load_active()[0].connections == ()


def test_duplicate_id_shows_field_error(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_opcua("line1_opcua", name="Original"))

    response = signed_in_client.post("/panel/connections", data=_create_form())

    assert response.status_code == 400
    assert "A connection with this ID already exists" in response.text
    existing = stores.config.get_connection("line1_opcua")
    assert existing is not None
    assert existing.name == "Original"


def test_create_requires_endpoint_and_positive_timeout(
    signed_in_client: TestClient,
) -> None:
    response = signed_in_client.post(
        "/panel/connections",
        data=_create_form(endpoint="", timeout_ms="0"),
    )

    assert response.status_code == 400
    assert "Enter the endpoint URL." in response.text
    assert "Enter a whole number of milliseconds, 1 or more." in response.text


def test_endpoint_with_credentials_is_refused(signed_in_client: TestClient) -> None:
    response = signed_in_client.post(
        "/panel/connections",
        data=_create_form(endpoint="opc.tcp://op:inline-pw@plc-sim:4840"),
    )

    assert response.status_code == 400
    assert "inline-pw" not in response.text
    assert "User name and Password fields" in response.text


def test_create_with_unavailable_protocol_is_refused(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    for protocol in ("mock", "modbus"):
        response = signed_in_client.post(
            "/panel/connections", data=_create_form(protocol=protocol)
        )
        assert response.status_code == 400, protocol
    assert _stores(fake_host).config.load_active()[0].connections == ()


def test_create_restores_archived_id(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_opcua("line1_opcua", name="Old"))
    stores.config.archive_connection("line1_opcua")

    response = signed_in_client.post("/panel/connections", data=_create_form())

    assert response.status_code == 303
    restored = stores.config.get_connection("line1_opcua")
    assert restored is not None
    assert restored.name == "Line 1 filler"


# Edit


def test_edit_page_shows_read_only_id_and_saved_password_hint(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _stores(fake_host).config.upsert_connection(
        _opcua(username="operator", password=PASSWORD)
    )

    response = signed_in_client.get("/panel/connections/line1_opcua")

    assert response.status_code == 200
    html = response.text
    assert re.search(r'<input[^>]*value="line1_opcua"[^>]*readonly', html)
    assert (
        "Used in the database and API. It can't be changed after the connection "
        "is created."
    ) in html.replace("&#39;", "'")
    assert 'name="remove_password"' in html
    assert "Test connection" in html
    assert "Save connection" in html


def test_edit_blank_password_keeps_existing(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(
        _opcua(username="operator", password=PASSWORD, reconnect_max_delay_s=5.0)
    )

    response = signed_in_client.post(
        "/panel/connections/line1_opcua",
        data=_edit_form(name="Renamed", username="op2"),
    )

    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/panel/connections/line1_opcua"
    stored = stores.config.get_connection("line1_opcua")
    assert stored is not None
    assert stored.name == "Renamed"
    assert stored.protocol_options["password"] == PASSWORD
    assert stored.protocol_options["username"] == "op2"
    # Options the form does not show are kept.
    assert stored.protocol_options["reconnect_max_delay_s"] == 5.0


def test_edit_new_password_replaces_existing(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_opcua(password=PASSWORD))

    signed_in_client.post(
        "/panel/connections/line1_opcua", data=_edit_form(password="new-pw")
    )

    stored = stores.config.get_connection("line1_opcua")
    assert stored is not None
    assert stored.protocol_options["password"] == "new-pw"


def test_remove_password_checkbox_clears_it(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_opcua(username="operator", password=PASSWORD))

    response = signed_in_client.post(
        "/panel/connections/line1_opcua",
        data=_edit_form(remove_password="on"),
    )

    assert response.status_code == 303
    stored = stores.config.get_connection("line1_opcua")
    assert stored is not None
    assert "password" not in stored.protocol_options
    with fake_host.engine.connect() as connection:
        secret = connection.execute(
            select(connections.c.secret_options).where(
                connections.c.id == "line1_opcua"
            )
        ).scalar_one()
    assert secret is None


def test_edit_clears_blank_optional_text(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(
        _opcua(username="operator", security_string="Basic256Sha256,Sign,c,k")
    )

    signed_in_client.post(
        "/panel/connections/line1_opcua",
        data=_edit_form(username="", security_string="", enabled=""),
    )

    stored = stores.config.get_connection("line1_opcua")
    assert stored is not None
    assert "username" not in stored.protocol_options
    assert "security_string" not in stored.protocol_options


def test_edit_unchecked_enabled_disables(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_opcua())
    data = _edit_form()
    del data["enabled"]

    signed_in_client.post("/panel/connections/line1_opcua", data=data)

    stored = stores.config.get_connection("line1_opcua")
    assert stored is not None
    assert stored.enabled is False


def test_edit_keeps_masked_endpoint_credentials(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    original = "opc.tcp://operator:url-secret@plc-sim:4840"
    stores.config.upsert_connection(
        ConnectionConfig(id="line1_opcua", protocol="opcua", endpoint=original)
    )

    page = signed_in_client.get("/panel/connections/line1_opcua").text
    assert "url-secret" not in page
    assert 'value="opc.tcp://***@plc-sim:4840"' in page

    response = signed_in_client.post(
        "/panel/connections/line1_opcua",
        data=_edit_form(endpoint="opc.tcp://***@plc-sim:4840"),
    )

    assert response.status_code == 303
    stored = stores.config.get_connection("line1_opcua")
    assert stored is not None
    assert stored.endpoint == original


def test_save_bumps_revision(signed_in_client: TestClient, fake_host: FakeHost) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_opcua())
    before = stores.config.revision()

    response = signed_in_client.post(
        "/panel/connections/line1_opcua", data=_edit_form(timeout_ms="2500")
    )

    assert response.status_code == 303
    assert stores.config.revision() == before + 1
    stored = stores.config.get_connection("line1_opcua")
    assert stored is not None
    assert stored.timeout_ms == 2500


def test_edit_with_errors_changes_nothing(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_opcua())
    before = stores.config.revision()

    response = signed_in_client.post(
        "/panel/connections/line1_opcua", data=_edit_form(timeout_ms="soon")
    )

    assert response.status_code == 400
    assert stores.config.revision() == before


def test_edit_mock_connection_keeps_its_options(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(
        ConnectionConfig(
            id="sim",
            protocol="mock",
            endpoint="mock://sim",
            protocol_options={"tags": {"t": {"value": 1}}},
        )
    )

    page = signed_in_client.get("/panel/connections/sim")
    assert page.status_code == 200
    response = signed_in_client.post(
        "/panel/connections/sim",
        data={"name": "Sim", "endpoint": "mock://sim2", "timeout_ms": "1000"},
    )

    assert response.status_code == 303, response.text
    stored = stores.config.get_connection("sim")
    assert stored is not None
    assert stored.endpoint == "mock://sim2"
    assert stored.protocol_options == {"tags": {"t": {"value": 1}}}


def test_unknown_connection_is_404(signed_in_client: TestClient) -> None:
    assert signed_in_client.get("/panel/connections/nope").status_code == 404
    assert (
        signed_in_client.post("/panel/connections/nope", data=_edit_form()).status_code
        == 404
    )
    assert signed_in_client.get("/panel/connections/nope/delete").status_code == 404


# Secrets never rendered


def test_password_never_rendered(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    created = signed_in_client.post("/panel/connections", data=_create_form())
    assert created.status_code == 303
    stores = _stores(fake_host)
    _add_group_with_tags(stores, "line1_opcua", "fast", 1)

    pages = [
        signed_in_client.get("/panel/connections"),
        signed_in_client.get("/panel/connections/line1_opcua"),
        signed_in_client.get("/panel/connections/line1_opcua/delete"),
        signed_in_client.get("/panel/connections/new?protocol=opcua"),
        signed_in_client.post(
            "/panel/connections/line1_opcua", data=_edit_form(timeout_ms="x")
        ),
        signed_in_client.post(
            "/panel/connections/line1_opcua",
            data=_edit_form(password="typed-but-invalid", timeout_ms="x"),
        ),
        signed_in_client.post(
            "/panel/connections", data=_create_form(id="line1_opcua")
        ),
    ]
    for response in pages:
        assert PASSWORD not in response.text, response.url
        assert "typed-but-invalid" not in response.text, response.url


# Delete


def test_delete_archives_with_children(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_opcua())
    fast = _add_group_with_tags(stores, "line1_opcua", "fast", 2)
    _add_group_with_tags(stores, "line1_opcua", "slow", 1)

    confirm = signed_in_client.get("/panel/connections/line1_opcua/delete")

    assert confirm.status_code == 200
    assert "2 tag groups and 3 tags" in confirm.text
    assert stores.config.get_connection("line1_opcua") is not None

    response = signed_in_client.post("/panel/connections/line1_opcua/delete")

    assert response.status_code == 303
    assert response.headers["location"] == "/panel/connections"
    assert stores.config.get_connection("line1_opcua") is None
    assert stores.config.get_tag_group("fast") is None
    assert stores.config.get_tag_group("slow") is None
    assert stores.config.is_archived_tag(fast[0].id) is True
    listing = signed_in_client.get("/panel/connections").text
    assert 'data-connection="line1_opcua"' not in listing
    assert "Connection deleted." in listing


# Same-site guard


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/panel/connections", _create_form()),
        ("/panel/connections/line1_opcua", _edit_form()),
        ("/panel/connections/line1_opcua/delete", {}),
        ("/panel/connections/line1_opcua/test", _edit_form()),
    ],
)
def test_cross_site_posts_are_refused(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    path: str,
    data: dict[str, str],
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_opcua(name="Original"))
    before = stores.config.revision()

    response = signed_in_client.post(
        path, data=data, headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert stores.config.revision() == before


# Test connection


def test_test_connection_partial_reports_result(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(
        ConnectionConfig(
            id="sim",
            protocol="mock",
            endpoint="mock://sim",
            timeout_ms=500,
            protocol_options={"tags": {"sim_temp": {"value": 21.5}}},
        )
    )
    stores.config.upsert_tag_group(
        TagGroupConfig(id="g", connection_id="sim", interval_ms=1000)
    )
    stores.config.upsert_tag(
        TagConfig(
            id="sim_temp",
            tag_group_id="g",
            name="Temp",
            address="ns=2;s=Temp",
            value_type=ValueType.NUMERIC,
        )
    )

    response = signed_in_client.post(
        "/panel/connections/sim/test",
        data={"name": "sim", "endpoint": "mock://sim", "timeout_ms": "500"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'role="status"' in response.text
    assert re.search(
        r"Connected in \d+ ms\. Read ns=2;s=Temp without errors\.", response.text
    )


def test_test_connection_reports_form_errors(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _stores(fake_host).config.upsert_connection(_opcua())

    response = signed_in_client.post(
        "/panel/connections/line1_opcua/test",
        data=_edit_form(endpoint=""),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Enter the endpoint URL." in response.text
    assert "alert-failed" in response.text
