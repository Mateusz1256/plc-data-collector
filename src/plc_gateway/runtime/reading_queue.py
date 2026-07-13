"""Bounded runtime queue separating polling from storage writes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from plc_gateway.domain import StorageError
from plc_gateway.runtime.connection_worker import WorkerPollResult


@dataclass(frozen=True, slots=True)
class ReadingQueueMetrics:
    """Observable reading queue occupancy metrics."""

    max_size: int
    size: int
    occupancy_ratio: float
    closed: bool
    dropped_items: int
    put_timeouts: int


@dataclass(frozen=True, slots=True)
class ReadingQueueShutdown:
    """Explicit shutdown outcome for pending queue items."""

    drained: bool
    pending_items: int
    dropped_items: int


class ReadingQueue:
    """Bounded async queue for worker poll results."""

    def __init__(self, *, max_size: int, put_timeout_s: float) -> None:
        """Create a bounded queue with producer wait timeout."""
        if max_size <= 0:
            raise ValueError("max_size must be positive.")
        if put_timeout_s <= 0:
            raise ValueError("put_timeout_s must be positive.")
        self._queue: asyncio.Queue[WorkerPollResult] = asyncio.Queue(maxsize=max_size)
        self._put_timeout_s = put_timeout_s
        self._closed = False
        self._dropped_items = 0
        self._put_timeouts = 0

    @property
    def max_size(self) -> int:
        """Return configured maximum queue size."""
        return self._queue.maxsize

    async def put(self, item: WorkerPollResult) -> None:
        """Put an item, waiting up to the configured producer timeout."""
        if self._closed:
            raise StorageError(
                "Reading queue is closed.",
                code="reading_queue_closed",
                details={"size": self.size},
            )
        try:
            await asyncio.wait_for(self._queue.put(item), timeout=self._put_timeout_s)
        except TimeoutError as error:
            self._put_timeouts += 1
            raise StorageError(
                "Timed out waiting for space in reading queue.",
                code="reading_queue_full",
                details={
                    "max_size": self.max_size,
                    "size": self.size,
                    "timeout_s": self._put_timeout_s,
                },
            ) from error

    async def get(self) -> WorkerPollResult:
        """Get the next queued item for a storage consumer."""
        return await self._queue.get()

    def task_done(self) -> None:
        """Mark one consumed item as fully processed."""
        self._queue.task_done()

    async def join(self) -> None:
        """Wait until all queued items have been processed."""
        await self._queue.join()

    def close(self) -> ReadingQueueShutdown:
        """Close the queue to producers without dropping pending items."""
        self._closed = True
        return ReadingQueueShutdown(
            drained=self.size == 0,
            pending_items=self.size,
            dropped_items=0,
        )

    def drop_pending(self) -> ReadingQueueShutdown:
        """Close the queue and explicitly drop all pending items."""
        self._closed = True
        dropped_now = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            dropped_now += 1
        self._dropped_items += dropped_now
        return ReadingQueueShutdown(
            drained=True,
            pending_items=0,
            dropped_items=dropped_now,
        )

    @property
    def size(self) -> int:
        """Return current queue size."""
        return self._queue.qsize()

    @property
    def closed(self) -> bool:
        """Return whether producers are closed."""
        return self._closed

    def metrics(self) -> ReadingQueueMetrics:
        """Return observable queue metrics."""
        return ReadingQueueMetrics(
            max_size=self.max_size,
            size=self.size,
            occupancy_ratio=self.size / self.max_size,
            closed=self._closed,
            dropped_items=self._dropped_items,
            put_timeouts=self._put_timeouts,
        )
