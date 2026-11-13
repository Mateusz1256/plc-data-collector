"""Tests for the API key store and the Settings API keys section."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from panel_support import FakeHost
from sqlalchemy import Engine, select

from plc_data_collector.domain import ConfigurationError
from plc_data_collector.persistence import ApiKeyStore
from plc_data_collector.persistence.api_keys import MAX_TRACKED_KEYS
from plc_data_collector.persistence.schema import api_keys

CROSS_SITE = {"Origin": "https://evil.example"}
START = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(fake_host: FakeHost, clock: Clock) -> ApiKeyStore:
    return ApiKeyStore(fake_host.engine, clock=clock)


def _rows(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as connection:
        return [dict(r._mapping) for r in connection.execute(select(api_keys))]


def test_create_returns_plaintext_once_and_stores_hash_only(
    store: ApiKeyStore, fake_host: FakeHost
) -> None:
    info, key = store.create("  grafana  ")
    assert key.startswith("pdc_") and len(key) == 52
    assert info.name == "grafana"
    assert info.prefix == key[:8]
    rows = _rows(fake_host.engine)
    assert len(rows) == 1
    assert key not in {str(v) for v in rows[0].values()}
    assert rows[0]["prefix"] == key[:8]
    assert len(str(rows[0]["key_hash"])) == 64
    assert all(key not in repr(i) for i in store.list())


@pytest.mark.parametrize("name", ["", "   ", "x" * 129])
def test_invalid_name_rejected(store: ApiKeyStore, name: str) -> None:
    with pytest.raises(ConfigurationError) as error:
        store.create(name)
    assert error.value.code == "invalid_api_key_name"


def test_authenticate_valid_unknown_revoked(store: ApiKeyStore) -> None:
    info, key = store.create("app")
    found = store.authenticate(key)
    assert found is not None and found.id == info.id
    assert store.authenticate("pdc_" + "0" * 48) is None
    for bad in ("", "abc", "xxx_" + key[4:], key + "0", key[:-1]):
        assert store.authenticate(bad) is None
    store.revoke(info.id)
    assert store.authenticate(key) is None


def test_bad_format_does_not_touch_the_database(store: ApiKeyStore) -> None:
    store._engine = None  # type: ignore[assignment]
    assert store.authenticate("") is None
    assert store.authenticate("nope") is None
    assert store.authenticate("pdc_short") is None


def test_last_used_throttled(
    store: ApiKeyStore, clock: Clock, fake_host: FakeHost
) -> None:
    _, key = store.create("app")
    store.authenticate(key)
    assert _rows(fake_host.engine)[0]["last_used_at"] == START
    clock.now = START + timedelta(seconds=30)
    store.authenticate(key)
    assert _rows(fake_host.engine)[0]["last_used_at"] == START
    clock.now = START + timedelta(seconds=61)
    store.authenticate(key)
    assert _rows(fake_host.engine)[0]["last_used_at"] == START + timedelta(seconds=61)


def test_throttle_map_is_bounded_and_dropped_on_revoke(
    store: ApiKeyStore, clock: Clock
) -> None:
    info, key = store.create("app")
    store.authenticate(key)
    assert len(store._last_written) == 1
    store.revoke(info.id)
    assert store._last_written == {}
    for i in range(MAX_TRACKED_KEYS + 50):
        clock.now += timedelta(seconds=1)
        store._touch(type(info)(f"id{i}", "n", "p", START, None, None))
    assert len(store._last_written) <= MAX_TRACKED_KEYS


def test_duplicate_active_name_rejected(store: ApiKeyStore) -> None:
    first, _ = store.create("grafana")
    with pytest.raises(ConfigurationError) as error:
        store.create("grafana")
    assert error.value.code == "duplicate_api_key_name"
    store.revoke(first.id)
    store.create("grafana")


def test_list_newest_first_with_revoked_and_revoke_idempotent(
    store: ApiKeyStore, clock: Clock
) -> None:
    a, _ = store.create("a")
    clock.now += timedelta(seconds=1)
    b, _ = store.create("b")
    store.revoke(a.id)
    first_revoked = store.list()[1].revoked_at
    clock.now += timedelta(seconds=5)
    store.revoke(a.id)
    store.revoke("missing")
    listed = store.list()
    assert [k.id for k in listed] == [b.id, a.id]
    assert listed[1].revoked_at == first_revoked is not None


def _key_from(html: str) -> str:
    match = re.search(
        r'id="new-api-key" aria-label="New API key" value="([^"]+)"', html
    )
    assert match
    return match.group(1)


def test_panel_reveal_shown_once(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    response = signed_in_client.post(
        "/panel/settings/api-keys", data={"name": "grafana-hall-2"}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    key = _key_from(response.text)
    assert (
        "Key for grafana-hall-2 created. Copy it now: it won&#39;t be shown again."
        in response.text
        or "Key for grafana-hall-2 created. Copy it now: it won't be shown again."
        in response.text
    )
    assert "Copy key" in response.text and "Done" in response.text
    assert "X-API-Key" in response.text
    assert key[:8] in response.text

    again = signed_in_client.get("/panel/settings")
    assert key not in again.text
    assert "New API key" not in again.text
    assert "grafana-hall-2" in again.text
    assert key not in str(signed_in_client.cookies)
    assert key not in str(response.headers)


def test_panel_create_errors(signed_in_client: TestClient) -> None:
    signed_in_client.post("/panel/settings/api-keys", data={"name": "dup"})
    duplicate = signed_in_client.post("/panel/settings/api-keys", data={"name": "dup"})
    assert duplicate.status_code == 400
    assert "already exists" in duplicate.text
    assert "New API key" not in duplicate.text
    empty = signed_in_client.post("/panel/settings/api-keys", data={"name": " "})
    assert empty.status_code == 400


def test_panel_revoke(signed_in_client: TestClient, fake_host: FakeHost) -> None:
    assert fake_host.stores is not None
    info, key = fake_host.stores.api_keys.create("oee")
    response = signed_in_client.post(f"/panel/settings/api-keys/{info.id}/revoke")
    assert response.status_code == 303
    assert response.headers["location"] == "/panel/settings"
    assert fake_host.stores.api_keys.authenticate(key) is None
    page = signed_in_client.get("/panel/settings")
    assert "Revoked " in page.text
    assert f"/api-keys/{info.id}/revoke" not in page.text


def test_panel_cross_site_refused(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    assert fake_host.stores is not None
    info, key = fake_host.stores.api_keys.create("oee")
    created = signed_in_client.post(
        "/panel/settings/api-keys", data={"name": "evil"}, headers=CROSS_SITE
    )
    assert created.status_code == 403
    revoked = signed_in_client.post(
        f"/panel/settings/api-keys/{info.id}/revoke", headers=CROSS_SITE
    )
    assert revoked.status_code == 403
    assert fake_host.stores.api_keys.authenticate(key) is not None
    assert len(fake_host.stores.api_keys.list()) == 1


def test_panel_requires_sign_in(panel_client: TestClient) -> None:
    response = panel_client.post("/panel/settings/api-keys", data={"name": "x"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/panel/login")
