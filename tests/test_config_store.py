"""Tests for the database-backed configuration store."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine, create_engine, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from plc_data_collector.app.config import CollectorConfig, parse_collector_config
from plc_data_collector.app.secrets import SecretBox
from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.persistence import (
    AppSettingsStore,
    ConfigStore,
    RetentionSettings,
    create_database_engine,
    run_migrations,
)
from plc_data_collector.persistence.schema import (
    connections,
    tag_groups,
    tag_readings,
    tags,
)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'config.db'}"
        return
    # PostgreSQL: a scratch database per test, so state never leaks between tests.
    base_url: str = request.getfixturevalue("postgres_url")
    admin_engine = create_engine(
        base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    name = f"pdc_config_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield (
            make_url(base_url).set(database=name).render_as_string(hide_password=False)
        )
    finally:
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    run_migrations(database_url)
    created = create_database_engine(database_url)
    yield created
    created.dispose()


@pytest.fixture
def store(engine: Engine) -> ConfigStore:
    return ConfigStore(engine, SecretBox(Fernet.generate_key()))


def _connection(connection_id: str = "c1", **options: object) -> ConnectionConfig:
    return ConnectionConfig(
        id=connection_id,
        protocol="opcua",
        endpoint="opc.tcp://host:4840",
        protocol_options=options,
        name="Line 1",
    )


def _group(group_id: str = "g1", connection_id: str = "c1") -> TagGroupConfig:
    return TagGroupConfig(id=group_id, connection_id=connection_id, interval_ms=1000)


def _tag(tag_id: str = "t1", group_id: str = "g1") -> TagConfig:
    return TagConfig(
        id=tag_id,
        tag_group_id=group_id,
        name=tag_id,
        address="ns=2;s=x",
        value_type=ValueType.NUMERIC,
    )


def _seed(store: ConfigStore) -> None:
    store.upsert_connection(_connection(username="u", password="p"))
    store.upsert_tag_group(_group())
    store.upsert_tag(_tag())


def test_load_active_round_trip_with_secrets(
    store: ConfigStore, engine: Engine
) -> None:
    _seed(store)

    config, revision = store.load_active()

    assert revision == 3
    assert config.connections[0].protocol_options == {
        "username": "u",
        "password": "p",
    }
    assert config.connections[0].name == "Line 1"
    assert [g.id for g in config.tag_groups] == ["g1"]
    assert [t.id for t in config.tags] == ["t1"]
    with engine.connect() as conn:
        row = conn.execute(
            select(connections.c.protocol_options, connections.c.secret_options)
        ).one()
    assert "password" not in row.protocol_options
    assert row.protocol_options == {"username": "u"}
    assert row.secret_options is not None
    assert '"password"' not in row.secret_options
    assert row.secret_options != "p"


def test_nested_secret_is_encrypted_not_in_protocol_options(
    store: ConfigStore, engine: Engine
) -> None:
    store.upsert_connection(
        _connection(auth={"user": "u", "password": "nested-pw"}, username="u")
    )

    with engine.connect() as conn:
        row = conn.execute(
            select(connections.c.protocol_options, connections.c.secret_options)
        ).one()
    assert row.protocol_options == {"auth": {"user": "u"}, "username": "u"}
    assert "nested-pw" not in str(row.protocol_options)
    assert row.secret_options is not None
    assert "nested-pw" not in row.secret_options
    loaded = store.get_connection("c1")
    assert loaded is not None
    assert loaded.protocol_options["auth"] == {"user": "u", "password": "nested-pw"}


def test_every_write_bumps_revision(store: ConfigStore) -> None:
    assert store.revision() == 0
    store.upsert_connection(_connection())
    assert store.revision() == 1
    store.upsert_tag_group(_group())
    assert store.revision() == 2
    store.upsert_tag(_tag())
    assert store.revision() == 3
    store.archive_tag("t1")
    assert store.revision() == 4
    store.replace_all(CollectorConfig((_connection(),), (_group(),), (_tag(),)))
    assert store.revision() == 5


def test_archive_connection_cascades(store: ConfigStore, engine: Engine) -> None:
    _seed(store)

    store.archive_connection("c1")

    config, _ = store.load_active()
    assert config.connections == ()
    assert config.tag_groups == ()
    assert config.tags == ()
    assert store.get_connection("c1") is None
    assert store.get_tag_group("g1") is None
    assert store.get_tag("t1") is None
    with engine.connect() as conn:
        for table in (connections, tag_groups, tags):
            assert conn.execute(select(table.c.archived_at)).scalar_one() is not None


def test_upsert_restores_archived_id(store: ConfigStore) -> None:
    _seed(store)
    store.archive_connection("c1")

    store.upsert_connection(_connection())
    store.upsert_tag_group(_group())
    store.upsert_tag(_tag())

    config, _ = store.load_active()
    assert [c.id for c in config.connections] == ["c1"]
    assert [t.id for t in config.tags] == ["t1"]
    assert store.is_archived_tag("t1") is False


def test_all_tag_ids_includes_archived(store: ConfigStore) -> None:
    _seed(store)
    store.upsert_tag(_tag("t2"))
    store.archive_tag("t2")

    assert store.all_tag_ids() == {"t1", "t2"}


def test_upsert_tag_rejects_archived_group(store: ConfigStore) -> None:
    _seed(store)
    store.archive_tag_group("g1")

    with pytest.raises(ConfigurationError) as caught:
        store.upsert_tag(_tag("t2"))

    assert caught.value.code == "unknown_reference"
    assert store.is_archived_tag("t2") is None


def test_upsert_group_rejects_missing_connection(store: ConfigStore) -> None:
    with pytest.raises(ConfigurationError) as caught:
        store.upsert_tag_group(_group(connection_id="nope"))

    assert caught.value.code == "unknown_reference"
    assert store.revision() == 0


def test_replace_all_archives_missing_and_keeps_history(
    store: ConfigStore, engine: Engine
) -> None:
    _seed(store)
    store.upsert_tag(_tag("t2"))
    with engine.begin() as conn:
        conn.execute(
            insert(tag_readings).values(
                event_id="e1",
                connection_id="c1",
                tag_group_id="g1",
                tag_id="t1",
                received_at=datetime.now(UTC),
                quality="good",
                value_type="numeric",
                numeric_value=1.0,
            )
        )

    store.replace_all(CollectorConfig((_connection(),), (_group(),), (_tag("t2"),)))

    with engine.connect() as conn:
        assert conn.execute(select(tag_readings.c.event_id)).scalars().all() == ["e1"]
    assert store.is_archived_tag("t1") is True
    assert store.is_archived_tag("t2") is False
    assert [t.id for t in store.load_active()[0].tags] == ["t2"]


def test_replace_all_rolls_back_on_invalid_references(store: ConfigStore) -> None:
    _seed(store)
    before = store.revision()
    broken = CollectorConfig((_connection(),), (_group(),), (_tag(group_id="x"),))

    with pytest.raises(ConfigurationError) as caught:
        store.replace_all(broken)

    assert caught.value.code == "unknown_reference"
    assert store.revision() == before
    assert store.get_tag("t1") is not None


def test_encrypt_plaintext_secrets_moves_legacy_password(
    store: ConfigStore, engine: Engine
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(connections).values(
                id="legacy",
                name="legacy",
                protocol="opcua",
                endpoint="opc.tcp://h:1",
                enabled=True,
                timeout_ms=3000,
                protocol_options={"username": "u", "password": "p"},
            )
        )

    assert store.encrypt_plaintext_secrets() == 1
    assert store.encrypt_plaintext_secrets() == 0
    assert store.revision() == 0

    with engine.connect() as conn:
        row = conn.execute(
            select(connections.c.protocol_options, connections.c.secret_options)
        ).one()
    assert row.protocol_options == {"username": "u"}
    assert row.secret_options is not None
    assert row.secret_options != "p"
    loaded = store.get_connection("legacy")
    assert loaded is not None
    assert loaded.protocol_options == {"username": "u", "password": "p"}


def test_connection_name_defaults_to_id() -> None:
    assert ConnectionConfig(id="c1", protocol="p", endpoint="e").name == "c1"
    assert ConnectionConfig(id="c1", protocol="p", endpoint="e", name=" ").name == "c1"


def test_parse_collector_config_keeps_name() -> None:
    raw: dict[str, object] = {
        "format_version": 1,
        "connections": [
            {"id": "c1", "name": "Line 1", "protocol": "mock", "endpoint": "mock://x"}
        ],
        "tag_groups": [{"id": "g1", "connection_id": "c1", "interval_ms": 1000}],
        "tags": [
            {
                "id": "t1",
                "tag_group_id": "g1",
                "name": "t",
                "address": "a",
                "value_type": "numeric",
            }
        ],
    }
    assert parse_collector_config(raw).connections[0].name == "Line 1"
    del raw["format_version"]
    assert parse_collector_config(raw).connections[0].name == "Line 1"


def test_format_version_other_than_1_is_rejected() -> None:
    raw: dict[str, object] = {
        "format_version": 2,
        "connections": [],
        "tag_groups": [],
        "tags": [],
    }
    with pytest.raises(ConfigurationError) as caught:
        parse_collector_config(raw)
    assert caught.value.code == "invalid_configuration"


def test_app_settings_and_retention(engine: Engine) -> None:
    settings = AppSettingsStore(engine)
    assert settings.get("missing") is None
    assert settings.retention() == RetentionSettings(enabled=False, days=30)

    settings.set_retention(RetentionSettings(enabled=True, days=7))

    assert settings.retention() == RetentionSettings(enabled=True, days=7)
    assert settings.get("retention") == {"enabled": True, "days": 7}
    settings.set("x", 1)
    settings.set("x", 2)
    assert settings.get("x") == 2


@pytest.mark.parametrize("days", [0, 3651])
def test_retention_days_are_validated(days: int) -> None:
    with pytest.raises(ConfigurationError) as caught:
        RetentionSettings(enabled=True, days=days)
    assert caught.value.code == "invalid_retention_days"


def test_revision_strictly_increases_and_failed_write_leaves_it(
    store: ConfigStore,
) -> None:
    seen = [store.revision()]
    store.upsert_connection(_connection())
    seen.append(store.revision())
    store.upsert_tag_group(_group())
    seen.append(store.revision())
    store.upsert_tag_group(_group())
    seen.append(store.revision())
    store.archive_tag_group("g1")
    seen.append(store.revision())
    assert seen == sorted(set(seen))

    with pytest.raises(ConfigurationError):
        store.upsert_tag(_tag())

    assert store.revision() == seen[-1]


def test_create_inserts_new_items_and_bumps_revision(store: ConfigStore) -> None:
    store.create_connection(_connection(password="p"))
    store.create_tag_group(_group())
    store.create_tag(_tag())

    config, revision = store.load_active()
    assert [c.id for c in config.connections] == ["c1"]
    assert config.connections[0].protocol_options["password"] == "p"
    assert [g.id for g in config.tag_groups] == ["g1"]
    assert [t.id for t in config.tags] == ["t1"]
    assert revision == 3


def test_create_refuses_active_ids(store: ConfigStore) -> None:
    _seed(store)
    before = store.revision()

    attempts = (
        partial(store.create_connection, _connection(username="other")),
        partial(store.create_tag_group, _group()),
        partial(store.create_tag, _tag()),
    )
    for attempt in attempts:
        with pytest.raises(ConfigurationError) as caught:
            attempt()
        assert caught.value.code == "duplicate_id"

    assert store.revision() == before
    existing = store.get_connection("c1")
    assert existing is not None
    assert existing.protocol_options["username"] == "u"


def test_create_restores_archived_ids_with_new_values(store: ConfigStore) -> None:
    _seed(store)
    store.archive_connection("c1")
    before = store.revision()

    store.create_connection(_connection(username="new-user"))

    restored = store.get_connection("c1")
    assert restored is not None
    assert restored.protocol_options == {"username": "new-user"}
    # Restoring a Connection does not restore its archived children.
    assert store.get_tag_group("g1") is None
    assert store.is_archived_tag("t1") is True
    assert store.revision() == before + 1

    store.create_tag_group(_group())
    store.create_tag(_tag())
    config, _ = store.load_active()
    assert [g.id for g in config.tag_groups] == ["g1"]
    assert [t.id for t in config.tags] == ["t1"]


def test_create_tag_group_needs_active_connection(store: ConfigStore) -> None:
    with pytest.raises(ConfigurationError) as caught:
        store.create_tag_group(_group())

    assert caught.value.code == "unknown_reference"


def test_replace_all_refuses_a_stale_expected_revision(store: ConfigStore) -> None:
    store.upsert_connection(_connection("rev_conn"))
    revision = store.revision()

    with pytest.raises(ConfigurationError) as error:
        store.replace_all(
            CollectorConfig(connections=(), tag_groups=(), tags=()),
            expected_revision=revision - 1,
        )

    assert error.value.code == "revision_changed"
    assert store.revision() == revision
    assert store.get_connection("rev_conn") is not None
    store.replace_all(
        CollectorConfig(connections=(), tag_groups=(), tags=()),
        expected_revision=revision,
    )
    assert store.get_connection("rev_conn") is None
