from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import (
    BigInteger,
    Engine,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from plc_data_collector.app.settings import RuntimeSettings
from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    PollExecution,
    PollStatus,
    StorageError,
    TagConfig,
    TagGroupConfig,
    TagResult,
    TagValue,
    ValueType,
)
from plc_data_collector.persistence import (
    ConfigurationRepository,
    ReadingRepository,
    create_database_engine,
    run_migrations,
    wait_for_database,
)
from plc_data_collector.persistence.database import PG_SOCKET_TIMEOUT_S
from plc_data_collector.persistence.schema import (
    metadata,
    poll_executions,
    tag_readings,
)
from plc_data_collector.runtime import WorkerPollResult

EXPECTED_TABLES = {
    "connections",
    "tag_groups",
    "tags",
    "poll_executions",
    "tag_readings",
}


def test_run_migrations_creates_schema_on_sqlite(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/t.db"

    run_migrations(url)

    engine = create_database_engine(url)
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES


def test_migrations_match_metadata(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/t.db"
    run_migrations(url)

    engine = create_database_engine(url)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), metadata)

    assert diff == []


def test_run_migrations_works_from_other_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{tmp_path}/t.db"

    run_migrations(url)

    engine = create_database_engine(url)
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES


def test_create_database_engine_rejects_unknown_scheme() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        create_database_engine("mysql://u:secret@h/db")

    assert exc_info.value.code == "unsupported_database_url"
    assert "secret" not in str(exc_info.value)


def test_postgres_engine_sets_socket_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_database_engine("postgresql+pg8000://u:p@127.0.0.1:1/x")
    captured: dict[str, object] = {}

    def fake_connect(*args: object, **kwargs: object) -> object:
        del args
        captured.update(kwargs)
        raise OSError("not connecting in a unit test")

    monkeypatch.setattr(engine.dialect, "connect", fake_connect)

    with pytest.raises(OSError, match="not connecting"):
        engine.connect()

    assert captured["timeout"] == PG_SOCKET_TIMEOUT_S
    storage_timeout = next(
        field.default
        for field in fields(RuntimeSettings)
        if field.name == "storage_timeout_s"
    )
    assert isinstance(storage_timeout, float)
    assert storage_timeout < PG_SOCKET_TIMEOUT_S


def test_sqlite_engine_has_no_socket_timeout(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path}/t.db")

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT 1")) == 1


def test_wait_for_database_times_out() -> None:
    engine = create_database_engine("postgresql+pg8000://u:p@127.0.0.1:1/x")
    started = time.monotonic()

    with pytest.raises(StorageError) as exc_info:
        wait_for_database(engine, timeout_s=0.5, interval_s=0.1)

    assert exc_info.value.code == "database_unavailable"
    assert time.monotonic() - started < 3


@pytest.mark.postgres
def test_writer_round_trip_on_postgres(postgres_url: str) -> None:
    run_migrations(postgres_url)
    engine = create_database_engine(postgres_url)
    wait_for_database(engine, timeout_s=10)
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    config_repository = ConfigurationRepository(engine)
    config_repository.upsert_connections(
        [
            ConnectionConfig(
                id="mock_connection",
                protocol="mock",
                endpoint="mock://local",
                protocol_options={"driver": "mock"},
            )
        ]
    )
    config_repository.upsert_tag_groups(
        [TagGroupConfig(id="fast", connection_id="mock_connection", interval_ms=1000)]
    )
    config_repository.upsert_tags(
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
    run_id = uuid.uuid4().hex
    values = [
        TagValue.numeric(20.5),
        TagValue.integer(7),
        TagValue.boolean(True),
        TagValue.text("ok"),
        TagValue.binary(b"\x00\x01"),
    ]
    poll_results = [
        WorkerPollResult(
            execution=PollExecution(
                execution_id=f"pg-poll-{run_id}-{index}",
                connection_id="mock_connection",
                tag_group_id="fast",
                status=PollStatus.SUCCESS,
                started_at=now,
                finished_at=now,
                requested_tags=1,
                successful_tags=1,
            ),
            results=(
                TagResult.success(
                    tag_id="temperature",
                    value=value,
                    source_timestamp=now,
                    received_at=now,
                ),
            ),
        )
        for index, value in enumerate(values)
    ]
    reading_repository = ReadingRepository(engine)
    before = reading_repository.count_tag_readings()

    reading_repository.save_poll_results(poll_results)

    assert reading_repository.count_tag_readings() - before == 5


@pytest.mark.postgres
def test_large_values_persist_on_postgres(postgres_url: str) -> None:
    run_migrations(postgres_url)
    engine = create_database_engine(postgres_url)
    wait_for_database(engine, timeout_s=10)
    _seed_mock_configuration(engine)
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    run_id = uuid.uuid4().hex
    big_integer = 2**40
    long_text = "x" * 5000
    long_error = "e" * 3000
    poll_results = [
        _single_result_poll(
            f"pg-large-{run_id}-int",
            TagResult.success(
                tag_id="temperature",
                value=TagValue.integer(big_integer),
                source_timestamp=now,
                received_at=now,
            ),
            now,
        ),
        _single_result_poll(
            f"pg-large-{run_id}-text",
            TagResult.success(
                tag_id="temperature",
                value=TagValue.text(long_text),
                source_timestamp=now,
                received_at=now,
            ),
            now,
        ),
        _single_result_poll(
            f"pg-large-{run_id}-error",
            TagResult.failure(
                tag_id="temperature",
                error_code="opcua_read_timeout",
                error_message=long_error,
                received_at=now,
            ),
            now,
            error_message=long_error,
        ),
    ]

    inserted = ReadingRepository(engine).save_poll_results(poll_results)

    assert inserted == 3
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                tag_readings.c.integer_value,
                tag_readings.c.text_value,
                tag_readings.c.error_message,
            ).where(tag_readings.c.event_id.like(f"pg-large-{run_id}-%"))
        ).all()
        stored_poll_error = connection.scalar(
            select(poll_executions.c.error_message).where(
                poll_executions.c.execution_id == f"pg-large-{run_id}-error"
            )
        )
    assert {row.integer_value for row in rows} >= {big_integer}
    assert {row.text_value for row in rows} >= {long_text}
    assert {row.error_message for row in rows} >= {long_error}
    assert stored_poll_error == long_error


@pytest.mark.postgres
def test_migration_0002_widens_columns_on_postgres(postgres_url: str) -> None:
    admin_engine = create_engine(
        postgres_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    database_name = f"pdc_migration_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    url = make_url(postgres_url).set(database=database_name)
    url_string = url.render_as_string(hide_password=False)
    try:
        _assert_migration_0002_widens_columns(url_string)
    finally:
        with admin_engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )


def test_migration_0002_widens_columns_on_sqlite(tmp_path: Path) -> None:
    _assert_migration_0002_widens_columns(f"sqlite:///{tmp_path}/t.db")


def _assert_migration_0002_widens_columns(url: str) -> None:
    config = _alembic_config(url)
    command.upgrade(config, "head")
    # Downgrading to 0001 rebuilds the 0.1.0 column types.
    command.downgrade(config, "0001")
    engine = create_database_engine(url)
    try:
        old_text_type = _column_type(engine, "tag_readings", "text_value")
        assert getattr(old_text_type, "length", None) == 4096
        assert not isinstance(
            _column_type(engine, "tag_readings", "integer_value"), BigInteger
        )

        command.upgrade(config, "head")

        assert isinstance(
            _column_type(engine, "tag_readings", "integer_value"), BigInteger
        )
        for table, column in (
            ("tag_readings", "text_value"),
            ("tag_readings", "error_message"),
            ("poll_executions", "error_message"),
            ("runtime_components", "error_message"),
        ):
            column_type = _column_type(engine, table, column)
            assert isinstance(column_type, Text), (table, column, column_type)
        with engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), metadata)
        type_changes = [
            change
            for change in diff
            if isinstance(change, list)
            and any(item[0] == "modify_type" for item in change)
        ]
        assert type_changes == []
        check_names = {
            constraint["name"]
            for constraint in inspect(engine).get_check_constraints("poll_executions")
        }
        assert "ck_poll_requested_non_negative" in check_names
    finally:
        engine.dispose()


def test_0003_upgrade_adds_configuration_columns_and_tables(tmp_path: Path) -> None:
    _assert_0003_upgrade(f"sqlite:///{tmp_path}/t.db")


def test_0003_downgrade_round_trip(tmp_path: Path) -> None:
    _assert_0003_round_trip(f"sqlite:///{tmp_path}/t.db")


@pytest.mark.postgres
def test_0003_upgrade_adds_configuration_columns_and_tables_on_postgres(
    postgres_url: str,
) -> None:
    with _scratch_postgres_database(postgres_url) as url:
        _assert_0003_upgrade(url)


@pytest.mark.postgres
def test_0003_downgrade_round_trip_on_postgres(postgres_url: str) -> None:
    with _scratch_postgres_database(postgres_url) as url:
        _assert_0003_round_trip(url)


@contextmanager
def _scratch_postgres_database(postgres_url: str) -> Iterator[str]:
    admin_engine = create_engine(
        postgres_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    database_name = f"pdc_migration_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    url = make_url(postgres_url).set(database=database_name)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with admin_engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        admin_engine.dispose()


def _assert_0003_upgrade(url: str) -> None:
    config = _alembic_config(url)
    command.upgrade(config, "0002")
    engine = create_database_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connections (id, protocol, endpoint, enabled, "
                    "timeout_ms, protocol_options) VALUES "
                    "('c1', 'mock', 'mock://local', true, 1000, '{}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO tag_groups (id, connection_id, interval_ms, "
                    "timeout_ms, overlap_policy, enabled) VALUES "
                    "('g1', 'c1', 1000, 1000, 'skip', true)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO tags (id, tag_group_id, name, address, enabled, "
                    "metadata_json) VALUES ('t1', 'g1', 'T', 'a', true, '{}')"
                )
            )

        command.upgrade(config, "head")

        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT name, secret_options, archived_at FROM connections")
            ).one()
            assert row.name == "c1"
            assert row.secret_options is None
            assert row.archived_at is None
            for table in ("tag_groups", "tags"):
                assert (
                    connection.scalar(text(f"SELECT archived_at FROM {table}")) is None
                )
        inspector = inspect(engine)
        assert {"app_settings", "api_keys"} <= set(inspector.get_table_names())
        assert {column["name"] for column in inspector.get_columns("api_keys")} == {
            "id",
            "name",
            "prefix",
            "key_hash",
            "created_at",
            "last_used_at",
            "revoked_at",
        }
        assert {column["name"] for column in inspector.get_columns("app_settings")} == {
            "key",
            "value",
        }
        name_column = next(
            column
            for column in inspector.get_columns("connections")
            if column["name"] == "name"
        )
        assert name_column["nullable"] is False
        indexes = {
            table: {index["name"] for index in inspector.get_indexes(table)}
            for table in ("tag_readings", "poll_executions")
        }
        assert {
            "ix_tag_readings_received_at",
            "ix_tag_readings_tag_id_received_at",
        } <= indexes["tag_readings"]
        assert "ix_poll_executions_started_at" in indexes["poll_executions"]
        with engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), metadata)
        assert diff == []
    finally:
        engine.dispose()


def _assert_0003_round_trip(url: str) -> None:
    config = _alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "0002")
    engine = create_database_engine(url)
    try:
        inspector = inspect(engine)
        assert "api_keys" not in inspector.get_table_names()
        assert "name" not in {c["name"] for c in inspector.get_columns("connections")}
    finally:
        engine.dispose()
    command.upgrade(config, "head")


def _alembic_config(url: str) -> Config:
    script_location = resources.files("plc_data_collector.persistence").joinpath(
        "migrations"
    )
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _column_type(engine: Engine, table: str, column: str) -> object:
    for reflected in inspect(engine).get_columns(table):
        if reflected["name"] == column:
            return reflected["type"]
    raise AssertionError(f"{table}.{column} not found")


def _seed_mock_configuration(engine: Engine) -> None:
    config_repository = ConfigurationRepository(engine)
    config_repository.upsert_connections(
        [
            ConnectionConfig(
                id="mock_connection",
                protocol="mock",
                endpoint="mock://local",
                protocol_options={"driver": "mock"},
            )
        ]
    )
    config_repository.upsert_tag_groups(
        [TagGroupConfig(id="fast", connection_id="mock_connection", interval_ms=1000)]
    )
    config_repository.upsert_tags(
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


def _single_result_poll(
    execution_id: str,
    result: TagResult,
    now: datetime,
    *,
    error_message: str | None = None,
) -> WorkerPollResult:
    return WorkerPollResult(
        execution=PollExecution(
            execution_id=execution_id,
            connection_id="mock_connection",
            tag_group_id="fast",
            status=PollStatus.SUCCESS if result.is_success else PollStatus.FAILED,
            started_at=now,
            finished_at=now,
            requested_tags=1,
            successful_tags=1 if result.is_success else 0,
            failed_tags=0 if result.is_success else 1,
            error_message=error_message,
        ),
        results=(result,),
    )
