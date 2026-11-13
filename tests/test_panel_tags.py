"""Tests for the Panel's Tag Group and Tag management on the Connection page."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from panel_support import FakeHost

from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import (
    ConnectionConfig,
    PollExecution,
    PollStatus,
    TagConfig,
    TagGroupConfig,
    TagResult,
    TagValue,
    ValueType,
)
from plc_data_collector.panel.tag_forms import generate_tag_id
from plc_data_collector.runtime.connection_worker import WorkerPollResult

SAVED_FLASH = "Saved changes take effect after you apply them from the banner."
RANGE_ERROR = "Enter a whole number of milliseconds from 1 to 600000."
TIMEOUT_ORDER_ERROR = "Timeout must not be longer than the read interval."
GROUP_ERROR = "Choose a tag group of this connection."
NOW = datetime.now(UTC)


def _stores(host: FakeHost) -> Stores:
    assert host.stores is not None
    return host.stores


def _connection(connection_id: str = "line1_opcua") -> ConnectionConfig:
    return ConnectionConfig(
        id=connection_id,
        name="Line 1 filler",
        protocol="opcua",
        endpoint="opc.tcp://plc-sim:4840",
    )


def _group(
    group_id: str = "fast", connection_id: str = "line1_opcua"
) -> TagGroupConfig:
    return TagGroupConfig(
        id=group_id, connection_id=connection_id, interval_ms=1000, timeout_ms=800
    )


def _tag(tag_id: str = "line1_opcua_temperature", group_id: str = "fast") -> TagConfig:
    return TagConfig(
        id=tag_id,
        tag_group_id=group_id,
        name="Temperature",
        address="ns=2;s=Temp",
        value_type=ValueType.NUMERIC,
    )


def _seed(host: FakeHost) -> Stores:
    stores = _stores(host)
    stores.config.upsert_connection(_connection())
    stores.config.upsert_tag_group(_group())
    return stores


def _group_form(**overrides: str) -> dict[str, str]:
    data = {
        "id": "fast",
        "interval_ms": "1000",
        "timeout_ms": "800",
        "enabled": "on",
    }
    data.update(overrides)
    return data


def _tag_form(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Temperature",
        "tag_group_id": "fast",
        "address": "ns=2;s=Temp",
        "value_type": "numeric",
        "unit": "C",
        "enabled": "on",
    }
    data.update(overrides)
    return data


def test_add_group_and_tag(signed_in_client: TestClient, fake_host: FakeHost) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_connection())

    response = signed_in_client.post(
        "/panel/connections/line1_opcua/groups", data=_group_form()
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/panel/connections/line1_opcua"
    group = stores.config.get_tag_group("fast")
    assert group is not None
    assert (group.connection_id, group.interval_ms, group.timeout_ms) == (
        "line1_opcua",
        1000,
        800,
    )

    response = signed_in_client.post(
        "/panel/connections/line1_opcua/tags", data=_tag_form()
    )

    assert response.status_code == 303
    tag = stores.config.get_tag("line1_opcua_temperature")
    assert tag is not None
    assert tag.tag_group_id == "fast"
    assert tag.address == "ns=2;s=Temp"
    assert tag.value_type is ValueType.NUMERIC
    assert tag.metadata == {"unit": "C"}
    page = signed_in_client.get("/panel/connections/line1_opcua").text
    assert SAVED_FLASH in page
    assert "Temperature" in page
    assert "fast (1 s)" in page


def test_generated_tag_id_skips_archived_ids(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("line1_opcua_temperature"))
    stores.config.upsert_tag(_tag("line1_opcua_temperature_2"))
    stores.config.archive_tag("line1_opcua_temperature_2")

    response = signed_in_client.post(
        "/panel/connections/line1_opcua/tags", data=_tag_form(name="Temperature")
    )

    assert response.status_code == 303
    assert stores.config.get_tag("line1_opcua_temperature_3") is not None
    assert stores.config.is_archived_tag("line1_opcua_temperature_2") is True


def test_generate_tag_id_rules() -> None:
    assert generate_tag_id("c1", "Main motor  current (A)", set()) == (
        "c1_main_motor_current_a"
    )
    assert generate_tag_id("c1", "!!!", set()) == "c1_tag"
    assert generate_tag_id("c1", "T", {"c1_t", "c1_t_2"}) == "c1_t_3"
    long = generate_tag_id("c1", "x" * 300, set())
    assert len(long) <= 128


@pytest.mark.parametrize(
    ("interval", "timeout", "field", "message"),
    [
        ("500", "600", "timeout_ms", TIMEOUT_ORDER_ERROR),
        ("0", "10", "interval_ms", RANGE_ERROR),
        ("1000", "0", "timeout_ms", RANGE_ERROR),
        ("1_000", "10", "interval_ms", RANGE_ERROR),
        ("600001", "10", "interval_ms", RANGE_ERROR),
        ("1000", "-5", "timeout_ms", RANGE_ERROR),
        ("", "10", "interval_ms", RANGE_ERROR),
    ],
)
def test_timeout_longer_than_interval_rejected(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    interval: str,
    timeout: str,
    field: str,
    message: str,
) -> None:
    stores = _stores(fake_host)
    stores.config.upsert_connection(_connection())
    before = stores.config.revision()

    response = signed_in_client.post(
        "/panel/connections/line1_opcua/groups",
        data=_group_form(interval_ms=interval, timeout_ms=timeout),
    )

    assert response.status_code == 400
    assert message in response.text
    assert f'id="field-{field}-error"' in response.text
    assert stores.config.get_tag_group("fast") is None
    assert stores.config.revision() == before


def test_group_edit_keeps_id_and_connection(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_connection(_connection("other"))

    response = signed_in_client.post(
        "/panel/groups/fast",
        data=_group_form(id="hijack", interval_ms="2000", timeout_ms="500"),
    )

    assert response.status_code == 303
    group = stores.config.get_tag_group("fast")
    assert group is not None
    assert (group.interval_ms, group.timeout_ms) == (2000, 500)
    assert group.connection_id == "line1_opcua"
    assert stores.config.get_tag_group("hijack") is None
    # Unticked "enabled" disables the group.
    data = _group_form()
    del data["enabled"]
    signed_in_client.post("/panel/groups/fast", data=data)
    group = stores.config.get_tag_group("fast")
    assert group is not None and group.enabled is False


def test_duplicate_group_id_shows_field_error(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_connection(_connection("other"))

    response = signed_in_client.post(
        "/panel/connections/other/groups", data=_group_form(id="fast")
    )

    assert response.status_code == 400
    assert "A tag group with this ID already exists. Choose another ID." in (
        response.text
    )
    group = stores.config.get_tag_group("fast")
    assert group is not None and group.connection_id == "line1_opcua"


def test_invalid_group_id_rejected(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _stores(fake_host).config.upsert_connection(_connection())

    response = signed_in_client.post(
        "/panel/connections/line1_opcua/groups", data=_group_form(id="bad id")
    )

    assert response.status_code == 400
    assert "Use letters, digits, dot, dash or underscore (up to 128)." in response.text


def test_tag_last_read_shows_bad_quality_message(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("t_bad"))
    stores.config.upsert_tag(_tag("t_good"))
    fake_host.live_values.update(
        WorkerPollResult(
            execution=PollExecution(
                execution_id="p1",
                connection_id="line1_opcua",
                tag_group_id="fast",
                status=PollStatus.PARTIAL_FAILURE,
                started_at=NOW,
                finished_at=NOW,
                requested_tags=2,
                successful_tags=0,
                failed_tags=2,
            ),
            results=(
                TagResult.failure(
                    tag_id="t_bad",
                    error_code="opcua_read_failed",
                    error_message="Node ns=2;s=Temp does not exist. Check the address.",
                    received_at=NOW,
                ),
            ),
        )
    )

    page = signed_in_client.get("/panel/connections/line1_opcua").text

    bad_row = re.search(r'<tr[^>]*data-tag="t_bad"[^>]*>.*?</tr>', page, re.DOTALL)
    assert bad_row is not None
    assert "Bad" in bad_row.group(0)
    assert "Node ns=2;s=Temp does not exist. Check the address." in bad_row.group(0)
    unread = re.search(r'<tr[^>]*data-tag="t_good"[^>]*>.*?</tr>', page, re.DOTALL)
    assert unread is not None
    assert "Bad" not in unread.group(0)


def test_delete_group_archives_tags(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("t1"))
    stores.config.upsert_tag(_tag("t2"))

    response = signed_in_client.post("/panel/groups/fast/delete")

    assert response.status_code == 303
    assert response.headers["location"] == "/panel/connections/line1_opcua"
    assert stores.config.get_tag_group("fast") is None
    assert stores.config.is_archived_tag("t1") is True
    assert stores.config.is_archived_tag("t2") is True
    assert "deleted" in signed_in_client.get("/panel/connections/line1_opcua").text


def test_delete_tag_archives_it(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("t1"))

    response = signed_in_client.post("/panel/tags/t1/delete")

    assert response.status_code == 303
    assert stores.config.is_archived_tag("t1") is True
    assert stores.config.get_tag_group("fast") is not None


def test_tag_for_other_connection_group_rejected(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_connection(_connection("other"))
    stores.config.upsert_tag_group(_group("other_group", "other"))
    stores.config.upsert_tag(_tag("t1"))
    before = stores.config.revision()

    created = signed_in_client.post(
        "/panel/connections/line1_opcua/tags",
        data=_tag_form(tag_group_id="other_group"),
    )
    moved = signed_in_client.post(
        "/panel/tags/t1", data=_tag_form(tag_group_id="other_group")
    )

    assert created.status_code == 400
    assert moved.status_code == 400
    assert GROUP_ERROR in created.text
    assert GROUP_ERROR in moved.text
    assert stores.config.revision() == before
    tag = stores.config.get_tag("t1")
    assert tag is not None and tag.tag_group_id == "fast"


def test_tag_edit_keeps_id_and_other_metadata(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(
        TagConfig(
            id="t1",
            tag_group_id="fast",
            name="Old",
            address="ns=2;s=Old",
            value_type=ValueType.NUMERIC,
            metadata={"unit": "C", "note": "kept"},
        )
    )

    response = signed_in_client.post(
        "/panel/tags/t1",
        data=_tag_form(name="New name", unit="", value_type="integer", id="x"),
    )

    assert response.status_code == 303
    tag = stores.config.get_tag("t1")
    assert tag is not None
    assert tag.name == "New name"
    assert tag.value_type is ValueType.INTEGER
    assert tag.metadata == {"note": "kept"}
    assert stores.config.get_tag("line1_opcua_new_name") is None
    assert stores.config.get_tag("x") is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "  "}, "Enter a name."),
        ({"address": ""}, "Enter an address."),
        ({"value_type": "float128"}, "Choose one of the listed options."),
    ],
)
def test_tag_form_errors(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    overrides: dict[str, str],
    message: str,
) -> None:
    stores = _seed(fake_host)

    response = signed_in_client.post(
        "/panel/connections/line1_opcua/tags", data=_tag_form(**overrides)
    )

    assert response.status_code == 400
    assert message in response.text
    assert stores.config.all_tag_ids() == set()


def test_forms_render_inline_for_htmx(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("t1"))
    htmx = {"HX-Request": "true"}

    new_tag = signed_in_client.get(
        "/panel/connections/line1_opcua/tags/new", headers=htmx
    )
    edit_tag = signed_in_client.get("/panel/tags/t1/edit", headers=htmx)
    new_group = signed_in_client.get(
        "/panel/connections/line1_opcua/groups/new", headers=htmx
    )
    edit_group = signed_in_client.get("/panel/groups/fast/edit", headers=htmx)

    for response in (new_tag, edit_tag, new_group, edit_group):
        assert response.status_code == 200
        assert "<html" not in response.text
    assert 'action="/panel/connections/line1_opcua/tags"' in new_tag.text
    assert "fast (1 s)" in new_tag.text
    assert 'action="/panel/tags/t1"' in edit_tag.text
    assert 'value="ns=2;s=Temp"' in edit_tag.text
    assert 'action="/panel/connections/line1_opcua/groups"' in new_group.text
    assert 'action="/panel/groups/fast"' in edit_group.text
    assert 'action="/panel/groups/fast/delete"' in edit_group.text
    full = signed_in_client.get("/panel/tags/t1/edit")
    assert "<html" in full.text
    assert 'action="/panel/tags/t1"' in full.text


def test_unknown_items_are_404(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _stores(fake_host)
    for path in (
        "/panel/groups/nope/edit",
        "/panel/tags/nope/edit",
        "/panel/connections/nope/tags/new",
        "/panel/connections/nope/groups/new",
    ):
        assert signed_in_client.get(path).status_code == 404
    for path in (
        "/panel/connections/nope/groups",
        "/panel/connections/nope/tags",
        "/panel/groups/nope",
        "/panel/groups/nope/delete",
        "/panel/tags/nope",
        "/panel/tags/nope/delete",
    ):
        assert signed_in_client.post(path, data={}).status_code == 404


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/panel/connections/line1_opcua/groups", _group_form(id="g2")),
        ("/panel/groups/fast", _group_form()),
        ("/panel/groups/fast/delete", {}),
        ("/panel/connections/line1_opcua/tags", _tag_form()),
        ("/panel/tags/t1", _tag_form()),
        ("/panel/tags/t1/delete", {}),
    ],
)
def test_cross_site_posts_are_refused(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    path: str,
    data: dict[str, str],
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("t1"))
    before = stores.config.revision()

    response = signed_in_client.post(
        path, data=data, headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert stores.config.revision() == before


def test_tag_routes_require_sign_in(panel_client: TestClient) -> None:
    assert panel_client.get("/panel/tags/t1/edit").status_code == 303
    assert panel_client.post("/panel/tags/t1", data={}).status_code == 303


def test_tag_last_read_marks_old_good_values_stale(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("t_old"))
    stores.config.upsert_tag(_tag("t_new"))
    now = datetime.now(UTC)
    old = now - timedelta(seconds=30)
    fake_host.live_values.update(
        WorkerPollResult(
            execution=PollExecution(
                execution_id="p2",
                connection_id="line1_opcua",
                tag_group_id="fast",
                status=PollStatus.SUCCESS,
                started_at=now,
                finished_at=now,
                requested_tags=2,
                successful_tags=2,
                failed_tags=0,
            ),
            results=(
                TagResult.success(
                    tag_id="t_old",
                    value=TagValue.numeric(1.0),
                    source_timestamp=old,
                    received_at=old,
                ),
                TagResult.success(
                    tag_id="t_new",
                    value=TagValue.numeric(2.0),
                    source_timestamp=now,
                    received_at=now,
                ),
            ),
        )
    )

    page = signed_in_client.get("/panel/connections/line1_opcua").text

    old_row = re.search(r'<tr[^>]*data-tag="t_old"[^>]*>.*?</tr>', page, re.DOTALL)
    new_row = re.search(r'<tr[^>]*data-tag="t_new"[^>]*>.*?</tr>', page, re.DOTALL)
    assert old_row is not None and new_row is not None
    assert "state-degraded" in old_row.group(0) and "Stale" in old_row.group(0)
    assert "state-normal" in new_row.group(0) and "Good" in new_row.group(0)
