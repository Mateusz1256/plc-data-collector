from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, select

from plc_data_collector.api import RuntimeApiState, create_api_app
from plc_data_collector.app.runtime import CollectorRuntime
from plc_data_collector.app.settings import RuntimeSettings
from plc_data_collector.domain import CollectorError, ConfigurationError, StorageError
from plc_data_collector.persistence import (
    DurableSqliteSpool,
    ReadingRepository,
    create_database_engine,
)
from plc_data_collector.persistence.schema import poll_executions
from plc_data_collector.runtime import WorkerPollResult

SECRET = "s3cr3t"


class AlwaysFailingRepository:
    """Repository double that simulates an unavailable primary database."""

    def save_poll_results(self, poll_results: list[WorkerPollResult]) -> int:
        """Raise a storage error for every attempted batch."""
        raise StorageError(
            "primary database unavailable",
            code="primary_database_unavailable",
            details={"poll_results": len(poll_results)},
        )


class HangingRepository:
    """Repository double whose writes hang until released, like a stalled DB."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def save_poll_results(self, poll_results: list[WorkerPollResult]) -> int:
        """Block until released, then fail."""
        self.release.wait(timeout=10)
        raise StorageError("primary database stalled", code="database_stalled")


def mock_connection(
    connection_id: str = "line_1",
    *,
    enabled: bool = True,
) -> dict[str, object]:
    return {
        "id": connection_id,
        "protocol": "mock",
        "endpoint": f"mock://{connection_id}",
        "enabled": enabled,
        "timeout_ms": 1000,
        "protocol_options": {
            "tags": {f"{connection_id}_temperature": {"sequence": [20.0, 21.0, 22.0]}}
        },
    }


def tag_group(
    group_id: str,
    connection_id: str,
    *,
    enabled: bool = True,
) -> dict[str, object]:
    return {
        "id": group_id,
        "connection_id": connection_id,
        "interval_ms": 50,
        "timeout_ms": 500,
        "enabled": enabled,
    }


def tag(tag_id: str, group_id: str, *, enabled: bool = True) -> dict[str, object]:
    return {
        "id": tag_id,
        "tag_group_id": group_id,
        "name": tag_id.replace("_", " ").title(),
        "address": f"mock://{tag_id}",
        "value_type": "numeric",
        "enabled": enabled,
    }


def single_mock_config() -> dict[str, object]:
    return {
        "connections": [mock_connection()],
        "tag_groups": [tag_group("fast", "line_1")],
        "tags": [tag("line_1_temperature", "fast")],
    }


def make_settings(
    tmp_path: Path,
    config: Mapping[str, object],
    **overrides: object,
) -> RuntimeSettings:
    config_file = tmp_path / "collector.config.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")
    values: dict[str, object] = {
        "config_file": config_file,
        "data_dir": tmp_path / "data",
        "database_wait_timeout_s": 2.0,
        "writer_batch_size": 2,
        "writer_flush_interval_s": 0.05,
        "storage_timeout_s": 1.0,
    }
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


def reading_repository(settings: RuntimeSettings) -> tuple[Engine, ReadingRepository]:
    engine = create_database_engine(settings.database_url)
    return engine, ReadingRepository(engine)


def captured_logs(caplog: pytest.LogCaptureFixture) -> str:
    """Return log text plus every record attribute, including ``extra`` fields."""
    return caplog.text + "".join(repr(vars(record)) for record in caplog.records)


def other_tasks() -> list[asyncio.Task[object]]:
    current = asyncio.current_task()
    return [task for task in asyncio.all_tasks() if task is not current]


async def test_runtime_collects_readings_end_to_end(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, single_mock_config())
    runtime = CollectorRuntime(settings)
    await runtime.start()
    engine, repository = reading_repository(settings)
    try:
        await wait_until(lambda: repository.count_tag_readings() >= 3)
        assert runtime.api_state().workers[0].state.value == "running"
    finally:
        await runtime.stop()
        engine.dispose()


def completed_polls(runtime: CollectorRuntime) -> int:
    """Return Poll Results produced so far; each one is put on the Reading Queue."""
    return sum(
        metrics.poll_successes + metrics.poll_failures
        for metrics in runtime.api_state().worker_metrics
    )


def queued(runtime: CollectorRuntime) -> int:
    queue_metrics = runtime.api_state().queue_metrics
    assert queue_metrics is not None
    return queue_metrics.size


def pending_in_writer(runtime: CollectorRuntime) -> int:
    writer_metrics = runtime.api_state().writer_metrics
    assert writer_metrics is not None
    return writer_metrics.pending_batch_size


async def test_stop_drains_queue_to_database(tmp_path: Path) -> None:
    # A large batch and a long flush interval keep every Poll Result in memory
    # until stop(), so only the drain on stop can persist them.
    settings = make_settings(
        tmp_path,
        single_mock_config(),
        writer_batch_size=1000,
        writer_flush_interval_s=60.0,
    )
    runtime = CollectorRuntime(settings)
    await runtime.start()
    engine, repository = reading_repository(settings)
    try:
        await wait_until(lambda: pending_in_writer(runtime) >= 3)
        assert repository.count_poll_executions() == 0
        await runtime.stop()

        produced = completed_polls(runtime)
        assert produced >= 3
        assert repository.count_poll_executions() == produced
        assert repository.count_tag_readings() == produced
        queue_metrics = runtime.api_state().queue_metrics
        assert queue_metrics is not None
        assert queue_metrics.size == 0
        assert DurableSqliteSpool(settings.spool_path, max_items=10).count() == 0
    finally:
        await runtime.stop()
        engine.dispose()


async def test_stop_spools_queue_when_writer_exceeds_budget(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        single_mock_config(),
        writer_batch_size=2,
        writer_flush_interval_s=0.05,
        storage_timeout_s=0.2,
    )
    hanging = HangingRepository()
    runtime = CollectorRuntime(settings, repository_factory=lambda _engine: hanging)
    await runtime.start()
    engine, repository = reading_repository(settings)
    loop = asyncio.get_running_loop()
    try:
        # The writer is stuck on a stalled batch while the queue keeps filling.
        await wait_until(lambda: queued(runtime) >= 4)
        started = loop.time()
        await runtime.stop(timeout_s=0.3)
        elapsed = loop.time() - started
    finally:
        hanging.release.set()
        await runtime.stop()

    produced = completed_polls(runtime)
    spooled = DurableSqliteSpool(settings.spool_path, max_items=1000).count()
    persisted = repository.count_poll_executions()
    engine.dispose()
    assert produced >= 4
    assert spooled + persisted == produced
    assert elapsed < 0.3 + 1.0


async def test_stop_with_database_down_spools_pending_results(tmp_path: Path) -> None:
    # A long flush interval and a large batch keep Poll Results pending in the
    # Database Writer, so only the shutdown path can move them to the Spool.
    settings = make_settings(
        tmp_path,
        single_mock_config(),
        writer_batch_size=1000,
        writer_flush_interval_s=60.0,
    )
    runtime = CollectorRuntime(
        settings,
        repository_factory=lambda _engine: AlwaysFailingRepository(),
    )
    await runtime.start()

    def pending() -> int:
        writer_metrics = runtime.api_state().writer_metrics
        assert writer_metrics is not None
        return writer_metrics.pending_batch_size

    try:
        await wait_until(lambda: pending() >= 2)
        assert DurableSqliteSpool(settings.spool_path, max_items=10).count() == 0
    finally:
        await runtime.stop()

    spool = DurableSqliteSpool(settings.data_dir / "spool.db", max_items=10)
    assert spool.count() > 0
    assert runtime.api_state().storage_available is False


async def test_runtime_skips_disabled_items(tmp_path: Path) -> None:
    config = {
        "connections": [
            mock_connection("line_1"),
            mock_connection("line_2", enabled=False),
        ],
        "tag_groups": [
            tag_group("fast", "line_1"),
            tag_group("only_disabled_tags", "line_1"),
            tag_group("disabled_group", "line_1", enabled=False),
            tag_group("disabled_connection_group", "line_2"),
        ],
        "tags": [
            tag("line_1_temperature", "fast"),
            tag("disabled_tag", "only_disabled_tags", enabled=False),
            tag("disabled_group_tag", "disabled_group"),
            tag("line_2_temperature", "disabled_connection_group"),
        ],
    }
    settings = make_settings(tmp_path, config)
    runtime = CollectorRuntime(settings)
    await runtime.start()
    engine, repository = reading_repository(settings)
    try:
        await wait_until(lambda: repository.count_tag_readings() >= 3)
        workers = runtime.api_state().workers
        assert [worker.component_id for worker in workers] == [
            "connection-worker:line_1"
        ]
    finally:
        await runtime.stop()

    with engine.connect() as connection:
        polled_groups = set(
            connection.execute(select(poll_executions.c.tag_group_id).distinct())
            .scalars()
            .all()
        )
    engine.dispose()
    assert polled_groups == {"fast"}


async def test_runtime_survives_unreachable_connection(tmp_path: Path) -> None:
    config = {
        "connections": [
            mock_connection("line_1"),
            {
                "id": "unreachable",
                "protocol": "opcua",
                "endpoint": "opc.tcp://127.0.0.1:1",
                "timeout_ms": 500,
            },
        ],
        "tag_groups": [
            tag_group("fast", "line_1"),
            tag_group("remote", "unreachable"),
        ],
        "tags": [
            tag("line_1_temperature", "fast"),
            {
                "id": "remote_temperature",
                "tag_group_id": "remote",
                "name": "Remote Temperature",
                "address": "ns=2;s=Machine.Temperature",
                "value_type": "numeric",
            },
        ],
    }
    settings = make_settings(tmp_path, config)
    runtime = CollectorRuntime(settings)
    await runtime.start()

    def successes(component_id: str) -> int:
        for metrics in runtime.api_state().worker_metrics:
            if metrics.component_id == component_id:
                return metrics.tag_successes
        raise AssertionError(f"no worker {component_id}")

    try:
        await wait_until(lambda: successes("connection-worker:line_1") >= 3)
        states = {
            worker.component_id: worker.state.value
            for worker in runtime.api_state().workers
        }
        assert states["connection-worker:line_1"] == "running"
        assert states["connection-worker:unreachable"] in {"failed", "degraded"}
    finally:
        await runtime.stop()


async def test_runtime_waits_for_database_then_fails_cleanly(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        single_mock_config(),
        database_url="postgresql+pg8000://u:p@127.0.0.1:1/x",
        database_wait_timeout_s=0.5,
    )
    runtime = CollectorRuntime(settings)

    with pytest.raises(StorageError) as error:
        await runtime.start()

    assert error.value.code == "database_unavailable"
    assert other_tasks() == []
    await runtime.stop()


async def test_stop_during_database_wait_aborts_start(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        single_mock_config(),
        database_url="postgresql+pg8000://u:p@127.0.0.1:1/x",
        database_wait_timeout_s=10.0,
    )
    runtime = CollectorRuntime(settings)
    start_task = asyncio.create_task(runtime.start())
    await asyncio.sleep(0.3)
    loop = asyncio.get_running_loop()

    started = loop.time()
    await asyncio.wait_for(runtime.stop(), timeout=5.0)
    elapsed = loop.time() - started

    with pytest.raises(CollectorError) as error:
        await asyncio.wait_for(start_task, timeout=5.0)
    assert error.value.code == "runtime_start_aborted"
    assert elapsed < 1.0
    assert other_tasks() == []
    await asyncio.wait_for(runtime.stop(), timeout=0.1)
    assert other_tasks() == []


async def test_database_wait_failure_does_not_leak_database_password(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    settings = make_settings(
        tmp_path,
        single_mock_config(),
        database_url=f"postgresql+pg8000://u:{SECRET}@127.0.0.1:1/x",
        database_wait_timeout_s=0.2,
    )

    with pytest.raises(StorageError) as error:
        await CollectorRuntime(settings).start()

    assert SECRET not in captured_logs(caplog)
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value.details)


async def test_migration_failure_does_not_leak_database_password(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    settings = make_settings(tmp_path, single_mock_config())

    def failing_migrations(database_url: str) -> None:
        raise RuntimeError(f"could not migrate {database_url} (password {SECRET})")

    monkeypatch.setattr(
        "plc_data_collector.app.runtime.run_migrations", failing_migrations
    )
    runtime = CollectorRuntime(settings)

    with pytest.raises(StorageError) as error:
        await runtime.start()

    assert error.value.code == "database_migration_failed"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert SECRET not in captured_logs(caplog)
    assert SECRET not in str(error.value)
    assert other_tasks() == []
    await runtime.stop()


async def test_runtime_logs_do_not_leak_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    config = {
        "connections": [
            {
                "id": "secured",
                "protocol": "opcua",
                "endpoint": f"opc.tcp://operator:{SECRET}@127.0.0.1:1",
                "timeout_ms": 500,
                "protocol_options": {"username": "operator", "password": SECRET},
            }
        ],
        "tag_groups": [tag_group("remote", "secured")],
        "tags": [
            {
                "id": "remote_temperature",
                "tag_group_id": "remote",
                "name": "Remote Temperature",
                "address": "ns=2;s=Machine.Temperature",
                "value_type": "numeric",
            }
        ],
    }
    settings = make_settings(tmp_path, config)
    runtime = CollectorRuntime(settings)
    app = create_api_app(runtime.api_state)
    await runtime.start()
    try:
        await wait_until(
            lambda: runtime.api_state().worker_metrics[0].poll_attempts >= 2
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            workers = (await client.get("/api/runtime/workers")).json()
            components = (await client.get("/api/runtime/components")).json()
    finally:
        await runtime.stop()

    assert workers["workers"][0]["component_id"] == "connection-worker:secured"
    assert SECRET not in captured_logs(caplog)
    assert SECRET not in json.dumps(workers)
    assert SECRET not in json.dumps(components)


def test_settings_config_file_is_optional() -> None:
    assert RuntimeSettings.from_env({}).config_file is None


async def test_runtime_start_requires_config_file(tmp_path: Path) -> None:
    settings = RuntimeSettings.from_env({}, data_dir=tmp_path / "data")
    runtime = CollectorRuntime(settings)

    with pytest.raises(ConfigurationError) as error:
        await runtime.start()

    assert error.value.code == "config_file_required"
    assert "PLC_DATA_COLLECTOR_CONFIG" in str(error.value)


def test_settings_read_environment_and_overrides(tmp_path: Path) -> None:
    environ = {
        "PLC_DATA_COLLECTOR_CONFIG": str(tmp_path / "collector.config.json"),
        "PLC_DATA_COLLECTOR_DATA_DIR": str(tmp_path / "data"),
        "PLC_DATA_COLLECTOR_API_PORT": "9090",
        "PLC_DATA_COLLECTOR_QUEUE_MAX_SIZE": "50",
        "PLC_DATA_COLLECTOR_WRITER_FLUSH_INTERVAL_S": "0.5",
    }

    settings = RuntimeSettings.from_env(environ, queue_max_size=7)

    assert settings.config_file == tmp_path / "collector.config.json"
    assert settings.data_dir == tmp_path / "data"
    assert settings.database_url == f"sqlite:///{tmp_path / 'data' / 'collector.db'}"
    assert settings.spool_path == tmp_path / "data" / "spool.db"
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 9090
    assert settings.queue_max_size == 7
    assert settings.writer_flush_interval_s == 0.5
    assert settings.writer_batch_size == 100
    assert settings.database_wait_timeout_s == 60.0
    assert settings.storage_timeout_s == 5.0
    assert settings.spool_max_items == 100000


def test_settings_reject_unknown_override(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        RuntimeSettings.from_env(
            {"PLC_DATA_COLLECTOR_CONFIG": str(tmp_path / "c.json")},
            no_such_setting=1,
        )


def test_settings_reject_invalid_environment_value(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as error:
        RuntimeSettings.from_env(
            {
                "PLC_DATA_COLLECTOR_CONFIG": str(tmp_path / "c.json"),
                "PLC_DATA_COLLECTOR_API_PORT": "not-a-port",
            }
        )

    assert error.value.code == "invalid_runtime_setting"


async def test_api_uses_state_provider() -> None:
    app = create_api_app(lambda: RuntimeApiState(storage_available=False))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
