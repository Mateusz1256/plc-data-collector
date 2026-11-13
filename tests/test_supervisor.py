"""Tests for the RuntimeSupervisor: database ownership, seeding and Apply."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import func, select, text

from plc_data_collector.api import create_api_app
from plc_data_collector.app.runtime import CollectorRuntime
from plc_data_collector.app.settings import RuntimeSettings
from plc_data_collector.app.supervisor import RuntimeSupervisor
from plc_data_collector.domain import (
    CollectorError,
    ConfigurationError,
    StorageError,
    TagConfig,
    ValueType,
)
from plc_data_collector.persistence import DurableSqliteSpool, create_database_engine
from plc_data_collector.persistence.schema import connections, tag_readings

SECRET = "s3cr3t"


def mock_config(
    *, tag_ids: tuple[str, ...] = ("line_1_temperature",), password: str | None = None
) -> dict[str, object]:
    options: dict[str, object] = {
        "tags": {"line_1_temperature": {"sequence": [20.0, 21.0, 22.0]}}
    }
    if password is not None:
        options["password"] = password
    return {
        "connections": [
            {
                "id": "line_1",
                "protocol": "mock",
                "endpoint": "mock://line_1",
                "timeout_ms": 1000,
                "protocol_options": options,
            }
        ],
        "tag_groups": [
            {
                "id": "fast",
                "connection_id": "line_1",
                "interval_ms": 50,
                "timeout_ms": 500,
            }
        ],
        "tags": [
            {
                "id": tag_id,
                "tag_group_id": "fast",
                "name": tag_id.replace("_", " ").title(),
                "address": f"mock://{tag_id}",
                "value_type": "numeric",
            }
            for tag_id in tag_ids
        ],
    }


def write_config(tmp_path: Path, config: Mapping[str, object]) -> Path:
    config_file = tmp_path / "collector.config.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")
    return config_file


def make_settings(
    tmp_path: Path,
    config: Mapping[str, object] | None = None,
    **overrides: object,
) -> RuntimeSettings:
    values: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "database_wait_timeout_s": 2.0,
        "writer_batch_size": 2,
        "writer_flush_interval_s": 0.05,
        "storage_timeout_s": 1.0,
    }
    if config is not None:
        values["config_file"] = write_config(tmp_path, config)
    values.update(overrides)
    return RuntimeSettings.from_env({}, **values)


async def wait_until(condition: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    """Poll a predicate until it holds or the timeout expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not reached before timeout")


def captured_logs(caplog: pytest.LogCaptureFixture) -> str:
    """Return log text plus every record attribute, including ``extra`` fields."""
    return caplog.text + "".join(repr(vars(record)) for record in caplog.records)


def other_tasks() -> list[asyncio.Task[object]]:
    current = asyncio.current_task()
    return [task for task in asyncio.all_tasks() if task is not current]


def readings_for(settings: RuntimeSettings, tag_id: str) -> int:
    engine = create_database_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            return int(
                connection.execute(
                    select(func.count())
                    .select_from(tag_readings)
                    .where(tag_readings.c.tag_id == tag_id)
                ).scalar_one()
            )
    finally:
        engine.dispose()


def pending_in_writer(supervisor: RuntimeSupervisor) -> int:
    writer_metrics = supervisor.api_state().writer_metrics
    return 0 if writer_metrics is None else writer_metrics.pending_batch_size


# Moved from tests/test_runtime.py: database wait, migration and stop-during-start.


async def test_runtime_waits_for_database_then_fails_cleanly(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mock_config(),
        database_url="postgresql+pg8000://u:p@127.0.0.1:1/x",
        database_wait_timeout_s=0.5,
    )
    supervisor = RuntimeSupervisor(settings)

    with pytest.raises(StorageError) as error:
        await supervisor.start()

    assert error.value.code == "database_unavailable"
    assert other_tasks() == []
    await supervisor.stop()


async def test_stop_during_database_wait_aborts_start(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mock_config(),
        database_url="postgresql+pg8000://u:p@127.0.0.1:1/x",
        database_wait_timeout_s=10.0,
    )
    supervisor = RuntimeSupervisor(settings)
    start_task = asyncio.create_task(supervisor.start())
    await asyncio.sleep(0.3)
    loop = asyncio.get_running_loop()

    started = loop.time()
    await asyncio.wait_for(supervisor.stop(), timeout=5.0)
    elapsed = loop.time() - started

    with pytest.raises(CollectorError) as error:
        await asyncio.wait_for(start_task, timeout=5.0)
    assert error.value.code == "runtime_start_aborted"
    assert elapsed < 1.0
    assert other_tasks() == []
    await asyncio.wait_for(supervisor.stop(), timeout=0.1)
    assert other_tasks() == []


async def test_database_wait_failure_does_not_leak_database_password(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    settings = make_settings(
        tmp_path,
        mock_config(),
        database_url=f"postgresql+pg8000://u:{SECRET}@127.0.0.1:1/x",
        database_wait_timeout_s=0.2,
    )

    with pytest.raises(StorageError) as error:
        await RuntimeSupervisor(settings).start()

    assert SECRET not in captured_logs(caplog)
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value.details)


async def test_migration_failure_does_not_leak_database_password(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    settings = make_settings(tmp_path, mock_config())

    def failing_migrations(database_url: str) -> None:
        raise RuntimeError(f"could not migrate {database_url} (password {SECRET})")

    monkeypatch.setattr(
        "plc_data_collector.app.supervisor.run_migrations", failing_migrations
    )
    supervisor = RuntimeSupervisor(settings)

    with pytest.raises(StorageError) as error:
        await supervisor.start()

    assert error.value.code == "database_migration_failed"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert SECRET not in captured_logs(caplog)
    assert SECRET not in str(error.value)
    assert other_tasks() == []
    await supervisor.stop()


# Seeding


async def test_first_start_seeds_from_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    settings = make_settings(tmp_path, mock_config())
    supervisor = RuntimeSupervisor(settings)
    assert supervisor.stores is None

    await supervisor.start()
    try:
        stores = supervisor.stores
        assert stores is not None
        config = supervisor.running_config
        assert config is not None
        assert [tag.id for tag in config.tags] == ["line_1_temperature"]
        assert supervisor.running_revision == stores.config.revision()
        assert supervisor.running_revision is not None
        assert supervisor.running_revision >= 1
        assert "configuration_seeded" in caplog.text
        await wait_until(lambda: readings_for(settings, "line_1_temperature") >= 2)
    finally:
        await supervisor.stop()
    assert other_tasks() == []


async def test_second_start_does_not_reseed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    settings = make_settings(tmp_path, mock_config())
    first = RuntimeSupervisor(settings)
    await first.start()
    await first.stop()

    write_config(tmp_path, mock_config(tag_ids=("line_1_pressure",)))
    caplog.clear()
    second = RuntimeSupervisor(settings)
    await second.start()
    try:
        config = second.running_config
        assert config is not None
        assert [tag.id for tag in config.tags] == ["line_1_temperature"]
        assert "configuration_seed_skipped" in caplog.text
        assert "configuration_seeded" not in caplog.text.replace(
            "configuration_seed_skipped", ""
        )
    finally:
        await second.stop()


async def test_start_with_empty_database_and_no_seed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    settings = make_settings(tmp_path)
    assert settings.config_file is None
    supervisor = RuntimeSupervisor(settings)

    await supervisor.start()
    try:
        config = supervisor.running_config
        assert config is not None
        assert config.connections == ()
        assert supervisor.running_revision == 0
        assert any(
            record.getMessage() == "configuration_empty"
            and record.levelno == logging.WARNING
            for record in caplog.records
        )
        # The scheduler idles with no Tag Groups instead of failing or spinning.
        await asyncio.sleep(0.3)
        state = supervisor.api_state()
        assert state.workers == ()
        assert state.configuration_loaded is True
        assert state.critical_errors == ()
    finally:
        await supervisor.stop()
    assert other_tasks() == []


def _alembic_config(url: str) -> Config:
    script_location = resources.files("plc_data_collector.persistence").joinpath(
        "migrations"
    )
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


async def test_supervisor_upgrades_0_2_0_database(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    settings = make_settings(
        tmp_path, mock_config(tag_ids=("line_1_pressure",), password="other")
    )
    settings.data_dir.mkdir(parents=True)
    # A 0.2.0 database: schema at 0002, configuration rows with a plaintext
    # password, as the 0.2.0 runtime upserted them from the file.
    command.upgrade(_alembic_config(settings.database_url), "0002")
    options = mock_config(password=SECRET)["connections"][0]["protocol_options"]  # type: ignore[index]
    engine = create_database_engine(settings.database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connections (id, protocol, endpoint, enabled, "
                    "timeout_ms, protocol_options) VALUES "
                    "('line_1', 'mock', 'mock://line_1', 1, 1000, :options)"
                ),
                {"options": json.dumps(options)},
            )
            connection.execute(
                text(
                    "INSERT INTO tag_groups (id, connection_id, interval_ms, "
                    "timeout_ms, overlap_policy, enabled) VALUES "
                    "('fast', 'line_1', 50, 500, 'skip', 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO tags (id, tag_group_id, name, address, value_type, "
                    "enabled, metadata_json) VALUES ('line_1_temperature', 'fast', "
                    "'T', 'mock://line_1_temperature', 'numeric', 1, '{}')"
                )
            )
    finally:
        engine.dispose()

    supervisor = RuntimeSupervisor(settings)
    await supervisor.start()
    try:
        assert "configuration_seed_skipped" in caplog.text
        config = supervisor.running_config
        assert config is not None
        assert [tag.id for tag in config.tags] == ["line_1_temperature"]
        assert config.connections[0].protocol_options["password"] == SECRET
        await wait_until(lambda: readings_for(settings, "line_1_temperature") >= 2)
    finally:
        await supervisor.stop()

    engine = create_database_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                select(connections.c.protocol_options, connections.c.secret_options)
            ).one()
    finally:
        engine.dispose()
    assert "password" not in row.protocol_options
    assert row.secret_options is not None
    assert SECRET not in row.secret_options
    assert SECRET not in captured_logs(caplog)


# Apply


async def test_runtime_fills_live_values(tmp_path: Path) -> None:
    supervisor = RuntimeSupervisor(make_settings(tmp_path, mock_config()))
    await supervisor.start()
    try:
        cache = supervisor.live_values
        await wait_until(lambda: cache.get("line_1_temperature") is not None)
        assert cache.last_poll("fast") is not None
        live = cache.get("line_1_temperature")
        assert live is not None
        assert live.connection_id == "line_1"
    finally:
        await supervisor.stop()


async def test_apply_picks_up_database_changes(tmp_path: Path) -> None:
    supervisor = RuntimeSupervisor(make_settings(tmp_path, mock_config()))
    await supervisor.start()
    try:
        stores = supervisor.stores
        assert stores is not None
        cache_before = supervisor.live_values
        stores.config.upsert_tag(
            TagConfig(
                id="line_1_pressure",
                tag_group_id="fast",
                name="Line 1 Pressure",
                address="mock://line_1_pressure",
                value_type=ValueType.NUMERIC,
            )
        )
        assert supervisor.running_revision != stores.config.revision()

        await supervisor.apply()

        assert supervisor.live_values is cache_before
        assert supervisor.running_revision == stores.config.revision()
        await wait_until(
            lambda: supervisor.live_values.get("line_1_pressure") is not None
        )
        assert supervisor.applying is False
    finally:
        await supervisor.stop()


async def test_apply_after_archiving_tag_stores_pending_readings(
    tmp_path: Path,
) -> None:
    # A large batch and a long flush interval keep Poll Results in memory, so
    # only the drain during Apply can store them.
    settings = make_settings(
        tmp_path,
        mock_config(),
        writer_batch_size=1000,
        writer_flush_interval_s=60.0,
    )
    supervisor = RuntimeSupervisor(settings)
    await supervisor.start()
    try:
        stores = supervisor.stores
        assert stores is not None
        await wait_until(lambda: pending_in_writer(supervisor) >= 3)
        assert readings_for(settings, "line_1_temperature") == 0
        stores.config.archive_tag("line_1_temperature")

        await supervisor.apply()

        assert readings_for(settings, "line_1_temperature") >= 3
        assert DurableSqliteSpool(settings.spool_path, max_items=10).count() == 0
        assert supervisor.live_values.get("line_1_temperature") is None
        config = supervisor.running_config
        assert config is not None
        assert config.tags == ()
    finally:
        await supervisor.stop()


def _add_pressure_tag(supervisor: RuntimeSupervisor) -> None:
    stores = supervisor.stores
    assert stores is not None
    stores.config.upsert_tag(
        TagConfig(
            id="line_1_pressure",
            tag_group_id="fast",
            name="Line 1 Pressure",
            address="mock://line_1_pressure",
            value_type=ValueType.NUMERIC,
        )
    )


async def _ready_status(client: httpx.AsyncClient) -> int:
    return (await client.get("/health/ready")).status_code


async def _wait_ready(client: httpx.AsyncClient) -> None:
    for _ in range(250):
        if await asyncio.wait_for(_ready_status(client), timeout=10) == 200:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("not ready before timeout")


async def test_apply_failure_keeps_web_up_and_retry_works(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed Apply falls back to the previous configuration."""
    caplog.set_level(logging.INFO)
    calls = 0

    def flaky_factory(*args: Any, **kwargs: Any) -> CollectorRuntime:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StorageError("database went away", code="database_unavailable")
        return CollectorRuntime(*args, **kwargs)

    supervisor = RuntimeSupervisor(
        make_settings(tmp_path, mock_config()), runtime_factory=flaky_factory
    )
    app = create_api_app(supervisor.api_state)
    transport = httpx.ASGITransport(app=app)
    await asyncio.wait_for(supervisor.start(), timeout=10)
    try:
        previous_revision = supervisor.running_revision
        previous_config = supervisor.running_config
        _add_pressure_tag(supervisor)

        with pytest.raises(StorageError):
            await asyncio.wait_for(supervisor.apply(), timeout=10)

        assert calls == 3
        assert supervisor.running_revision == previous_revision
        assert supervisor.running_config == previous_config
        assert supervisor.last_error_code == "database_unavailable"
        assert "runtime_apply_failed" in caplog.text
        assert "runtime_apply_rolled_back" in caplog.text
        await wait_until(
            lambda: supervisor.live_values.get("line_1_temperature") is not None
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            # Collection continues, so readiness stays OK.
            await _wait_ready(client)
            body = (await client.get("/health/ready")).json()
            assert "database_unavailable" not in body["critical_errors"]

            await asyncio.wait_for(supervisor.apply(), timeout=10)

            stores = supervisor.stores
            assert stores is not None
            assert supervisor.running_revision == stores.config.revision()
            assert supervisor.last_error_code is None
            await _wait_ready(client)
    finally:
        await asyncio.wait_for(supervisor.stop(), timeout=10)


async def test_apply_failure_without_fallback_stops_collection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the previous configuration cannot start either, collection stops."""
    caplog.set_level(logging.INFO)
    calls = 0

    def flaky_factory(*args: Any, **kwargs: Any) -> CollectorRuntime:
        nonlocal calls
        calls += 1
        if calls in (2, 3):
            raise StorageError("database went away", code="database_unavailable")
        return CollectorRuntime(*args, **kwargs)

    supervisor = RuntimeSupervisor(
        make_settings(tmp_path, mock_config()), runtime_factory=flaky_factory
    )
    app = create_api_app(supervisor.api_state)
    transport = httpx.ASGITransport(app=app)
    await asyncio.wait_for(supervisor.start(), timeout=10)
    try:
        _add_pressure_tag(supervisor)

        with pytest.raises(StorageError):
            await asyncio.wait_for(supervisor.apply(), timeout=10)

        assert calls == 3
        state = supervisor.api_state()
        assert state.ready is False
        assert "database_unavailable" in state.critical_errors
        assert supervisor.running_revision is None
        assert supervisor.running_config is None
        assert supervisor.last_error_code == "database_unavailable"
        assert "runtime_apply_failed" in caplog.text
        assert "runtime_rollback_failed" in caplog.text
        assert "runtime_apply_rolled_back" not in caplog.text
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            assert await _ready_status(client) == 503

            await asyncio.wait_for(supervisor.apply(), timeout=10)

            stores = supervisor.stores
            assert stores is not None
            assert supervisor.running_revision == stores.config.revision()
            assert supervisor.last_error_code is None
            await _wait_ready(client)
    finally:
        await asyncio.wait_for(supervisor.stop(), timeout=10)


async def test_concurrent_apply_rejected(tmp_path: Path) -> None:
    supervisor = RuntimeSupervisor(make_settings(tmp_path, mock_config()))
    await supervisor.start()
    try:
        first = asyncio.create_task(supervisor.apply())
        await asyncio.sleep(0)
        assert supervisor.applying is True
        state = supervisor.api_state()
        assert state.configuration_loaded is False
        assert state.critical_errors == ("applying_changes",)

        with pytest.raises(CollectorError) as error:
            await supervisor.apply()

        assert error.value.code == "apply_in_progress"
        await first
        assert supervisor.applying is False
    finally:
        await supervisor.stop()


async def test_supervisor_wrong_secret_key_fails_cleanly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    key_a = Fernet.generate_key().decode("ascii")
    key_b = Fernet.generate_key().decode("ascii")
    config = mock_config(password=SECRET)
    first = RuntimeSupervisor(make_settings(tmp_path, config, secret_key=key_a))
    await first.start()
    await first.stop()

    second = RuntimeSupervisor(make_settings(tmp_path, config, secret_key=key_b))
    with pytest.raises(ConfigurationError) as error:
        await second.start()

    assert error.value.code == "secret_decryption_failed"
    assert other_tasks() == []
    logs = captured_logs(caplog)
    for value in (SECRET, key_a, key_b):
        assert value not in logs
        assert value not in str(error.value)
    await second.stop()


async def test_stop_during_apply_does_not_start_a_new_runtime(
    tmp_path: Path,
) -> None:
    calls = 0
    old_runtime_stopping = asyncio.Event()
    release_old_runtime = asyncio.Event()

    def factory(*args: Any, **kwargs: Any) -> CollectorRuntime:
        nonlocal calls
        calls += 1
        runtime = CollectorRuntime(*args, **kwargs)
        if calls == 1:
            original_stop = runtime.stop

            async def slow_stop(timeout_s: float = 10.0) -> None:
                # Holds the old runtime's drain open, so stop() arrives mid-apply.
                old_runtime_stopping.set()
                await release_old_runtime.wait()
                await original_stop(timeout_s=timeout_s)

            runtime.stop = slow_stop  # type: ignore[method-assign]
        return runtime

    supervisor = RuntimeSupervisor(
        make_settings(tmp_path, mock_config()), runtime_factory=factory
    )
    await asyncio.wait_for(supervisor.start(), timeout=10)
    apply_task = asyncio.create_task(supervisor.apply())
    stop_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(old_runtime_stopping.wait(), timeout=10)
        stop_task = asyncio.create_task(supervisor.stop())
        await asyncio.sleep(0)
        release_old_runtime.set()

        await asyncio.wait_for(apply_task, timeout=10)
        await asyncio.wait_for(stop_task, timeout=10)

        assert calls == 1
        assert supervisor.running_revision is None
        assert supervisor.running_config is None
        assert supervisor.applying is False
    finally:
        release_old_runtime.set()
        for task in (apply_task, stop_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.wait_for(supervisor.stop(), timeout=10)
