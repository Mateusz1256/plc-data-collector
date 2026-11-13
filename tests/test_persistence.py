from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from plc_data_collector.app.secrets import SecretBox
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
from plc_data_collector.persistence import (
    ConfigStore,
    ReadingRepository,
    RuntimeStatusRepository,
    TagReadingRecord,
    create_database_engine,
    create_sqlite_engine,
    initialize_schema,
)
from plc_data_collector.persistence.schema import (
    runtime_components,
    tag_readings,
)


def utc_now() -> datetime:
    return datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def make_engine() -> Engine:
    engine = create_sqlite_engine("sqlite:///:memory:")
    initialize_schema(engine)
    return engine


def seed_configuration(engine: Engine) -> None:
    store = ConfigStore(engine, SecretBox(Fernet.generate_key()))
    store.upsert_connection(
        ConnectionConfig(
            id="mock_connection",
            protocol="mock",
            endpoint="mock://local",
            protocol_options={"driver": "mock"},
        )
    )
    store.upsert_tag_group(
        TagGroupConfig(
            id="fast",
            connection_id="mock_connection",
            interval_ms=1000,
        )
    )
    store.upsert_tag(
        TagConfig(
            id="temperature",
            tag_group_id="fast",
            name="Temperature",
            address="mock://temperature",
            value_type=ValueType.NUMERIC,
            metadata={"unit": "C"},
        )
    )


def make_poll_execution() -> PollExecution:
    timestamp = utc_now()
    return PollExecution(
        execution_id="poll-1",
        connection_id="mock_connection",
        tag_group_id="fast",
        status=PollStatus.SUCCESS,
        started_at=timestamp,
        finished_at=timestamp,
        requested_tags=1,
        successful_tags=1,
    )


def make_reading(event_id: str) -> TagReadingRecord:
    timestamp = utc_now()
    return TagReadingRecord(
        event_id=event_id,
        connection_id="mock_connection",
        tag_group_id="fast",
        result=TagResult.success(
            tag_id="temperature",
            value=TagValue.numeric(20.5),
            source_timestamp=timestamp,
            received_at=timestamp,
        ),
    )


def make_failed_reading(event_id: str) -> TagReadingRecord:
    timestamp = utc_now()
    return TagReadingRecord(
        event_id=event_id,
        connection_id="mock_connection",
        tag_group_id="fast",
        result=TagResult.failure(
            tag_id="temperature",
            error_code="mock_timeout",
            error_message="Mock read timed out.",
            received_at=timestamp,
        ),
    )


def test_alembic_migration_creates_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "collector.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_sqlite_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    assert {
        "connections",
        "tag_groups",
        "tags",
        "tag_readings",
        "poll_executions",
        "runtime_components",
    }.issubset(set(inspector.get_table_names()))


def test_repositories_persist_poll_executions_and_readings() -> None:
    engine = make_engine()
    seed_configuration(engine)
    repository = ReadingRepository(engine)

    repository.save_poll_execution(make_poll_execution())
    inserted = repository.save_tag_readings([make_reading("event-1")])

    assert inserted == 1
    assert repository.count_poll_executions() == 1
    assert repository.count_tag_readings() == 1

    with engine.connect() as connection:
        row = connection.execute(select(tag_readings)).mappings().one()
    assert row["event_id"] == "event-1"
    assert row["numeric_value"] == 20.5
    assert row["received_at"] == utc_now()


def test_repositories_persist_failed_readings_with_configured_value_type() -> None:
    engine = make_engine()
    seed_configuration(engine)
    repository = ReadingRepository(engine)

    inserted = repository.save_tag_readings([make_failed_reading("event-1")])

    assert inserted == 1
    with engine.connect() as connection:
        row = connection.execute(select(tag_readings)).mappings().one()
    assert row["value_type"] == "numeric"
    assert row["numeric_value"] is None
    assert row["error_code"] == "mock_timeout"


def test_duplicate_event_id_does_not_create_second_reading() -> None:
    engine = make_engine()
    seed_configuration(engine)
    repository = ReadingRepository(engine)

    first_insert = repository.save_tag_readings([make_reading("event-1")])
    second_insert = repository.save_tag_readings([make_reading("event-1")])

    assert first_insert == 1
    assert second_insert == 0
    assert repository.count_tag_readings() == 1


def test_runtime_status_repository_upserts_component_status() -> None:
    engine = make_engine()
    repository = RuntimeStatusRepository(engine)
    timestamp = utc_now()

    repository.upsert_status(
        RuntimeComponentStatus(
            component_id="worker:mock_connection",
            state=WorkerState.RUNNING,
            updated_at=timestamp,
            message="healthy",
        )
    )
    repository.upsert_status(
        RuntimeComponentStatus(
            component_id="worker:mock_connection",
            state=WorkerState.DEGRADED,
            updated_at=timestamp,
            error_code="timeout",
            error_message="read timed out",
        )
    )

    with engine.connect() as connection:
        row = connection.execute(select(runtime_components)).mappings().one()
    assert row["state"] == "degraded"
    assert row["error_code"] == "timeout"


def test_engines_hide_statement_parameters(tmp_path: Path) -> None:
    sqlite = create_database_engine(f"sqlite:///{tmp_path / 'p.db'}")
    postgres = create_database_engine("postgresql+pg8000://u:p@127.0.0.1:1/x")
    try:
        assert sqlite.hide_parameters is True
        assert postgres.hide_parameters is True
        with sqlite.begin() as connection:
            connection.execute(text("CREATE TABLE t (v TEXT UNIQUE)"))
            connection.execute(text("INSERT INTO t VALUES (:v)"), {"v": "secret-v"})
        with pytest.raises(IntegrityError) as error, sqlite.begin() as connection:
            connection.execute(text("INSERT INTO t VALUES (:v)"), {"v": "secret-v"})
        assert "secret-v" not in str(error.value)
    finally:
        sqlite.dispose()
        postgres.dispose()
