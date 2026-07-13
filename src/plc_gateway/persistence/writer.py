"""Batched async database writer for queued readings."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from plc_gateway.persistence.spool import SpoolEntry
from plc_gateway.runtime import ReadingQueue, RetryPolicy, WorkerPollResult
from plc_gateway.runtime.retry import run_with_retry

LOGGER = logging.getLogger(__name__)


class PollResultRepository(Protocol):
    """Repository contract required by the database writer."""

    def save_poll_results(self, poll_results: list[WorkerPollResult]) -> int:
        """Persist a batch of poll results and return inserted reading count."""
        ...


class PollResultSpool(Protocol):
    """Durable spool contract required by the database writer."""

    def append(self, poll_results: list[WorkerPollResult]) -> int:
        """Persist poll results for later replay."""
        ...

    def fetch_due(
        self,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> list[SpoolEntry]:
        """Return due spool entries."""
        ...

    def mark_replayed(self, execution_ids: list[str]) -> int:
        """Delete entries confirmed in the main database."""
        ...

    def mark_failed(self, execution_ids: list[str], *, retry_delay_s: float) -> int:
        """Record failed replay attempts and schedule another attempt."""
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
    spooled_poll_results: int = 0
    spool_replayed_poll_results: int = 0
    spool_failed_replays: int = 0
    spool_full_failures: int = 0
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
        retry_policy: RetryPolicy | None = None,
        spool: PollResultSpool | None = None,
        spool_replay_batch_size: int | None = None,
        spool_retry_delay_s: float = 1.0,
        spool_timeout_s: float | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        logger: logging.Logger | None = None,
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
        if spool_replay_batch_size is not None and spool_replay_batch_size <= 0:
            raise ValueError("spool_replay_batch_size must be positive.")
        if spool_retry_delay_s < 0:
            raise ValueError("spool_retry_delay_s must be non-negative.")
        if spool_timeout_s is not None and spool_timeout_s <= 0:
            raise ValueError("spool_timeout_s must be positive.")

        self._queue = queue
        self._repository = repository
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._storage_timeout_s = storage_timeout_s
        self._retry_policy = retry_policy or _retry_policy_from_legacy_settings(
            max_retries=max_retries,
            retry_delay_s=retry_delay_s,
        )
        self._spool = spool
        self._spool_replay_batch_size = spool_replay_batch_size or batch_size
        self._spool_retry_delay_s = spool_retry_delay_s
        self._spool_timeout_s = spool_timeout_s or storage_timeout_s
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._logger = LOGGER if logger is None else logger
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
        self._spooled_poll_results = 0
        self._spool_replayed_poll_results = 0
        self._spool_failed_replays = 0
        self._spool_full_failures = 0
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
            spooled_poll_results=self._spooled_poll_results,
            spool_replayed_poll_results=self._spool_replayed_poll_results,
            spool_failed_replays=self._spool_failed_replays,
            spool_full_failures=self._spool_full_failures,
            last_error=self._last_error,
        )

    def _should_stop(self) -> bool:
        return (
            self._stop_requested.is_set()
            and self._queue.size == 0
            and not self._pending_batch
        )

    async def _consume_or_flush(self) -> None:
        if not self._pending_batch and await self._replay_spool_once():
            return

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
        persisted = await self._persist_batch(batch)
        if persisted:
            self._successful_batches += 1
            self._successful_poll_results += len(batch)
            self._last_error = None
            self._ack_batch(len(batch))
            return

        spooled = await self._spool_batch(batch)
        if spooled:
            self._failed_batches += 1
            self._failed_poll_results += len(batch)
            self._ack_batch(len(batch))
            return

        self._failed_batches += 1
        self._failed_poll_results += len(batch)
        self._ack_batch(len(batch))

    async def _persist_batch(self, batch: list[WorkerPollResult]) -> bool:
        correlation_id = _batch_correlation_id(batch)
        try:
            inserted, _ = await run_with_retry(
                lambda: self._save_batch(batch),
                self._retry_policy,
                operation_name="database_batch_write",
                correlation_id=correlation_id,
                component_id="database-writer",
                logger=self._logger,
                sleep=self._sleep,
                on_retry=self._record_retry,
            )
        except Exception as error:
            self._last_error = str(error) or error.__class__.__name__
            return False

        self._inserted_readings += inserted
        return True

    async def _spool_batch(self, batch: list[WorkerPollResult]) -> bool:
        if self._spool is None:
            return False
        try:
            inserted = await asyncio.wait_for(
                asyncio.to_thread(self._spool.append, batch),
                timeout=self._spool_timeout_s,
            )
        except Exception as error:
            self._last_error = str(error) or error.__class__.__name__
            self._spool_full_failures += 1
            return False
        self._spooled_poll_results += inserted
        return True

    async def _replay_spool_once(self) -> bool:
        if self._spool is None:
            return False
        try:
            entries = await asyncio.wait_for(
                asyncio.to_thread(
                    self._spool.fetch_due,
                    limit=self._spool_replay_batch_size,
                ),
                timeout=self._spool_timeout_s,
            )
        except Exception as error:
            self._last_error = str(error) or error.__class__.__name__
            return False
        if not entries:
            return False

        poll_results = [_entry_poll_result(entry) for entry in entries]
        execution_ids = [_entry_execution_id(entry) for entry in entries]
        if await self._persist_batch(poll_results):
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._spool.mark_replayed, execution_ids),
                    timeout=self._spool_timeout_s,
                )
            except Exception as error:
                self._last_error = str(error) or error.__class__.__name__
            else:
                self._spool_replayed_poll_results += len(poll_results)
            return True

        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self._spool.mark_failed,
                    execution_ids,
                    retry_delay_s=self._spool_retry_delay_s,
                ),
                timeout=self._spool_timeout_s,
            )
        except Exception as error:
            self._last_error = str(error) or error.__class__.__name__
            return True
        self._spool_failed_replays += len(poll_results)
        return True

    async def _save_batch(self, batch: list[WorkerPollResult]) -> int:
        return await asyncio.wait_for(
            asyncio.to_thread(self._repository.save_poll_results, batch),
            timeout=self._storage_timeout_s,
        )

    def _record_retry(
        self,
        _error: Exception,
        _delay_s: float,
        _retry_number: int,
    ) -> None:
        self._retry_attempts += 1

    def _ack_batch(self, count: int) -> None:
        del self._pending_batch[:count]
        for _ in range(count):
            self._queue.task_done()


def _retry_policy_from_legacy_settings(
    *,
    max_retries: int,
    retry_delay_s: float,
) -> RetryPolicy:
    retry_delays = max(max_retries, 1)
    max_delay_s = retry_delay_s * 2 ** (retry_delays - 1)
    return RetryPolicy(
        max_attempts=max_retries + 1,
        initial_delay_s=retry_delay_s,
        max_delay_s=max_delay_s,
        jitter_ratio=0.2 if retry_delay_s > 0 else 0,
    )


def _batch_correlation_id(batch: list[WorkerPollResult]) -> str:
    if not batch:
        return "database-batch:empty"
    return f"database-batch:{batch[0].execution.execution_id}"


def _entry_poll_result(entry: object) -> WorkerPollResult:
    poll_result = getattr(entry, "poll_result", None)
    if not isinstance(poll_result, WorkerPollResult):
        raise TypeError("Spool entry must expose a WorkerPollResult poll_result.")
    return poll_result


def _entry_execution_id(entry: object) -> str:
    execution_id = getattr(entry, "execution_id", None)
    if not isinstance(execution_id, str):
        raise TypeError("Spool entry must expose a string execution_id.")
    return execution_id
