from __future__ import annotations

from datetime import UTC, datetime

import pytest

from plc_gateway.domain import PollExecution, PollStatus, StorageError
from plc_gateway.runtime import ReadingQueue, WorkerPollResult


def make_poll_result(execution_id: str = "poll-1") -> WorkerPollResult:
    started_at = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    return WorkerPollResult(
        execution=PollExecution(
            execution_id=execution_id,
            connection_id="mock_connection",
            tag_group_id="fast",
            status=PollStatus.SUCCESS,
            started_at=started_at,
            finished_at=started_at,
            requested_tags=0,
        ),
        results=(),
    )


def test_reading_queue_rejects_unbounded_or_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="max_size"):
        ReadingQueue(max_size=0, put_timeout_s=0.1)

    with pytest.raises(ValueError, match="put_timeout_s"):
        ReadingQueue(max_size=1, put_timeout_s=0)


@pytest.mark.asyncio
async def test_reading_queue_put_get_and_metrics() -> None:
    queue = ReadingQueue(max_size=2, put_timeout_s=0.1)
    first = make_poll_result("poll-1")
    second = make_poll_result("poll-2")

    await queue.put(first)
    await queue.put(second)

    metrics = queue.metrics()
    assert metrics.max_size == 2
    assert metrics.size == 2
    assert metrics.occupancy_ratio == 1.0
    assert metrics.closed is False

    assert await queue.get() == first
    queue.task_done()
    assert await queue.get() == second
    queue.task_done()
    await queue.join()
    assert queue.metrics().size == 0


@pytest.mark.asyncio
async def test_reading_queue_full_put_times_out_with_explicit_error() -> None:
    queue = ReadingQueue(max_size=1, put_timeout_s=0.001)
    await queue.put(make_poll_result("poll-1"))

    with pytest.raises(StorageError, match="Timed out"):
        await queue.put(make_poll_result("poll-2"))

    metrics = queue.metrics()
    assert metrics.size == 1
    assert metrics.put_timeouts == 1


@pytest.mark.asyncio
async def test_reading_queue_close_preserves_pending_items() -> None:
    queue = ReadingQueue(max_size=2, put_timeout_s=0.1)
    item = make_poll_result()
    await queue.put(item)

    shutdown = queue.close()

    assert shutdown.drained is False
    assert shutdown.pending_items == 1
    assert shutdown.dropped_items == 0
    assert queue.closed is True
    with pytest.raises(StorageError, match="closed"):
        await queue.put(make_poll_result("poll-2"))
    assert await queue.get() == item
    queue.task_done()
    await queue.join()


@pytest.mark.asyncio
async def test_reading_queue_drop_pending_reports_discarded_items() -> None:
    queue = ReadingQueue(max_size=2, put_timeout_s=0.1)
    await queue.put(make_poll_result("poll-1"))
    await queue.put(make_poll_result("poll-2"))

    shutdown = queue.drop_pending()

    assert shutdown.drained is True
    assert shutdown.pending_items == 0
    assert shutdown.dropped_items == 2
    metrics = queue.metrics()
    assert metrics.size == 0
    assert metrics.dropped_items == 2


@pytest.mark.asyncio
async def test_reading_queue_consumer_contract_uses_task_done_and_join() -> None:
    queue = ReadingQueue(max_size=1, put_timeout_s=0.1)
    await queue.put(make_poll_result())

    item = await queue.get()
    assert item.execution.execution_id == "poll-1"
    queue.task_done()
    await queue.join()

    assert queue.metrics().occupancy_ratio == 0.0
