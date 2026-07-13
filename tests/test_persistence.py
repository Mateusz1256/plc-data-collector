from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, select

from plc_gateway.domain import (
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
from plc_gateway.persistence import (
    ConfigurationRepository,
    ReadingRepository,
    RuntimeStatusRepository,
    TagReadingRecord,
    create_sqlite_engine,
    initialize_schema,
)
from plc_gateway.persistence.schema import (
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
    repository = ConfigurationRepository(engine)
    repository.upsert_connections(
        [
            ConnectionConfig(
                id="mock_connection",
                protocol="mock",
                endpoint="mock://local",
                protocol_options={"driver": "mock"},
            )
        ]
    )
    repository.upsert_tag_groups(
        [
            TagGroupConfig(
                id="fast",
                connection_id="mock_connection",
                interval_ms=1000,
            )
        ]
    )
    repository.upsert_tags(
        [
            TagConfig(
                id="temperature",
                tag_group_id="fast",
                name="Temperature",
                address="mock://temperature",
                value_type=ValueType.NUMERIC,
                metadata={"unit": "C"},
            )
        ]
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


def test_alembic_migration_creates_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
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
