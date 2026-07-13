from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine

from plc_gateway.domain import (
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
from plc_gateway.persistence import (
    ConfigurationRepository,
    DatabaseWriter,
    DurableSqliteSpool,
    ReadingRepository,
    create_sqlite_engine,
    initialize_schema,
)
from plc_gateway.runtime import ReadingQueue, WorkerPollResult


class FakeRepository:
    """Configurable repository for writer tests."""

    def __init__(
        self,
        *,
        failures_before_success: int = 0,
        always_fail: bool = False,
    ) -> None:
        """Create a fake repository."""
        self.failures_before_success = failures_before_success
        self.always_fail = always_fail
        self.batches: list[list[WorkerPollResult]] = []

    def save_poll_results(self, poll_results: list[WorkerPollResult]) -> int:
        """Record a batch or raise a configured storage failure."""
        if self.always_fail:
            raise StorageError("storage failed", code="fake_storage_failed")
        if self.failures_before_success > 0:
            self.failures_before_success -= 1
            raise StorageError("storage failed", code="fake_storage_failed")
        self.batches.append(list(poll_results))
        return sum(len(item.results) for item in poll_results)


async def no_sleep(_: float) -> None:
    """Deterministic retry sleeper."""


def timestamp() -> datetime:
    return datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def make_poll_result(
    execution_id: str,
    *,
    tag_id: str = "temperature",
) -> WorkerPollResult:
    now = timestamp()
    return WorkerPollResult(
        execution=PollExecution(
            execution_id=execution_id,
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
                tag_id=tag_id,
                value=TagValue.numeric(20.5),
                source_timestamp=now,
                received_at=now,
            ),
        ),
    )


def make_queue() -> ReadingQueue:
    return ReadingQueue(max_size=10, put_timeout_s=0.1)


async def start_writer(writer: DatabaseWriter) -> asyncio.Task[None]:
    """Start a writer task and let it enter its run loop."""
    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0)
    return task


async def wait_until(
    condition: Callable[[], bool],
    *,
    timeout_s: float = 1.0,
) -> None:
    """Wait until a zero-argument predicate returns true."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


@pytest.mark.asyncio
async def test_database_writer_flushes_when_batch_size_is_reached() -> None:
    queue = make_queue()
    repository = FakeRepository()
    writer = DatabaseWriter(
        queue,
        repository,
        batch_size=2,
        flush_interval_s=10,
        storage_timeout_s=1,
    )
    task = await start_writer(writer)

    await queue.put(make_poll_result("poll-1"))
    await queue.put(make_poll_result("poll-2"))
    await queue.join()
    shutdown = await writer.shutdown(timeout_s=1)
    await task

    assert shutdown.drained is True
    batch_execution_ids = [
        [item.execution.execution_id for item in batch] for batch in repository.batches
    ]
    assert batch_execution_ids == [["poll-1", "poll-2"]]
    metrics = writer.metrics()
    assert metrics.successful_batches == 1
    assert metrics.successful_poll_results == 2
    assert metrics.inserted_readings == 2


@pytest.mark.asyncio
async def test_database_writer_flushes_after_interval() -> None:
    queue = make_queue()
    repository = FakeRepository()
    writer = DatabaseWriter(
        queue,
        repository,
        batch_size=10,
        flush_interval_s=0.01,
        storage_timeout_s=1,
    )
    task = await start_writer(writer)

    await queue.put(make_poll_result("poll-1"))
    await queue.join()
    await writer.shutdown(timeout_s=1)
    await task

    assert len(repository.batches) == 1
    assert repository.batches[0][0].execution.execution_id == "poll-1"


@pytest.mark.asyncio
async def test_database_writer_retries_storage_errors() -> None:
    queue = make_queue()
    repository = FakeRepository(failures_before_success=1)
    writer = DatabaseWriter(
        queue,
        repository,
        batch_size=1,
        flush_interval_s=1,
        storage_timeout_s=1,
        max_retries=2,
        retry_delay_s=0,
        sleep=no_sleep,
    )
    task = await start_writer(writer)

    await queue.put(make_poll_result("poll-1"))
    await queue.join()
    await writer.shutdown(timeout_s=1)
    await task

    assert len(repository.batches) == 1
    metrics = writer.metrics()
    assert metrics.retry_attempts == 1
    assert metrics.successful_batches == 1
    assert metrics.failed_batches == 0


@pytest.mark.asyncio
async def test_database_writer_records_failed_batches_after_retry_exhaustion() -> None:
    queue = make_queue()
    repository = FakeRepository(failures_before_success=3)
    writer = DatabaseWriter(
        queue,
        repository,
        batch_size=1,
        flush_interval_s=1,
        storage_timeout_s=1,
        max_retries=1,
        retry_delay_s=0,
        sleep=no_sleep,
    )
    task = await start_writer(writer)

    await queue.put(make_poll_result("poll-1"))
    await queue.join()
    await writer.shutdown(timeout_s=1)
    await task

    metrics = writer.metrics()
    assert metrics.failed_batches == 1
    assert metrics.failed_poll_results == 1
    assert metrics.last_error == "storage failed"


@pytest.mark.asyncio
async def test_database_writer_retry_does_not_duplicate_event_ids(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "gateway.db"
    engine = create_sqlite_engine(f"sqlite:///{database_path}")
    initialize_schema(engine)
    seed_configuration(engine)
    real_repository = ReadingRepository(engine)
    repository = PersistThenFailOnceRepository(real_repository)
    queue = make_queue()
    writer = DatabaseWriter(
        queue,
        repository,
        batch_size=1,
        flush_interval_s=1,
        storage_timeout_s=1,
        max_retries=1,
        retry_delay_s=0,
        sleep=no_sleep,
    )
    task = await start_writer(writer)

    await queue.put(make_poll_result("poll-1"))
    await queue.join()
    await writer.shutdown(timeout_s=1)
    await task

    assert real_repository.count_poll_executions() == 1
    assert real_repository.count_tag_readings() == 1


@pytest.mark.asyncio
async def test_database_writer_spools_after_database_failure(
    tmp_path: Path,
) -> None:
    queue = make_queue()
    repository = FakeRepository(always_fail=True)
    spool = DurableSqliteSpool(tmp_path / "spool.db", max_items=10)
    writer = DatabaseWriter(
        queue,
        repository,
        batch_size=1,
        flush_interval_s=1,
        storage_timeout_s=1,
        max_retries=0,
        spool=spool,
        spool_timeout_s=1,
    )
    task = await start_writer(writer)

    await queue.put(make_poll_result("poll-1"))
    await queue.join()
    await writer.shutdown(timeout_s=1)
    await task

    assert spool.count() == 1
    metrics = writer.metrics()
    assert metrics.spooled_poll_results == 1
    assert metrics.failed_batches == 1


@pytest.mark.asyncio
async def test_database_writer_replays_spool_after_database_recovers(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "gateway.db"
    engine = create_sqlite_engine(f"sqlite:///{database_path}")
    initialize_schema(engine)
    seed_configuration(engine)
    repository = ReadingRepository(engine)
    spool = DurableSqliteSpool(tmp_path / "spool.db", max_items=10)
    spool.append([make_poll_result("poll-1")])
    writer = DatabaseWriter(
        make_queue(),
        repository,
        batch_size=1,
        flush_interval_s=0.01,
        storage_timeout_s=1,
        max_retries=0,
        spool=spool,
        spool_timeout_s=1,
    )
    task = await start_writer(writer)

    await wait_until(lambda: spool.count() == 0)
    await writer.shutdown(timeout_s=1)
    await task

    assert repository.count_poll_executions() == 1
    assert repository.count_tag_readings() == 1
    assert writer.metrics().spool_replayed_poll_results == 1


@pytest.mark.asyncio
async def test_database_writer_spool_replay_does_not_duplicate_event_ids(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "gateway.db"
    engine = create_sqlite_engine(f"sqlite:///{database_path}")
    initialize_schema(engine)
    seed_configuration(engine)
    real_repository = ReadingRepository(engine)
    spool = DurableSqliteSpool(tmp_path / "spool.db", max_items=10)
    failing_repository = PersistThenFailOnceRepository(real_repository)
    queue = make_queue()
    first_writer = DatabaseWriter(
        queue,
        failing_repository,
        batch_size=1,
        flush_interval_s=1,
        storage_timeout_s=1,
        max_retries=0,
        spool=spool,
        spool_timeout_s=1,
    )
    first_task = await start_writer(first_writer)

    await queue.put(make_poll_result("poll-1"))
    await queue.join()
    await first_writer.shutdown(timeout_s=1)
    await first_task

    replay_writer = DatabaseWriter(
        make_queue(),
        real_repository,
        batch_size=1,
        flush_interval_s=0.01,
        storage_timeout_s=1,
        max_retries=0,
        spool=spool,
        spool_timeout_s=1,
    )
    replay_task = await start_writer(replay_writer)

    await wait_until(lambda: spool.count() == 0)
    await replay_writer.shutdown(timeout_s=1)
    await replay_task

    assert real_repository.count_poll_executions() == 1
    assert real_repository.count_tag_readings() == 1


@pytest.mark.asyncio
async def test_database_writer_schedules_failed_spool_replay(
    tmp_path: Path,
) -> None:
    repository = FakeRepository(always_fail=True)
    spool = DurableSqliteSpool(tmp_path / "spool.db", max_items=10)
    spool.append([make_poll_result("poll-1")])
    writer = DatabaseWriter(
        make_queue(),
        repository,
        batch_size=1,
        flush_interval_s=0.01,
        storage_timeout_s=1,
        max_retries=0,
        spool=spool,
        spool_retry_delay_s=60,
        spool_timeout_s=1,
    )
    task = await start_writer(writer)

    await wait_until(lambda: writer.metrics().spool_failed_replays == 1)
    await writer.shutdown(timeout_s=1)
    await task

    assert spool.fetch_due(limit=10, now=timestamp()) == []
    metrics = writer.metrics()
    assert metrics.spool_failed_replays == 1
    assert metrics.last_error == "storage failed"


@pytest.mark.asyncio
async def test_database_writer_shutdown_reports_timeout() -> None:
    queue = make_queue()
    repository = FakeRepository()
    writer = DatabaseWriter(
        queue,
        repository,
        batch_size=1,
        flush_interval_s=10,
        storage_timeout_s=1,
    )
    task = await start_writer(writer)

    shutdown = await writer.shutdown(timeout_s=0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert shutdown.drained is False


class PersistThenFailOnceRepository:
    """Repository wrapper that simulates failure after commit."""

    def __init__(self, repository: ReadingRepository) -> None:
        """Create a wrapper around a real repository."""
        self._repository = repository
        self._should_fail = True

    def save_poll_results(self, poll_results: list[WorkerPollResult]) -> int:
        """Persist once, then raise to force idempotent retry."""
        inserted = self._repository.save_poll_results(poll_results)
        if self._should_fail:
            self._should_fail = False
            raise StorageError("post-commit failure", code="post_commit_failure")
        return inserted


def seed_configuration(engine: Engine) -> None:
    repository = ConfigurationRepository(engine)
    repository.upsert_connections(
        [
            ConnectionConfig(
                id="mock_connection",
                protocol="mock",
                endpoint="mock://local",
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
            )
        ]
    )
