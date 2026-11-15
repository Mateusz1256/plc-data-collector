"""Tests for the Panel's audit log lines: codes and IDs, never secrets."""

from __future__ import annotations

import json
import logging
import re

import pytest
from fastapi.testclient import TestClient
from panel_support import FakeHost

from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import (
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
    ValueType,
)

PASSWORD = "audit-secret-pw"


@pytest.fixture(autouse=True)
def info_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)


def _stores(host: FakeHost) -> Stores:
    assert host.stores is not None
    return host.stores


def _records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == message]


def _all_logs(caplog: pytest.LogCaptureFixture) -> str:
    return caplog.text + "".join(repr(vars(r)) for r in caplog.records)


def _seed(host: FakeHost) -> Stores:
    stores = _stores(host)
    stores.config.upsert_connection(
        ConnectionConfig(
            id="line1",
            name="Line 1",
            protocol="opcua",
            endpoint="opc.tcp://plc:4840",
            enabled=False,
            protocol_options={"username": "op", "password": PASSWORD},
        )
    )
    stores.config.upsert_tag_group(
        TagGroupConfig(id="fast", connection_id="line1", interval_ms=1000)
    )
    stores.config.upsert_tag(
        TagConfig(
            id="t1",
            tag_group_id="fast",
            name="T1",
            address="ns=2;s=T1",
            value_type=ValueType.NUMERIC,
        )
    )
    return stores


def test_failed_sign_in_logs_the_client_only(
    panel_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    panel_client.post("/panel/login", data={"password": "wrong-guess-123"})

    records = _records(caplog, "panel_sign_in_failed")
    assert len(records) == 1
    assert records[0].client == "testclient"  # type: ignore[attr-defined]
    assert "wrong-guess-123" not in _all_logs(caplog)


def test_configuration_changes_are_logged_with_revision(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stores = _seed(fake_host)

    assert signed_in_client.post("/panel/connections/line1/enable").status_code == 303
    assert signed_in_client.post("/panel/tags/t1/delete").status_code == 303
    assert signed_in_client.post("/panel/groups/fast/delete").status_code == 303
    assert signed_in_client.post("/panel/connections/line1/delete").status_code == 303

    changes = [
        (r.kind, r.id, r.action, r.revision)  # type: ignore[attr-defined]
        for r in _records(caplog, "configuration_changed")
    ]
    final = stores.config.revision()
    assert changes == [
        ("connection", "line1", "enabled", final - 3),
        ("tag", "t1", "deleted", final - 2),
        ("tag_group", "fast", "deleted", final - 1),
        ("connection", "line1", "deleted", final),
    ]
    assert PASSWORD not in _all_logs(caplog)


def test_import_is_logged_with_revision(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stores = _seed(fake_host)
    document = json.loads(signed_in_client.get("/panel/settings/export").content)
    document["connections"][0]["name"] = "Renamed"
    review = signed_in_client.post(
        "/panel/settings/import",
        files={"config_file": ("c.json", json.dumps(document).encode(), "x/json")},
    )
    token = re.search(r'name="token" value="([^"]+)"', review.text)
    assert token is not None

    signed_in_client.post(
        "/panel/settings/import/confirm", data={"token": token.group(1)}
    )

    records = _records(caplog, "configuration_imported")
    assert [r.revision for r in records] == [stores.config.revision()]  # type: ignore[attr-defined]
    assert PASSWORD not in _all_logs(caplog)


def test_api_keys_are_logged_by_id_and_prefix(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = signed_in_client.post("/panel/settings/api-keys", data={"name": "BI"})
    key = re.search(r'value="(pdc_[^"]+)"', response.text)
    assert key is not None
    info = _stores(fake_host).api_keys.list()[0]

    signed_in_client.post(f"/panel/settings/api-keys/{info.id}/revoke")
    signed_in_client.post(f"/panel/settings/api-keys/{info.id}/revoke")

    created = _records(caplog, "api_key_created")
    revoked = _records(caplog, "api_key_revoked")
    assert [(r.id, r.prefix) for r in created] == [(info.id, info.prefix)]  # type: ignore[attr-defined]
    assert [(r.id, r.prefix) for r in revoked] == [(info.id, info.prefix)]  # type: ignore[attr-defined]
    assert key.group(1) not in _all_logs(caplog)


def test_apply_is_logged_and_unexpected_failure_flashes(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(fake_host)
    fake_host.apply_error = RuntimeError("boom")

    response = signed_in_client.post("/panel/apply")

    assert response.status_code == 303
    assert _records(caplog, "apply_requested")
    failed = _records(caplog, "apply_request_failed")
    assert failed and failed[0].error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert "Changes could not be applied." in signed_in_client.get("/panel/live").text
