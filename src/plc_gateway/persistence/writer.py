"""Batched async database writer for queued readings."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from plc_gateway.runtime import ReadingQueue, WorkerPollResult


class PollResultRepository(Protocol):
    """Repository contract required by the database writer."""

    def save_poll_results(self, poll_results: list[WorkerPollResult]) -> int:
        """Persist a batch of poll results and return inserted reading count."""
        ...


@dataclass(frozen=True, slots=True)
class DatabaseWriterMetrics:
    """Observable database writer metrics."""

    running: bool
    successful_batches: int
    failed_batches: int
    successful_poll_results: int
    failed_poll_results: int
    inserted_readings: int
    retry_attempts: int
    pending_batch_size: int
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class DatabaseWriterShutdown:
    """Outcome of a graceful database writer shutdown."""

    drained: bool
    pending_queue_items: int
    pending_batch_items: int


class DatabaseWriter:
    """Consume reading queue items and persist them in batches."""

    def __init__(
        self,
        queue: ReadingQueue,
        repository: PollResultRepository,
        *,
        batch_size: int,
        flush_interval_s: float,
        storage_timeout_s: float,
        max_retries: int = 2,
        retry_delay_s: float = 0.1,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Create a database writer."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive.")
        if flush_interval_s <= 0:
            raise ValueError("flush_interval_s must be positive.")
        if storage_timeout_s <= 0:
            raise ValueError("storage_timeout_s must be positive.")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative.")
        if retry_delay_s < 0:
            raise ValueError("retry_delay_s must be non-negative.")

        self._queue = queue
        self._repository = repository
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._storage_timeout_s = storage_timeout_s
        self._max_retries = max_retries
        self._retry_delay_s = retry_delay_s
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._stop_requested = asyncio.Event()
        self._stopped = asyncio.Event()
        self._stopped.set()
        self._pending_batch: list[WorkerPollResult] = []
        self._running = False
        self._successful_batches = 0
        self._failed_batches = 0
        self._successful_poll_results = 0
        self._failed_poll_results = 0
        self._inserted_readings = 0
        self._retry_attempts = 0
        self._last_error: str | None = None

    async def run(self) -> None:
        """Run the writer until stopped and drained."""
        if self._running:
            raise RuntimeError("DatabaseWriter is already running.")
        self._running = True
        self._stopped.clear()
        try:
            while True:
                if self._should_stop():
                    break
                await self._consume_or_flush()
        except asyncio.CancelledError:
            await self._flush_pending()
            raise
        finally:
            await self._flush_pending()
            self._running = False
            self._stopped.set()

    def stop(self) -> None:
        """Request writer shutdown after draining available work."""
        self._stop_requested.set()

    async def shutdown(self, timeout_s: float) -> DatabaseWriterShutdown:
        """Request graceful shutdown and wait up to a timeout."""
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive.")
        self.stop()
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=timeout_s)
        except TimeoutError:
            return DatabaseWriterShutdown(
                drained=False,
                pending_queue_items=self._queue.size,
                pending_batch_items=len(self._pending_batch),
            )
        return DatabaseWriterShutdown(
            drained=self._queue.size == 0 and not self._pending_batch,
            pending_queue_items=self._queue.size,
            pending_batch_items=len(self._pending_batch),
        )

    def metrics(self) -> DatabaseWriterMetrics:
        """Return observable writer metrics."""
        return DatabaseWriterMetrics(
            running=self._running,
            successful_batches=self._successful_batches,
            failed_batches=self._failed_batches,
            successful_poll_results=self._successful_poll_results,
            failed_poll_results=self._failed_poll_results,
            inserted_readings=self._inserted_readings,
            retry_attempts=self._retry_attempts,
            pending_batch_size=len(self._pending_batch),
            last_error=self._last_error,
        )

    def _should_stop(self) -> bool:
        return (
            self._stop_requested.is_set()
            and self._queue.size == 0
            and not self._pending_batch
        )

    async def _consume_or_flush(self) -> None:
        if len(self._pending_batch) >= self._batch_size:
            await self._flush_pending()
            return
        if self._stop_requested.is_set() and self._queue.size == 0:
            await self._flush_pending()
            return

        item = await self._wait_for_item_or_stop()
        if item is None:
            await self._flush_pending()
            return

        self._pending_batch.append(item)
        if len(self._pending_batch) >= self._batch_size:
            await self._flush_pending()

    async def _wait_for_item_or_stop(self) -> WorkerPollResult | None:
        get_task = asyncio.create_task(self._queue.get())
        stop_task = asyncio.create_task(self._stop_requested.wait())
        done, pending = await asyncio.wait(
            {get_task, stop_task},
            timeout=self._flush_interval_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if get_task in done:
            return get_task.result()
        return None

    async def _flush_pending(self) -> None:
        if not self._pending_batch:
            return

        batch = list(self._pending_batch)
        for attempt in range(self._max_retries + 1):
            try:
                inserted = await asyncio.wait_for(
                    asyncio.to_thread(self._repository.save_poll_results, batch),
                    timeout=self._storage_timeout_s,
                )
            except Exception as error:
                self._last_error = str(error) or error.__class__.__name__
                if attempt < self._max_retries:
                    self._retry_attempts += 1
                    await self._sleep(self._retry_delay_s)
                    continue
                self._failed_batches += 1
                self._failed_poll_results += len(batch)
                self._ack_batch(len(batch))
                return

            self._successful_batches += 1
            self._successful_poll_results += len(batch)
            self._inserted_readings += inserted
            self._last_error = None
            self._ack_batch(len(batch))
            return

    def _ack_batch(self, count: int) -> None:
        del self._pending_batch[:count]
        for _ in range(count):
            self._queue.task_done()
