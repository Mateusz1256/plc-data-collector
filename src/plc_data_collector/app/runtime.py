"""Composition root that wires configuration, workers and storage together."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from plc_data_collector.api.state import RuntimeApiState
from plc_data_collector.app.config import (
    ENV_PREFIX,
    CollectorConfig,
    load_collector_config,
)
from plc_data_collector.app.settings import RuntimeSettings
from plc_data_collector.domain import (
    CollectorError,
    ConfigurationError,
    StorageError,
    TagConfig,
    TagGroupConfig,
)
from plc_data_collector.persistence import (
    ConfigurationRepository,
    DatabaseWriter,
    DurableSqliteSpool,
    ReadingRepository,
    create_database_engine,
    run_migrations,
    wait_for_database,
)
from plc_data_collector.persistence.writer import PollResultRepository
from plc_data_collector.protocols import DriverRegistry, default_driver_registry
from plc_data_collector.runtime import (
    ConnectionWorker,
    PollScheduler,
    ReadingQueue,
    WorkerPollResult,
)

LOGGER = logging.getLogger(__name__)
RepositoryFactory = Callable[[Engine], PollResultRepository]
DATABASE_PROBE_INTERVAL_S = 1.0


class _StoppableRepository:
    """Repository wrapper that fails fast once the stop budget is spent.

    The Database Writer then sends its last batch to the Spool at once instead
    of retrying a stalled database past the stop deadline.
    """

    def __init__(self, inner: PollResultRepository) -> None:
        self._inner = inner
        self.closed = False

    def save_poll_results(self, poll_results: list[WorkerPollResult]) -> int:
        if self.closed:
            raise StorageError(
                "Runtime is stopping; Poll Results go to the Spool.",
                code="runtime_stopping",
            )
        return self._inner.save_poll_results(poll_results)


@dataclass(frozen=True, slots=True)
class _ScheduledGroup:
    worker: ConnectionWorker
    tags: tuple[TagConfig, ...]


class CollectorRuntime:
    """Run the collector: poll enabled Tag Groups and store their Poll Results."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        registry: DriverRegistry | None = None,
        repository_factory: RepositoryFactory | None = None,
    ) -> None:
        """Create a runtime; nothing connects until ``start()``."""
        self._settings = settings
        self._registry = registry or default_driver_registry()
        self._repository_factory: RepositoryFactory = (
            repository_factory or ReadingRepository
        )
        self._engine: Engine | None = None
        self._config: CollectorConfig | None = None
        self._workers: tuple[ConnectionWorker, ...] = ()
        self._schedule: dict[str, _ScheduledGroup] = {}
        self._queue: ReadingQueue | None = None
        self._spool: DurableSqliteSpool | None = None
        self._writer: DatabaseWriter | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._scheduler: PollScheduler | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._repository: _StoppableRepository | None = None
        # Lifecycle: idle -> starting -> running -> stopping -> stopped.
        self._phase = "idle"
        self._stop_requested = False
        self._startup_task: asyncio.Task[None] | None = None
        self._start_finished = asyncio.Event()
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        """Prepare the database and configuration, then start polling.

        A failure leaves nothing running: every resource created so far is
        released before the error propagates.
        """
        if self._phase != "idle":
            raise RuntimeError("CollectorRuntime can only be started once.")
        self._phase = "starting"
        settings = self._settings
        LOGGER.info(
            "runtime_starting",
            extra={
                "database_url": _mask_database_url(settings.database_url),
                "config_file": (
                    None if settings.config_file is None else str(settings.config_file)
                ),
                "data_dir": str(settings.data_dir),
            },
        )
        # The startup steps run in their own task so stop() can cancel them
        # without cancelling the caller of start().
        self._startup_task = asyncio.create_task(
            self._startup(), name="runtime-startup"
        )
        try:
            await self._startup_task
            if self._stop_requested:
                raise asyncio.CancelledError
        except BaseException as error:
            current = asyncio.current_task()
            aborted = (
                self._stop_requested
                and isinstance(error, asyncio.CancelledError)
                and not (current is not None and current.cancelling())
            )
            if aborted:
                LOGGER.info("runtime_start_aborted")
            else:
                LOGGER.error(
                    "runtime_start_failed",
                    extra={
                        "error_code": _error_code(error),
                        "error_type": type(error).__name__,
                    },
                )
            await self._release(timeout_s=settings.storage_timeout_s)
            self._start_finished.set()
            if aborted:
                raise CollectorError(
                    "Runtime start was aborted by stop().",
                    code="runtime_start_aborted",
                ) from None
            raise
        finally:
            self._startup_task = None
        self._phase = "running"
        self._start_finished.set()
        LOGGER.info(
            "runtime_started",
            extra={
                "workers": len(self._workers),
                "tag_groups": len(self._schedule),
            },
        )

    async def _startup(self) -> None:
        config_file = self._settings.config_file
        if config_file is None:
            # Removed in Task 6, when configuration moves into the database.
            raise ConfigurationError(
                f"Collector configuration file is required; set {ENV_PREFIX}CONFIG.",
                code="config_file_required",
            )
        await self._prepare_database()
        config = load_collector_config(config_file)
        await asyncio.to_thread(self._upsert_configuration, config)
        self._config = config
        self._build_components(config)
        await self._connect_workers()
        self._start_tasks()

    async def stop(self, timeout_s: float = 10.0) -> None:
        """Stop polling, drain the Reading Queue and release resources.

        ``timeout_s`` bounds the drain: Poll Results the database does not take
        in time go to the Spool. A stop() during start() aborts the start, which
        then raises ``CollectorError(code="runtime_start_aborted")``. Calling
        stop() again, or before start(), does nothing.
        """
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive.")
        self._stop_requested = True
        if self._phase == "starting":
            task = self._startup_task
            if task is not None:
                task.cancel()
            await self._start_finished.wait()
            return
        if self._phase == "running":
            await self._release(timeout_s=timeout_s)
        elif self._phase == "stopping":
            await self._stopped.wait()

    def api_state(self) -> RuntimeApiState:
        """Return a fresh snapshot of the runtime for the API."""
        writer_metrics = None if self._writer is None else self._writer.metrics()
        return RuntimeApiState(
            configuration_loaded=self._config is not None,
            storage_available=(
                writer_metrics is not None and writer_metrics.last_error is None
            ),
            workers=tuple(worker.status() for worker in self._workers),
            worker_metrics=tuple(worker.metrics() for worker in self._workers),
            queue_metrics=None if self._queue is None else self._queue.metrics(),
            writer_metrics=writer_metrics,
            spool_metrics=None if self._spool is None else self._spool.metrics(),
        )

    async def _prepare_database(self) -> None:
        settings = self._settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._engine = create_database_engine(settings.database_url)
        await self._wait_for_database(self._engine)
        try:
            await asyncio.to_thread(run_migrations, settings.database_url)
        except Exception as error:
            # Alembic and SQLAlchemy errors may quote the database URL, so only
            # the error type leaves this block.
            raise StorageError(
                "Database migration failed.",
                code="database_migration_failed",
                details={"error_type": type(error).__name__},
            ) from None

    async def _wait_for_database(self, engine: Engine) -> None:
        """Probe the database until it answers or the wait timeout expires.

        Each probe is a single attempt in a thread, and the waiting happens on
        the event loop, so stop() can abort the wait between probes.
        """
        timeout_s = self._settings.database_wait_timeout_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            try:
                await asyncio.to_thread(wait_for_database, engine, timeout_s=0)
                return
            except StorageError as error:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise StorageError(
                        f"Database did not become available within {timeout_s:g}s.",
                        code="database_unavailable",
                        details=error.details,
                    ) from None
            await asyncio.sleep(min(DATABASE_PROBE_INTERVAL_S, remaining))

    def _upsert_configuration(self, config: CollectorConfig) -> None:
        if self._engine is None:
            raise RuntimeError("Database engine is not created.")
        repository = ConfigurationRepository(self._engine)
        repository.upsert_connections(config.connections)
        repository.upsert_tag_groups(config.tag_groups)
        repository.upsert_tags(config.tags)

    def _build_components(self, config: CollectorConfig) -> None:
        if self._engine is None:
            raise RuntimeError("Database engine is not created.")
        settings = self._settings
        groups_by_connection = _enabled_groups_with_tags(config)
        workers: list[ConnectionWorker] = []
        for connection in config.connections:
            groups = groups_by_connection.get(connection.id)
            if not connection.enabled or not groups:
                LOGGER.debug(
                    "runtime_connection_skipped",
                    extra={"connection_id": connection.id},
                )
                continue
            worker = ConnectionWorker(connection, self._registry)
            workers.append(worker)
            for group, tags in groups:
                self._schedule[group.id] = _ScheduledGroup(worker=worker, tags=tags)
        self._workers = tuple(workers)
        self._queue = ReadingQueue(
            max_size=settings.queue_max_size,
            put_timeout_s=settings.storage_timeout_s,
        )
        self._spool = DurableSqliteSpool(
            settings.spool_path,
            max_items=settings.spool_max_items,
        )
        self._repository = _StoppableRepository(self._repository_factory(self._engine))
        self._writer = DatabaseWriter(
            self._queue,
            self._repository,
            batch_size=settings.writer_batch_size,
            flush_interval_s=settings.writer_flush_interval_s,
            storage_timeout_s=settings.storage_timeout_s,
            spool=self._spool,
        )
        scheduled_groups = [
            group for group in config.tag_groups if group.id in self._schedule
        ]
        self._scheduler = PollScheduler(scheduled_groups, self._poll)

    async def _connect_workers(self) -> None:
        results = await asyncio.gather(
            *(worker.connect() for worker in self._workers),
            return_exceptions=True,
        )
        for worker, result in zip(self._workers, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                # The worker reconnects on its next poll; one unreachable
                # Connection must not stop the others.
                LOGGER.warning(
                    "runtime_connection_unavailable",
                    extra={
                        "connection_id": worker.connection.id,
                        "error_code": _error_code(result),
                    },
                )

    def _start_tasks(self) -> None:
        if self._writer is None or self._scheduler is None:
            raise RuntimeError("Runtime components are not built.")
        self._writer_task = asyncio.create_task(
            self._writer.run(), name="database-writer"
        )
        self._scheduler_task = asyncio.create_task(
            self._scheduler.run(), name="poll-scheduler"
        )

    async def _poll(self, group: TagGroupConfig) -> None:
        if self._queue is None:
            raise RuntimeError("Reading Queue is not created.")
        scheduled = self._schedule[group.id]
        result = await scheduled.worker.poll_group(group, scheduled.tags)
        await self._queue.put(result)

    async def _release(self, *, timeout_s: float) -> None:
        """Stop every component created so far; used by stop() and failed starts."""
        self._phase = "stopping"
        deadline = asyncio.get_running_loop().time() + timeout_s
        LOGGER.info("runtime_stopping")
        try:
            await self._stop_scheduler()
            await self._stop_writer(deadline)
            await self._disconnect_workers()
            if self._engine is not None:
                self._engine.dispose()
        finally:
            self._phase = "stopped"
            self._stopped.set()
            LOGGER.info("runtime_stopped")

    async def _stop_scheduler(self) -> None:
        if self._scheduler is not None:
            self._scheduler.stop()
        task = self._scheduler_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._scheduler is not None:
            await self._scheduler.cancel_active_cycles()

    async def _stop_writer(self, deadline: float) -> None:
        if self._queue is not None:
            self._queue.close()
        task = self._writer_task
        if self._writer is None or task is None:
            return
        remaining = deadline - asyncio.get_running_loop().time()
        shutdown = await self._writer.shutdown(max(remaining, 0.001))
        if not task.done():
            # Out of time: make further writes fail fast, so the writer's
            # cancel path moves its pending batch to the Spool right away.
            if self._repository is not None:
                self._repository.closed = True
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not shutdown.drained:
            LOGGER.warning(
                "runtime_writer_not_drained",
                extra={
                    "pending_queue_items": shutdown.pending_queue_items,
                    "pending_batch_items": shutdown.pending_batch_items,
                },
            )
        await self._spool_remaining_queue()

    async def _spool_remaining_queue(self) -> None:
        """Move Poll Results the writer never took from the queue to the Spool."""
        queue = self._queue
        if queue is None or queue.size == 0:
            return
        remaining: list[WorkerPollResult] = []
        while queue.size:
            remaining.append(await queue.get())
            queue.task_done()
        try:
            if self._spool is None:
                raise StorageError("Spool is not created.", code="spool_missing")
            await asyncio.to_thread(self._spool.append, remaining)
        except Exception as error:
            LOGGER.error(
                "runtime_queue_spool_failed",
                extra={
                    "poll_results": len(remaining),
                    "error_code": _error_code(error),
                },
            )
        else:
            LOGGER.warning(
                "runtime_queue_spooled", extra={"poll_results": len(remaining)}
            )

    async def _disconnect_workers(self) -> None:
        for worker in self._workers:
            try:
                await worker.disconnect()
            except Exception as error:
                LOGGER.debug(
                    "runtime_disconnect_failed",
                    extra={
                        "connection_id": worker.connection.id,
                        "error_code": _error_code(error),
                    },
                )


def _enabled_groups_with_tags(
    config: CollectorConfig,
) -> dict[str, list[tuple[TagGroupConfig, tuple[TagConfig, ...]]]]:
    """Map Connection ids to their enabled Tag Groups that have enabled Tags."""
    groups: dict[str, list[tuple[TagGroupConfig, tuple[TagConfig, ...]]]] = {}
    for group in config.tag_groups:
        if not group.enabled:
            continue
        tags = tuple(
            tag for tag in config.tags if tag.tag_group_id == group.id and tag.enabled
        )
        if tags:
            groups.setdefault(group.connection_id, []).append((group, tags))
    return groups


def _mask_database_url(database_url: str) -> str:
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid database URL>"


def _error_code(error: BaseException) -> str:
    if isinstance(error, CollectorError) and error.code:
        return error.code
    return type(error).__name__
