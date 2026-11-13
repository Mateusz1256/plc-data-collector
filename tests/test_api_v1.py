"""Tests for the read-only data API (/api/v1) and the reading queries behind it."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from panel_support import PANEL_SESSION_SECRET, FakeHost
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from plc_data_collector.api import WebOptions, create_api_app, data_v1
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
from plc_data_collector.persistence import (
    ReadingRepository,
    TagReadingRecord,
    create_database_engine,
    run_migrations,
)
from plc_data_collector.persistence.reading_queries import ReadingQueryRepository
from plc_data_collector.runtime.connection_worker import WorkerPollResult

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
MISSING_KEY = {"detail": "Send an API key in the X-API-Key header."}
INVALID_KEY = {"detail": "API key is not valid or was revoked."}
NAIVE = {"detail": "Include a timezone, for example 2026-11-12T10:00:00Z."}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_v1, "utc_now", lambda: NOW)


def _stores(host: FakeHost) -> Stores:
    assert host.stores is not None
    return host.stores


def _seed_config(stores: Stores) -> None:
    stores.config.upsert_connection(
        ConnectionConfig(
            id="line1",
            name="Line 1",
            protocol="opcua",
            endpoint="opc.tcp://plc:4840",
            protocol_options={"password": "s3cret-pass"},
        )
    )
    stores.config.upsert_tag_group(
        TagGroupConfig(id="fast", connection_id="line1", interval_ms=1000)
    )
    for tag_id, value_type, unit in (
        ("temperature", ValueType.NUMERIC, "°C"),
        ("count", ValueType.INTEGER, None),
        ("running", ValueType.BOOLEAN, None),
        ("recipe", ValueType.TEXT, None),
        ("blob", ValueType.BINARY, None),
    ):
        stores.config.upsert_tag(
            TagConfig(
                id=tag_id,
                tag_group_id="fast",
                name=tag_id.capitalize(),
                address=f"ns=2;s={tag_id}",
                value_type=value_type,
                metadata={"unit": unit} if unit else {},
            )
        )


def _good(tag_id: str, value: TagValue, at: datetime) -> TagResult:
    return TagResult.success(
        tag_id=tag_id, value=value, source_timestamp=at, received_at=at
    )


def _save(engine: Engine, results: list[TagResult]) -> None:
    ReadingRepository(engine).save_tag_readings(
        TagReadingRecord(
            event_id=f"e{index}-{result.tag_id}-{result.received_at.isoformat()}",
            connection_id="line1",
            tag_group_id="fast",
            result=result,
        )
        for index, result in enumerate(results)
    )


@pytest.fixture
def api_client(fake_host: FakeHost) -> Iterator[TestClient]:
    app = create_api_app(
        fake_host.api_state,
        web=WebOptions(
            host=fake_host, admin_password="pw", session_secret=PANEL_SESSION_SECRET
        ),
    )
    with TestClient(app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def api_key(fake_host: FakeHost) -> str:
    _, key = _stores(fake_host).api_keys.create("Historian")
    return key


@pytest.fixture
def auth(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def test_requires_key_401_messages(api_client: TestClient, api_key: str) -> None:
    for path in ("/api/v1/tags", "/api/v1/tags/x", "/api/v1/tags/x/history"):
        missing = api_client.get(path)
        assert missing.status_code == 401
        assert missing.json() == MISSING_KEY
        empty = api_client.get(path, headers={"X-API-Key": ""})
        assert empty.json() == MISSING_KEY
    # Wrong format and right format with an unknown key fail the same way.
    for bad in ("nope", "pdc_" + "0" * 48, api_key[:-1] + "x"):
        response = api_client.get("/api/v1/tags", headers={"X-API-Key": bad})
        assert response.status_code == 401
        assert response.json() == INVALID_KEY
        assert bad not in response.text


def test_revoked_key_rejected(
    api_client: TestClient, fake_host: FakeHost, api_key: str
) -> None:
    stores = _stores(fake_host)
    assert api_client.get("/api/v1/tags", headers={"X-API-Key": api_key}).is_success
    (info,) = stores.api_keys.list()
    stores.api_keys.revoke(info.id)
    response = api_client.get("/api/v1/tags", headers={"X-API-Key": api_key})
    assert response.status_code == 401
    assert response.json() == INVALID_KEY


def test_stores_not_ready_503(
    api_client: TestClient, fake_host: FakeHost, auth: dict[str, str]
) -> None:
    fake_host.stores = None
    response = api_client.get("/api/v1/tags", headers=auth)
    assert response.status_code == 503


def test_health_open_without_key(api_client: TestClient) -> None:
    assert api_client.get("/health/live").status_code == 200
    assert api_client.get("/api/about").status_code == 200
    schema = api_client.get("/openapi.json").json()
    v1_paths = [path for path in schema["paths"] if path.startswith("/api/v1/")]
    assert sorted(v1_paths) == [
        "/api/v1/tags",
        "/api/v1/tags/{tag_id}",
        "/api/v1/tags/{tag_id}/history",
    ]
    for path in v1_paths:
        assert schema["paths"][path]["get"]["tags"] == ["Data"]


def test_v1_absent_without_web() -> None:
    app = create_api_app()
    with TestClient(app) as client:
        assert client.get("/api/v1/tags").status_code == 404


def test_list_tags_with_current_from_cache(
    api_client: TestClient, fake_host: FakeHost, auth: dict[str, str]
) -> None:
    stores = _stores(fake_host)
    _seed_config(stores)
    stores.config.archive_tag("recipe")
    at = NOW - timedelta(seconds=2)
    fake_host.live_values.update(
        WorkerPollResult(
            execution=PollExecution(
                execution_id="x1",
                connection_id="line1",
                tag_group_id="fast",
                status=PollStatus.PARTIAL_FAILURE,
                started_at=at,
                finished_at=at,
                requested_tags=2,
                successful_tags=1,
                failed_tags=1,
            ),
            results=(
                _good("temperature", TagValue.numeric(21.5), at),
                TagResult.failure(
                    tag_id="count",
                    error_code="opcua_bad_node",
                    error_message="Bad node",
                    received_at=at,
                ),
            ),
        )
    )

    response = api_client.get("/api/v1/tags", headers=auth)

    assert response.status_code == 200, response.text
    assert "s3cret-pass" not in response.text
    assert "opc.tcp" not in response.text
    tags = {tag["id"]: tag for tag in response.json()["tags"]}
    assert sorted(tags) == ["blob", "count", "running", "temperature"]
    assert tags["temperature"] == {
        "id": "temperature",
        "name": "Temperature",
        "connection_id": "line1",
        "tag_group_id": "fast",
        "address": "ns=2;s=temperature",
        "value_type": "numeric",
        "unit": "°C",
        "enabled": True,
        "current": {
            "value": 21.5,
            "quality": "good",
            "received_at": "2026-11-13T11:59:58Z",
            "source_timestamp": "2026-11-13T11:59:58Z",
            "error_code": None,
        },
    }
    assert tags["count"]["current"] == {
        "value": None,
        "quality": "bad",
        "received_at": "2026-11-13T11:59:58Z",
        "source_timestamp": None,
        "error_code": "opcua_bad_node",
    }
    assert tags["running"]["current"] is None
    assert tags["running"]["unit"] is None

    single = api_client.get("/api/v1/tags/temperature", headers=auth)
    assert single.status_code == 200
    assert single.json() == tags["temperature"]


def test_tag_404_for_archived_in_current_but_history_works(
    api_client: TestClient, fake_host: FakeHost, auth: dict[str, str]
) -> None:
    stores = _stores(fake_host)
    _seed_config(stores)
    _save(
        fake_host.engine,
        [_good("count", TagValue.integer(7), NOW - timedelta(minutes=5))],
    )
    stores.config.archive_tag("count")

    current = api_client.get("/api/v1/tags/count", headers=auth)
    assert current.status_code == 404
    assert current.json() == {"detail": "Tag not found."}
    unknown = api_client.get("/api/v1/tags/never", headers=auth)
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Tag not found."}

    history = api_client.get("/api/v1/tags/count/history", headers=auth)
    assert history.status_code == 200, history.text
    body = history.json()
    assert body["archived"] is True
    assert body["readings"][0]["value"] == 7

    never = api_client.get("/api/v1/tags/never/history", headers=auth)
    assert never.status_code == 404
    assert never.json() == {"detail": "Tag not found."}


def test_history_range_order_limit_truncated(
    api_client: TestClient, fake_host: FakeHost, auth: dict[str, str]
) -> None:
    stores = _stores(fake_host)
    _seed_config(stores)
    base = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)
    # Saved out of order; one reading sits exactly on "to" and is excluded.
    offsets = [3, 0, 4, 1, 2, 10]
    _save(
        fake_host.engine,
        [
            _good(
                "temperature", TagValue.numeric(float(o)), base + timedelta(minutes=o)
            )
            for o in offsets
        ]
        + [
            TagResult.failure(
                tag_id="temperature",
                error_code="timeout",
                error_message="Timed out",
                received_at=base + timedelta(minutes=5),
            ),
            _good("running", TagValue.boolean(True), base + timedelta(minutes=1)),
        ],
    )
    params = {"from": "2026-11-12T10:00:00Z", "to": "2026-11-12T10:10:00Z"}

    full = api_client.get(
        "/api/v1/tags/temperature/history", params=params, headers=auth
    )

    assert full.status_code == 200, full.text
    body = full.json()
    assert body["tag_id"] == "temperature"
    assert body["archived"] is False
    assert body["from"] == "2026-11-12T10:00:00Z"
    assert body["to"] == "2026-11-12T10:10:00Z"
    assert body["truncated"] is False
    assert [r["value"] for r in body["readings"]] == [0.0, 1.0, 2.0, 3.0, 4.0, None]
    assert body["readings"][0] == {
        "received_at": "2026-11-12T10:00:00Z",
        "source_timestamp": "2026-11-12T10:00:00Z",
        "quality": "good",
        "value": 0.0,
        "error_code": None,
    }
    assert body["readings"][5]["error_code"] == "timeout"
    assert body["readings"][5]["quality"] == "bad"

    limited = api_client.get(
        "/api/v1/tags/temperature/history",
        params={**params, "limit": 3},
        headers=auth,
    ).json()
    assert limited["truncated"] is True
    assert [r["value"] for r in limited["readings"]] == [0.0, 1.0, 2.0]

    exact = api_client.get(
        "/api/v1/tags/temperature/history",
        params={**params, "limit": 6},
        headers=auth,
    ).json()
    assert exact["truncated"] is False
    assert len(exact["readings"]) == 6

    # An offset other than UTC selects the same instants.
    shifted = api_client.get(
        "/api/v1/tags/temperature/history",
        params={"from": "2026-11-12T11:01:00+01:00", "to": "2026-11-12T11:03:00+01:00"},
        headers=auth,
    ).json()
    assert [r["value"] for r in shifted["readings"]] == [1.0, 2.0]
    assert shifted["from"] == "2026-11-12T10:01:00Z"

    booleans = api_client.get(
        "/api/v1/tags/running/history", params=params, headers=auth
    ).json()
    assert booleans["readings"][0]["value"] is True

    for bad_limit in (0, 10_001):
        response = api_client.get(
            "/api/v1/tags/temperature/history",
            params={**params, "limit": bad_limit},
            headers=auth,
        )
        assert response.status_code == 422


def test_history_default_range_is_last_hour(
    api_client: TestClient, fake_host: FakeHost, auth: dict[str, str]
) -> None:
    _seed_config(_stores(fake_host))
    _save(
        fake_host.engine,
        [
            _good("count", TagValue.integer(1), NOW - timedelta(minutes=61)),
            _good("count", TagValue.integer(2), NOW - timedelta(minutes=59)),
        ],
    )
    body = api_client.get("/api/v1/tags/count/history", headers=auth).json()
    assert body["from"] == "2026-11-13T11:00:00Z"
    assert body["to"] == "2026-11-13T12:00:00Z"
    assert [r["value"] for r in body["readings"]] == [2]


def test_history_rejects_naive_datetime(
    api_client: TestClient, fake_host: FakeHost, auth: dict[str, str]
) -> None:
    _seed_config(_stores(fake_host))
    for params in (
        {"from": "2026-11-12T10:00:00"},
        {"to": "2026-11-12T10:00:00"},
        {"from": "2026-11-12T09:00:00Z", "to": "2026-11-12T10:00:00"},
    ):
        response = api_client.get(
            "/api/v1/tags/count/history", params=params, headers=auth
        )
        assert response.status_code == 422
        assert response.json() == NAIVE


def test_history_from_after_to_422(
    api_client: TestClient, fake_host: FakeHost, auth: dict[str, str]
) -> None:
    _seed_config(_stores(fake_host))
    for params in (
        {"from": "2026-11-12T11:00:00Z", "to": "2026-11-12T10:00:00Z"},
        {"from": "2026-11-12T10:00:00Z", "to": "2026-11-12T10:00:00Z"},
        {"from": "2026-11-14T10:00:00Z"},
    ):
        response = api_client.get(
            "/api/v1/tags/count/history", params=params, headers=auth
        )
        assert response.status_code == 422
        assert response.json() == {"detail": data_v1.FROM_AFTER_TO_MESSAGE}


def test_binary_value_base64(
    api_client: TestClient, fake_host: FakeHost, auth: dict[str, str]
) -> None:
    _seed_config(_stores(fake_host))
    payload = b"\x00\xffPLC"
    at = NOW - timedelta(minutes=1)
    _save(fake_host.engine, [_good("blob", TagValue.binary(payload), at)])
    fake_host.live_values.update(
        WorkerPollResult(
            execution=PollExecution(
                execution_id="x1",
                connection_id="line1",
                tag_group_id="fast",
                status=PollStatus.SUCCESS,
                started_at=at,
                finished_at=at,
                requested_tags=1,
                successful_tags=1,
            ),
            results=(_good("blob", TagValue.binary(payload), at),),
        )
    )
    encoded = base64.b64encode(payload).decode("ascii")

    history = api_client.get("/api/v1/tags/blob/history", headers=auth).json()
    current = api_client.get("/api/v1/tags/blob", headers=auth).json()

    assert history["readings"][0]["value"] == encoded
    assert current["current"]["value"] == encoded


def test_encode_value_types() -> None:
    assert data_v1.encode_value(TagValue.numeric(3)) == 3.0
    assert isinstance(data_v1.encode_value(TagValue.numeric(3)), float)
    assert data_v1.encode_value(TagValue.integer(3)) == 3
    assert data_v1.encode_value(TagValue.boolean(False)) is False
    assert data_v1.encode_value(TagValue.text("abc")) == "abc"
    assert data_v1.encode_value(TagValue.binary(b"\x01")) == "AQ=="
    assert data_v1.encode_value(None) is None
    # JSON has no NaN or infinity.
    assert data_v1.encode_value(TagValue.numeric(float("nan"))) is None
    assert data_v1.encode_value(TagValue.numeric(float("inf"))) is None


# ReadingQueryRepository


def _seed_readings(engine: Engine, stores: Stores, count: int) -> None:
    _seed_config(stores)
    base = datetime(2026, 11, 1, tzinfo=UTC)
    _save(
        engine,
        [
            _good("count", TagValue.integer(i), base + timedelta(hours=i))
            for i in range(count)
        ],
    )


def test_count_and_delete_older_than(fake_host: FakeHost) -> None:
    stores = _stores(fake_host)
    _seed_readings(fake_host.engine, stores, 10)
    readings = ReadingQueryRepository(fake_host.engine)
    cutoff = datetime(2026, 11, 1, 7, tzinfo=UTC)  # hours 0..6 are older

    assert readings.count_older_than(cutoff) == 7
    assert readings.delete_older_than(cutoff, batch_size=3) == 7
    assert readings.count_older_than(cutoff) == 0
    assert readings.delete_older_than(cutoff, batch_size=3) == 0
    remaining, truncated = readings.history(
        "count",
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 12, 1, tzinfo=UTC),
        100,
    )
    assert [row.value for row in remaining] == [
        TagValue.integer(7),
        TagValue.integer(8),
        TagValue.integer(9),
    ]
    assert truncated is False


def test_delete_older_than_batches_one_transaction_each(
    fake_host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = _stores(fake_host)
    _seed_readings(fake_host.engine, stores, 10)
    readings = ReadingQueryRepository(fake_host.engine)
    begins = 0
    original_begin = fake_host.engine.begin

    def counting_begin() -> object:
        nonlocal begins
        begins += 1
        return original_begin()

    monkeypatch.setattr(fake_host.engine, "begin", counting_begin)
    deleted = readings.delete_older_than(datetime(2026, 12, 1, tzinfo=UTC), 4)

    assert deleted == 10
    assert begins == 3  # 4 + 4 + 2


def test_reading_queries_reject_naive_and_bad_arguments(fake_host: FakeHost) -> None:
    readings = ReadingQueryRepository(fake_host.engine)
    naive = datetime(2026, 11, 1)
    aware = datetime(2026, 11, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        readings.count_older_than(naive)
    with pytest.raises(ValueError):
        readings.delete_older_than(naive)
    with pytest.raises(ValueError):
        readings.delete_older_than(aware, batch_size=0)
    with pytest.raises(ValueError):
        readings.history("count", naive, aware, 10)
    with pytest.raises(ValueError):
        readings.history("count", aware - timedelta(hours=1), aware, 0)


@pytest.fixture
def postgres_engine(postgres_url: str) -> Iterator[Engine]:
    """A migrated scratch database per test, so state never leaks between tests."""
    admin_engine = create_engine(
        postgres_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    name = f"pdc_api_v1_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        url = make_url(postgres_url).set(database=name)
        rendered = url.render_as_string(hide_password=False)
        run_migrations(rendered)
        engine = create_database_engine(rendered)
        try:
            yield engine
        finally:
            engine.dispose()
    finally:
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.mark.postgres
def test_history_postgres(postgres_engine: Engine) -> None:
    host = FakeHost(postgres_engine)
    stores = _stores(host)
    _seed_config(stores)
    base = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)
    payload = b"\x00\x01"
    _save(
        postgres_engine,
        [
            _good("count", TagValue.integer(2**40 + i), base + timedelta(minutes=i))
            for i in (3, 0, 2, 1)
        ]
        + [_good("blob", TagValue.binary(payload), base)],
    )
    _, key = stores.api_keys.create("Historian")
    app = create_api_app(
        host.api_state,
        web=WebOptions(
            host=host, admin_password="pw", session_secret=PANEL_SESSION_SECRET
        ),
    )
    params = {"from": "2026-11-12T10:00:00Z", "to": "2026-11-12T10:03:00Z", "limit": 2}
    with TestClient(app) as client:
        body = client.get(
            "/api/v1/tags/count/history", params=params, headers={"X-API-Key": key}
        ).json()
        blob = client.get(
            "/api/v1/tags/blob/history", params=params, headers={"X-API-Key": key}
        ).json()

    assert [r["value"] for r in body["readings"]] == [2**40, 2**40 + 1]
    assert body["truncated"] is True
    assert blob["readings"][0]["value"] == base64.b64encode(payload).decode()

    readings = stores.readings
    cutoff = base + timedelta(minutes=2)
    assert readings.count_older_than(cutoff) == 3  # count 0, 1 and the blob
    assert readings.delete_older_than(cutoff, batch_size=2) == 3
    assert readings.count_older_than(cutoff) == 0
