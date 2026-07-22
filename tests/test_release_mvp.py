from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from plc_gateway.domain import (
    ConnectionConfig,
    PollStatus,
    StorageError,
    TagConfig,
    TagGroupConfig,
    ValueType,
    WorkerState,
)
from plc_gateway.persistence import (
    ConfigurationRepository,
    DatabaseWriter,
    DurableSqliteSpool,
    ReadingRepository,
    create_sqlite_engine,
    initialize_schema,
)
from plc_gateway.persistence.schema import poll_executions
from plc_gateway.protocols import MOCK_PROTOCOL, DriverRegistry, create_mock_driver
from plc_gateway.runtime import ConnectionWorker, PollScheduler, ReadingQueue
from plc_gateway.runtime.connection_worker import WorkerPollResult


class AlwaysFailingRepository:
    """Repository test double that simulates an unavailable primary database."""

    def save_poll_results(self, poll_results: list[WorkerPollResult]) -> int:
        """Raise a storage error for every attempted batch."""
        raise StorageError(
            "primary database unavailable",
            code="primary_database_unavailable",
            details={"poll_results": len(poll_results)},
        )


async def no_sleep(_: float) -> None:
    """Deterministic retry sleeper."""


async def start_writer(writer: DatabaseWriter) -> asyncio.Task[None]:
    """Start a writer task and let it enter its run loop."""
    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0)
    return task


async def wait_until(
    condition: object,
    *,
    timeout_s: float = 1.0,
) -> None:
    """Wait until a predicate returns true."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if callable(condition) and condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


@pytest.mark.asyncio
async def test_mvp_runtime_polls_mock_connection_for_multiple_cycles(
    tmp_path: Path,
) -> None:
    engine = create_release_engine(tmp_path)
    connection = make_connection(
        protocol_options={"tags": {"temperature": {"sequence": [20.0, 21.0, 22.0]}}},
    )
    group = make_group(interval_ms=10)
    tag = make_tag()
    seed_configuration(engine, [connection], [group], [tag])
    repository = ReadingRepository(engine)
    queue = ReadingQueue(max_size=20, put_timeout_s=0.1)
    writer = DatabaseWriter(
        queue,
        repository,
        batch_size=2,
        flush_interval_s=0.01,
        storage_timeout_s=1,
    )
    worker = ConnectionWorker(
        connection,
        make_registry(),
        retry_sleep=no_sleep,
        jitter=lambda: 0.5,
    )
    completed_polls = 0

    async def handle_poll(scheduled_group: TagGroupConfig) -> None:
        nonlocal completed_polls
        poll_result = await worker.poll_group(scheduled_group, (tag,))
        await queue.put(poll_result)
        completed_polls += 1

    writer_task = await start_writer(writer)
    await worker.connect()
    scheduler = PollScheduler([group], handle_poll)
    scheduler_task = asyncio.create_task(scheduler.run())

    try:
        await wait_until(lambda: completed_polls >= 6)
        scheduler.stop()
        await asyncio.wait_for(scheduler_task, timeout=1)
        await queue.join()
        shutdown = await writer.shutdown(timeout_s=1)
    finally:
        scheduler.stop()
        if not scheduler_task.done():
            scheduler_task.cancel()
            await asyncio.gather(scheduler_task, return_exceptions=True)
        await worker.disconnect()
        await writer.shutdown(timeout_s=1)
        await writer_task

    assert shutdown.drained is True
    assert repository.count_poll_executions() >= 6
    assert repository.count_tag_readings() >= 6
    assert writer.metrics().successful_batches >= 3
    assert worker.status().state is WorkerState.STOPPED


@pytest.mark.asyncio
async def test_mvp_connection_failure_is_persisted_without_stopping_healthy_worker(
    tmp_path: Path,
) -> None:
    engine = create_release_engine(tmp_path)
    failing_connection = make_connection(
        "failing_connection",
        protocol_options={"timeout_on_read": True},
    )
    healthy_connection = make_connection("healthy_connection")
    failing_group = make_group(
        "failing_group",
        connection_id=failing_connection.id,
    )
    healthy_group = make_group(
        "healthy_group",
        connection_id=healthy_connection.id,
    )
    failing_tag = make_tag("failing_temperature", tag_group_id=failing_group.id)
    healthy_tag = make_tag("healthy_temperature", tag_group_id=healthy_group.id)
    seed_configuration(
        engine,
        [failing_connection, healthy_connection],
        [failing_group, healthy_group],
        [failing_tag, healthy_tag],
    )
    repository = ReadingRepository(engine)
    queue = ReadingQueue(max_size=10, put_timeout_s=0.1)
    writer = DatabaseWriter(
        queue,
        repository,
        batch_size=2,
        flush_interval_s=1,
        storage_timeout_s=1,
    )
    registry = make_registry()
    failing_worker = ConnectionWorker(
        failing_connection,
        registry,
        retry_sleep=no_sleep,
        jitter=lambda: 0.5,
    )
    healthy_worker = ConnectionWorker(
        healthy_connection,
        registry,
        retry_sleep=no_sleep,
        jitter=lambda: 0.5,
    )

    writer_task = await start_writer(writer)
    await failing_worker.connect()
    await healthy_worker.connect()

    try:
        failed_poll = await failing_worker.poll_group(failing_group, (failing_tag,))
        healthy_poll = await healthy_worker.poll_group(healthy_group, (healthy_tag,))
        await queue.put(failed_poll)
        await queue.put(healthy_poll)
        await queue.join()
        shutdown = await writer.shutdown(timeout_s=1)
    finally:
        await failing_worker.disconnect()
        await healthy_worker.disconnect()
        await writer.shutdown(timeout_s=1)
        await writer_task

    assert shutdown.drained is True
    assert failed_poll.execution.status is PollStatus.FAILED
    assert healthy_poll.execution.status is PollStatus.SUCCESS
    assert sorted(poll_status_values(engine)) == ["failed", "success"]
    assert repository.count_tag_readings() == 2
    assert failing_worker.metrics().poll_failures == 1
    assert healthy_worker.metrics().poll_successes == 1


@pytest.mark.asyncio
async def test_mvp_storage_failure_spools_and_replays_after_recovery(
    tmp_path: Path,
) -> None:
    connection = make_connection()
    group = make_group()
    tag = make_tag()
    worker = ConnectionWorker(
        connection,
        make_registry(),
        retry_sleep=no_sleep,
        jitter=lambda: 0.5,
    )
    await worker.connect()
    try:
        poll_result = await worker.poll_group(group, (tag,))
    finally:
        await worker.disconnect()

    spool = DurableSqliteSpool(tmp_path / "spool.db", max_items=10)
    failing_queue = ReadingQueue(max_size=10, put_timeout_s=0.1)
    failing_writer = DatabaseWriter(
        failing_queue,
        AlwaysFailingRepository(),
        batch_size=1,
        flush_interval_s=1,
        storage_timeout_s=1,
        max_retries=0,
        spool=spool,
        spool_timeout_s=1,
    )
    failing_task = await start_writer(failing_writer)

    await failing_queue.put(poll_result)
    await failing_queue.join()
    failing_shutdown = await failing_writer.shutdown(timeout_s=1)
    await failing_task

    engine = create_release_engine(tmp_path)
    seed_configuration(engine, [connection], [group], [tag])
    repository = ReadingRepository(engine)
    replay_writer = DatabaseWriter(
        ReadingQueue(max_size=10, put_timeout_s=0.1),
        repository,
        batch_size=1,
        flush_interval_s=0.01,
        storage_timeout_s=1,
        max_retries=0,
        spool=spool,
        spool_timeout_s=1,
    )
    replay_task = await start_writer(replay_writer)

    await wait_until(lambda: spool.count() == 0)
    replay_shutdown = await replay_writer.shutdown(timeout_s=1)
    await replay_task

    assert failing_shutdown.drained is True
    assert failing_writer.metrics().spooled_poll_results == 1
    assert replay_shutdown.drained is True
    assert repository.count_poll_executions() == 1
    assert repository.count_tag_readings() == 1
    assert replay_writer.metrics().spool_replayed_poll_results == 1


@pytest.mark.asyncio
async def test_mvp_graceful_shutdown_drains_active_poll_and_writer(
    tmp_path: Path,
) -> None:
    engine = create_release_engine(tmp_path)
    connection = make_connection()
    group = make_group(interval_ms=10)
    tag = make_tag()
    seed_configuration(engine, [connection], [group], [tag])
    repository = ReadingRepository(engine)
    queue = ReadingQueue(max_size=10, put_timeout_s=0.1)
    writer = DatabaseWriter(
        queue,
        repository,
        batch_size=1,
        flush_interval_s=1,
        storage_timeout_s=1,
    )
    worker = ConnectionWorker(
        connection,
        make_registry(),
        retry_sleep=no_sleep,
        jitter=lambda: 0.5,
    )
    started = asyncio.Event()
    release_poll = asyncio.Event()

    async def handle_poll(scheduled_group: TagGroupConfig) -> None:
        started.set()
        await release_poll.wait()
        poll_result = await worker.poll_group(scheduled_group, (tag,))
        await queue.put(poll_result)

    writer_task = await start_writer(writer)
    await worker.connect()
    scheduler = PollScheduler([group], handle_poll)
    scheduler_task = asyncio.create_task(scheduler.run())

    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        scheduler.stop()
        release_poll.set()
        await asyncio.wait_for(scheduler_task, timeout=1)
        await queue.join()
        shutdown = await writer.shutdown(timeout_s=1)
    finally:
        scheduler.stop()
        if not scheduler_task.done():
            scheduler_task.cancel()
            await asyncio.gather(scheduler_task, return_exceptions=True)
        await worker.disconnect()
        await writer.shutdown(timeout_s=1)
        await writer_task

    assert shutdown.drained is True
    assert scheduler.snapshot().running is False
    assert repository.count_poll_executions() == 1
    assert repository.count_tag_readings() == 1


def create_release_engine(tmp_path: Path) -> Engine:
    """Create and initialize a SQLite database for release-level tests."""
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'gateway.db'}")
    initialize_schema(engine)
    return engine


def make_registry() -> DriverRegistry:
    """Create the production driver registry subset used by MVP tests."""
    registry = DriverRegistry()
    registry.register(MOCK_PROTOCOL, create_mock_driver)
    return registry


def make_connection(
    connection_id: str = "mock_connection",
    *,
    protocol_options: dict[str, object] | None = None,
) -> ConnectionConfig:
    """Build a mock connection configuration."""
    return ConnectionConfig(
        id=connection_id,
        protocol=MOCK_PROTOCOL,
        endpoint=f"mock://{connection_id}",
        timeout_ms=1000,
        protocol_options=protocol_options or {},
    )


def make_group(
    group_id: str = "fast",
    *,
    connection_id: str = "mock_connection",
    interval_ms: int = 1000,
) -> TagGroupConfig:
    """Build a tag group configuration."""
    return TagGroupConfig(
        id=group_id,
        connection_id=connection_id,
        interval_ms=interval_ms,
        timeout_ms=500,
    )


def make_tag(
    tag_id: str = "temperature",
    *,
    tag_group_id: str = "fast",
) -> TagConfig:
    """Build a numeric tag configuration."""
    return TagConfig(
        id=tag_id,
        tag_group_id=tag_group_id,
        name=tag_id.replace("_", " ").title(),
        address=f"mock://{tag_id}",
        value_type=ValueType.NUMERIC,
    )


def seed_configuration(
    engine: Engine,
    connections: Iterable[ConnectionConfig],
    tag_groups: Iterable[TagGroupConfig],
    tags: Iterable[TagConfig],
) -> None:
    """Persist configuration required by relational reading constraints."""
    repository = ConfigurationRepository(engine)
    repository.upsert_connections(connections)
    repository.upsert_tag_groups(tag_groups)
    repository.upsert_tags(tags)


def poll_status_values(engine: Engine) -> list[str]:
    """Return persisted poll execution statuses in deterministic order."""
    with engine.connect() as connection:
        values = connection.execute(
            select(poll_executions.c.status).order_by(
                poll_executions.c.execution_id,
            )
        ).scalars()
        return [str(value) for value in values]
