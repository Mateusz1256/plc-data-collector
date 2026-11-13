"""Tests for Panel configuration export and import."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from panel_support import FakeHost

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.config_transfer import MAX_IMPORT_BYTES
from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import (
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.panel import routes_settings

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
CROSS_SITE = {"Origin": "https://evil.example"}
SECRET = "hunter2-very-secret"
IMPORTED = "Configuration imported. Apply changes to start using it."


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_settings, "utc_now", lambda: NOW)


def _stores(host: FakeHost) -> Stores:
    assert host.stores is not None
    return host.stores


def _connection(
    connection_id: str = "line1", *, name: str = "Line 1"
) -> ConnectionConfig:
    return ConnectionConfig(
        id=connection_id,
        name=name,
        protocol="opcua",
        endpoint="opc.tcp://plc-sim:4840",
        protocol_options={"username": "operator", "password": SECRET},
    )


def _group(group_id: str = "fast", connection_id: str = "line1") -> TagGroupConfig:
    return TagGroupConfig(id=group_id, connection_id=connection_id, interval_ms=1000)


def _tag(tag_id: str = "temp", group_id: str = "fast") -> TagConfig:
    return TagConfig(
        id=tag_id,
        tag_group_id=group_id,
        name="Temperature",
        address="ns=2;s=Temp",
        value_type=ValueType.NUMERIC,
    )


def _seed(host: FakeHost) -> None:
    _stores(host).config.replace_all(
        CollectorConfig(
            connections=(_connection(),),
            tag_groups=(_group(),),
            tags=(_tag(),),
        )
    )


def _upload(
    client: TestClient, data: bytes, headers: dict[str, str] | None = None
) -> Response:
    response: Response = client.post(
        "/panel/settings/import",
        files={"config_file": ("config.json", data, "application/json")},
        headers=headers,
    )
    return response


def _export_json(client: TestClient) -> bytes:
    return bytes(client.get("/panel/settings/export").content)


def _token(html: str) -> str:
    match = re.search(r'<input type="hidden" name="token" value="([^"]+)">', html)
    assert match
    return match.group(1)


def _confirm(client: TestClient, token: str) -> Response:
    response: Response = client.post(
        "/panel/settings/import/confirm", data={"token": token}
    )
    return response


def _renamed_export(client: TestClient, name: str = "Renamed") -> bytes:
    document = json.loads(_export_json(client))
    document["connections"][0]["name"] = name
    return json.dumps(document).encode()


def test_settings_page_has_three_sections(signed_in_client: TestClient) -> None:
    response = signed_in_client.get("/panel/settings")
    assert response.status_code == 200
    for text in ("API keys", "Data retention", "Configuration file", "Review import"):
        assert text in response.text
    assert (
        "Connections that already exist keep theirs; enter them for new "
        "connections after importing." in response.text
    )


def test_export_downloads_without_secrets(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    response = signed_in_client.get("/panel/settings/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"] == (
        'attachment; filename="plc-data-collector-config-20261113-120000.json"'
    )
    assert SECRET not in response.text
    document = json.loads(response.text)
    assert document["connections"][0]["id"] == "line1"
    assert "password" not in document["connections"][0]["protocol_options"]


def test_import_review_shows_added_changed_removed(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    document = json.loads(_export_json(signed_in_client))
    document["connections"][0]["name"] = "Renamed"
    document["tags"][0]["id"] = "pressure"
    before = _stores(fake_host).config.revision()

    response = _upload(signed_in_client, json.dumps(document).encode())

    assert response.status_code == 200
    text = response.text
    assert "Review import" in text
    assert "Replace configuration" in text
    for heading in ("Added", "Changed", "Removed"):
        assert heading in text
    assert "pressure" in text and "Renamed" in text and "temp" in text
    assert SECRET not in text
    assert _stores(fake_host).config.revision() == before


def test_review_page_holds_no_part_of_the_file(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    document = json.loads(_export_json(signed_in_client))
    document["connections"][0]["name"] = "</textarea><script>alert(1)</script>"
    document["connections"][0]["protocol_options"]["password"] = "file-only-pw-42"
    document["tags"][0]["address"] = "ns=2;s=OnlyInTheFile"

    response = _upload(signed_in_client, json.dumps(document).encode())

    assert response.status_code == 200
    text = response.text
    assert "<script>alert(1)</script>" not in text
    assert "file-only-pw-42" not in text
    assert "OnlyInTheFile" not in text
    assert "config_json" not in text and "<textarea" not in text
    assert len(_token(text)) >= 40


def test_import_confirm_replaces_and_bumps_revision(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    review = _upload(signed_in_client, _renamed_export(signed_in_client))
    before = _stores(fake_host).config.revision()

    response = _confirm(signed_in_client, _token(review.text))

    assert response.status_code == 303
    assert response.headers["location"] == "/panel/settings"
    assert _stores(fake_host).config.revision() > before
    config, _ = _stores(fake_host).config.load_active()
    assert config.connections[0].name == "Renamed"
    # The stored secret survives because the file omitted it.
    assert config.connections[0].protocol_options["password"] == SECRET
    assert IMPORTED in signed_in_client.get("/panel/settings").text


def test_import_token_works_only_once(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    token = _token(_upload(signed_in_client, _renamed_export(signed_in_client)).text)
    assert _confirm(signed_in_client, token).status_code == 303
    revision = _stores(fake_host).config.revision()

    again = _confirm(signed_in_client, token)

    assert again.status_code == 400
    assert "This import has expired. Upload the file again." in again.text
    assert _stores(fake_host).config.revision() == revision


@pytest.mark.parametrize(
    "token", ["", "unknown-token", "x" * 5000], ids=["empty", "unknown", "long"]
)
def test_unknown_import_token_is_refused(
    signed_in_client: TestClient, fake_host: FakeHost, token: str
) -> None:
    _seed(fake_host)
    before = _stores(fake_host).config.revision()

    response = _confirm(signed_in_client, token)

    assert response.status_code == 400
    assert "This import has expired. Upload the file again." in response.text
    assert _stores(fake_host).config.revision() == before


def test_expired_import_token_is_refused(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(fake_host)
    clock = [1000.0]
    monkeypatch.setattr(routes_settings, "monotonic", lambda: clock[0])
    token = _token(_upload(signed_in_client, _renamed_export(signed_in_client)).text)
    before = _stores(fake_host).config.revision()
    clock[0] += 15 * 60 + 1

    response = _confirm(signed_in_client, token)

    assert response.status_code == 400
    assert "This import has expired. Upload the file again." in response.text
    assert _stores(fake_host).config.revision() == before


def test_import_refused_when_configuration_changed_after_review(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    token = _token(_upload(signed_in_client, _renamed_export(signed_in_client)).text)
    _stores(fake_host).config.upsert_tag(_tag("pressure"))
    before = _stores(fake_host).config.revision()

    response = _confirm(signed_in_client, token)

    assert response.status_code == 409
    assert (
        "The configuration changed after you reviewed this import. Upload the "
        "file again to see the new differences."
    ) in response.text
    assert _stores(fake_host).config.revision() == before
    assert _stores(fake_host).config.get_tag("pressure") is not None


def test_two_megabyte_configuration_imports_end_to_end(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    document = json.loads(_export_json(signed_in_client))
    document["tags"] = [
        {
            "id": f"tag_{index}",
            "tag_group_id": "fast",
            "name": f"Tag {index}",
            "address": f"ns=2;s=Tag{index}",
            "value_type": "numeric",
            "metadata": {"description": "d" * 4000},
        }
        for index in range(520)
    ]
    data = json.dumps(document).encode()
    assert 2 * 1024 * 1024 <= len(data) < MAX_IMPORT_BYTES

    review = _upload(signed_in_client, data)
    assert review.status_code == 200
    response = _confirm(signed_in_client, _token(review.text))

    assert response.status_code == 303
    config, _ = _stores(fake_host).config.load_active()
    assert len(config.tags) == 520


def test_import_without_a_file_shows_the_settings_error(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    response = signed_in_client.post(
        "/panel/settings/import", data={"other": "x"}, files={"x": ("x", b"")}
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "Choose a configuration file to import." in response.text


def test_pending_imports_keep_at_most_eight() -> None:
    now = [0.0]
    pending = routes_settings.PendingImports(clock=lambda: now[0])
    config = CollectorConfig(connections=(), tag_groups=(), tags=())
    tokens = [pending.add(config, revision) for revision in range(9)]

    assert pending.take(tokens[0]) is None
    entry = pending.take(tokens[8])
    assert entry is not None and entry.revision == 8
    assert pending.take(tokens[8]) is None
    assert pending.take(tokens[1]) is not None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"{not json", "line 1"),
        (b"[]", "$"),
        (
            json.dumps(
                {
                    "format_version": 1,
                    "connections": [
                        {
                            "id": "c",
                            "name": "C",
                            "protocol": "opcua",
                            "endpoint": "opc.tcp://x:4840",
                        }
                    ],
                    "tag_groups": [
                        {
                            "id": "g",
                            "connection_id": "missing",
                            "interval_ms": 1000,
                        }
                    ],
                    "tags": [
                        {
                            "id": "t",
                            "tag_group_id": "g",
                            "name": "T",
                            "address": "a",
                            "value_type": "numeric",
                        }
                    ],
                }
            ).encode(),
            "missing",
        ),
        (b" " * (MAX_IMPORT_BYTES + 1), "5 MB"),
    ],
    ids=["not-json", "list-root", "unknown-reference", "too-large"],
)
def test_import_rejects_invalid_files_without_changes(
    signed_in_client: TestClient, fake_host: FakeHost, data: bytes, expected: str
) -> None:
    _seed(fake_host)
    before = _stores(fake_host).config.revision()
    response = _upload(signed_in_client, data)
    assert response.status_code == 400
    assert expected in response.text
    assert _stores(fake_host).config.revision() == before


def test_import_requires_login(panel_client: TestClient) -> None:
    response = _upload(panel_client, b"{}")
    assert response.status_code in (303, 401, 403)
    assert "/panel/login" in response.headers.get("location", "/panel/login")
    assert panel_client.get("/panel/settings").status_code == 303
    assert panel_client.get("/panel/settings/export").status_code == 303
    confirm = panel_client.post("/panel/settings/import/confirm", data={"token": "x"})
    assert confirm.status_code in (303, 401, 403)


def test_cross_site_posts_are_refused(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    before = _stores(fake_host).config.revision()
    document = _export_json(signed_in_client)
    upload = _upload(signed_in_client, document, headers=CROSS_SITE)
    token = _token(_upload(signed_in_client, _renamed_export(signed_in_client)).text)
    confirm = signed_in_client.post(
        "/panel/settings/import/confirm",
        data={"token": token},
        headers=CROSS_SITE,
    )
    assert upload.status_code == 403
    assert confirm.status_code == 403
    assert _stores(fake_host).config.revision() == before
