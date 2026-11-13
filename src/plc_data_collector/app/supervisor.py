"""Supervisor that owns the database and a replaceable collector runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from plc_data_collector.api.state import RuntimeApiState
from plc_data_collector.app.config import CollectorConfig, load_collector_config
from plc_data_collector.app.config_transfer import keep_stored_values
from plc_data_collector.app.retention import RetentionJob
from plc_data_collector.app.runtime import CollectorRuntime, error_code
from plc_data_collector.app.secrets import SecretBox
from plc_data_collector.app.settings import RuntimeSettings
from plc_data_collector.app.stores import Stores, build_stores
from plc_data_collector.domain import (
    CollectorError,
    ConfigurationError,
    StorageError,
)
from plc_data_collector.persistence import (
    ConfigStore,
    create_database_engine,
    run_migrations,
    wait_for_database,
)
from plc_data_collector.protocols import DriverRegistry, default_driver_registry
from plc_data_collector.runtime.live_values import LiveValueCache

LOGGER = logging.getLogger(__name__)
DATABASE_PROBE_INTERVAL_S = 1.0
RuntimeFactory = Callable[..., CollectorRuntime]
RetentionJobFactory = Callable[[Stores], RetentionJob]


class RuntimeSupervisor:
    """Own the database and run a ``CollectorRuntime`` that "Apply changes" replaces.

    ``start()`` waits for the database, migrates it, encrypts legacy plaintext
    secrets, seeds the configuration once from the JSON file, loads the active
    configuration and starts the runtime. ``apply()`` stops the runtime (its
    Reading Queue drains into the database or the Spool) and starts a new one
    from the database, while the API keeps serving. The engine and the live
    value cache outlive every runtime, and so does the retention job, which
    starts once the database is prepared and stops before the engine is
    disposed.
    """

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        registry: DriverRegistry | None = None,
        runtime_factory: RuntimeFactory = CollectorRuntime,
        retention_job_factory: RetentionJobFactory = RetentionJob,
    ) -> None:
        """Create a supervisor; nothing touches the database until ``start()``."""
        self._settings = settings
        self._registry = registry or default_driver_registry()
        self._runtime_factory = runtime_factory
        self._retention_job_factory = retention_job_factory
        self._retention_job: RetentionJob | None = None
        self._retention_task: asyncio.Task[None] | None = None
        self._engine: Engine | None = None
        self._stores: Stores | None = None
        self._live_values = LiveValueCache()
        self._runtime: CollectorRuntime | None = None
        self._running_revision: int | None = None
        self._running_config: CollectorConfig | None = None
        self._last_error_code: str | None = None
        self._apply_lock = asyncio.Lock()
        self._apply_task: asyncio.Task[None] | None = None
        self._applying = False
        # Lifecycle: idle -> starting -> running -> stopping -> stopped.
        self._phase = "idle"
        self._stop_requested = False
        self._startup_task: asyncio.Task[None] | None = None
        self._start_finished = asyncio.Event()
        self._stopped = asyncio.Event()

    @property
    def stores(self) -> Stores | None:
        """Return the database stores, or None before the secret key is loaded."""
        return self._stores

    @property
    def live_values(self) -> LiveValueCache:
        """Return the live value cache; the same object across applies."""
        return self._live_values

    @property
    def running_revision(self) -> int | None:
        """Return the configuration revision the runtime runs, or None."""
        return self._running_revision

    @property
    def running_config(self) -> CollectorConfig | None:
        """Return the configuration the runtime runs, or None."""
        return self._running_config

    @property
    def last_error_code(self) -> str | None:
        """Return the code of the last failed start or apply, if any."""
        return self._last_error_code

    @property
    def applying(self) -> bool:
        """Return whether an apply is in progress."""
        return self._applying

    @property
    def retention_task(self) -> asyncio.Task[None] | None:
        """Return the running retention task, or None before start or after stop."""
        return self._retention_task

    def trigger_retention(self) -> None:
        """Run retention now instead of at the next interval, if it is running.

        Call it on the event loop thread; it returns at once.
        """
        if self._retention_job is not None:
            self._retention_job.trigger()

    @property
    def registry(self) -> DriverRegistry:
        """Return the protocol driver registry the runtimes use."""
        return self._registry

    async def start(self) -> None:
        """Prepare the database and configuration, then start the runtime.

        A failure leaves nothing running: the runtime and the engine are
        released before the error propagates. A stop() during start() aborts
        it, and start() then raises ``CollectorError(code="runtime_start_aborted")``.
        """
        if self._phase != "idle":
            raise RuntimeError("RuntimeSupervisor can only be started once.")
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
            self._startup(), name="supervisor-startup"
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
                self._last_error_code = error_code(error)
                LOGGER.error(
                    "supervisor_start_failed",
                    extra={
                        "error_code": error_code(error),
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
        LOGGER.info("supervisor_started", extra={"revision": self._running_revision})

    async def apply(self) -> None:
        """Replace the runtime with one built from the database configuration.

        The current runtime stops first, so its pending Poll Results are stored
        (or spooled) before the new runtime starts. When loading or starting
        the new configuration fails, a runtime for the previous configuration
        starts again with its previous revision, ``last_error_code`` keeps the
        failure's code and the error propagates; a later apply() retries. Only
        when that fallback fails too is collection left stopped, and readiness
        then reports the error code.
        The swap runs in its own task, so a cancelled caller cannot leave it
        half done.
        """
        if self._apply_lock.locked():
            raise CollectorError(
                "Changes are already being applied.", code="apply_in_progress"
            )
        if self._phase != "running":
            raise CollectorError(
                "The collector is not running, so changes cannot be applied now.",
                code="runtime_not_running",
            )
        # Uncontended, acquire() returns without yielding, so the check above
        # and taking the lock cannot interleave with another apply().
        await self._apply_lock.acquire()
        self._applying = True
        task = asyncio.create_task(self._apply(), name="runtime-apply")
        self._apply_task = task
        # The caller sees the error; this keeps a cancelled caller from leaving
        # an "exception was never retrieved" warning behind.
        task.add_done_callback(_consume_result)
        await asyncio.shield(task)

    async def _apply(self) -> None:
        try:
            LOGGER.info("runtime_apply_started")
            runtime, self._runtime = self._runtime, None
            previous_config = self._running_config
            previous_revision = self._running_revision
            self._running_revision = None
            self._running_config = None
            try:
                if runtime is not None:
                    await runtime.stop(timeout_s=self._settings.storage_timeout_s * 2)
                if self._stop_requested:
                    # stop() arrived while the old runtime drained: starting a
                    # new runtime now would only connect it to drain it again.
                    LOGGER.info("runtime_apply_aborted")
                    return
                await self._start_runtime()
            except Exception as error:
                self._running_revision = None
                if self._stop_requested:
                    # stop() aborted the new runtime's start; not a failure.
                    LOGGER.info("runtime_apply_aborted")
                    return
                self._last_error_code = error_code(error)
                LOGGER.error(
                    "runtime_apply_failed",
                    extra={
                        "error_code": error_code(error),
                        "error_type": type(error).__name__,
                    },
                )
                if previous_config is not None and previous_revision is not None:
                    await self._roll_back(previous_config, previous_revision)
                raise
            self._last_error_code = None
            LOGGER.info("runtime_applied", extra={"revision": self._running_revision})
        finally:
            self._applying = False
            self._apply_task = None
            self._apply_lock.release()

    async def _roll_back(self, config: CollectorConfig, revision: int) -> None:
        """Start a runtime for the configuration that ran before a failed apply.

        ``last_error_code`` keeps the apply's code, so the Panel can say the
        changes were not applied. When this start fails too, no runtime runs
        and readiness reports the apply's error code.
        """
        if self._stop_requested:
            return
        try:
            await self._start_runtime((config, revision))
        except Exception as error:
            LOGGER.error(
                "runtime_rollback_failed",
                extra={
                    "error_code": error_code(error),
                    "error_type": type(error).__name__,
                },
            )
            return
        LOGGER.warning(
            "runtime_apply_rolled_back",
            extra={"revision": revision, "error_code": self._last_error_code},
        )

    async def stop(self, timeout_s: float = 10.0) -> None:
        """Stop the runtime, then dispose the database engine.

        ``timeout_s`` bounds the runtime's drain. An apply in progress finishes
        first, without starting a new runtime if it has not started one yet;
        a runtime it is starting is stopped, which aborts its start (connect
        retries included), so shutdown does not wait for it.
        A stop() during start() aborts the start. Calling stop() again, or
        before start(), does nothing.
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
            self._phase = "stopping"
            starting = self._runtime if self._applying else None
            if starting is not None:
                await starting.stop(timeout_s=timeout_s)
            async with self._apply_lock:
                await self._release(timeout_s=timeout_s)
        elif self._phase == "stopping":
            await self._stopped.wait()

    def api_state(self) -> RuntimeApiState:
        """Return a fresh snapshot for the API."""
        if self._applying:
            return RuntimeApiState(
                configuration_loaded=False,
                critical_errors=("applying_changes",),
            )
        runtime = self._runtime
        if runtime is None:
            return RuntimeApiState(
                configuration_loaded=False,
                storage_available=False,
                critical_errors=(
                    () if self._last_error_code is None else (self._last_error_code,)
                ),
            )
        return runtime.api_state()

    async def _startup(self) -> None:
        settings = self._settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._engine = create_database_engine(settings.database_url)
        await self._wait_for_database(self._engine)
        await self._migrate()
        secret_box = await asyncio.to_thread(SecretBox.from_settings, settings)
        self._stores = build_stores(self._engine, secret_box)
        await asyncio.to_thread(self._prepare_configuration, self._stores.config)
        self._start_retention(self._stores)
        await self._start_runtime()

    def _start_retention(self, stores: Stores) -> None:
        """Start the retention job; it keeps running across applies."""
        job = self._retention_job_factory(stores)
        self._retention_job = job
        self._retention_task = asyncio.create_task(job.run(), name="retention")

    async def _stop_retention(self) -> None:
        """Cancel the retention job and wait until its current batch ends."""
        task, self._retention_task = self._retention_task, None
        self._retention_job = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception as error:
            LOGGER.error(
                "retention_stop_failed",
                extra={
                    "error_code": error_code(error),
                    "error_type": type(error).__name__,
                },
            )

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

    async def _migrate(self) -> None:
        try:
            await asyncio.to_thread(run_migrations, self._settings.database_url)
        except Exception as error:
            # Alembic and SQLAlchemy errors may quote the database URL, so only
            # the error type leaves this block.
            raise StorageError(
                "Database migration failed.",
                code="database_migration_failed",
                details={"error_type": type(error).__name__},
            ) from None

    def _prepare_configuration(self, store: ConfigStore) -> None:
        """Encrypt legacy secrets and seed from the file once; runs in a thread.

        A 0.2.0 database (Connection rows but no revision) is reconciled with
        the file once instead of being seeded. Credentials in OPC UA endpoint
        URLs then move into the user name and encrypted password options.
        """
        encrypted = store.encrypt_plaintext_secrets()
        if encrypted:
            LOGGER.info(
                "configuration_secrets_encrypted", extra={"connections": encrypted}
            )
        self._seed_or_reconcile(store)
        # After seeding: a file can carry credentials in an endpoint URL too.
        moved, left = store.move_endpoint_credentials()
        if moved:
            LOGGER.info(
                "configuration_endpoint_credentials_moved",
                extra={"connections": len(moved)},
            )
        for connection_id in left:
            LOGGER.warning(
                "endpoint_credentials_left_in_url",
                extra={"connection_id": connection_id},
            )

    def _seed_or_reconcile(self, store: ConfigStore) -> None:
        config_file = self._settings.config_file
        if store.has_any_connection_rows() and not store.has_revision():
            _reconcile_upgrade(store, config_file)
            return
        if config_file is None:
            return
        # Archived rows count: a database that ever had configuration is never
        # re-seeded, so the database stays the source of truth.
        if store.has_any_connection_rows():
            LOGGER.info(
                "configuration_seed_skipped", extra={"config_file": str(config_file)}
            )
            return
        config = load_collector_config(config_file)
        store.replace_all(config)
        LOGGER.info(
            "configuration_seeded",
            extra={
                "config_file": str(config_file),
                "connections": len(config.connections),
                "tag_groups": len(config.tag_groups),
                "tags": len(config.tags),
            },
        )

    async def _start_runtime(
        self, previous: tuple[CollectorConfig, int] | None = None
    ) -> None:
        """Start a runtime for the active configuration, or for ``previous``.

        Without ``previous`` the active configuration and its revision are
        loaded from the database; with it (a rollback) nothing is read.
        """
        if self._engine is None or self._stores is None:
            raise RuntimeError("The database is not prepared.")
        if previous is None:
            config, revision = await asyncio.to_thread(self._stores.config.load_active)
        else:
            config, revision = previous
        if not config.connections:
            LOGGER.warning("configuration_empty")
        runtime = self._runtime_factory(
            self._settings,
            self._engine,
            config,
            registry=self._registry,
            live_values=self._live_values,
        )
        self._runtime = runtime
        if self._applying and self._stop_requested:
            # stop() arrived while the configuration loaded: do not connect.
            self._runtime = None
            raise CollectorError(
                "Runtime start was aborted by stop().", code="runtime_start_aborted"
            )
        try:
            await runtime.start()
        except BaseException:
            self._runtime = None
            raise
        self._running_revision = revision
        self._running_config = config
        self._live_values.retain(
            {tag.id for tag in config.tags},
            {group.id for group in config.tag_groups},
        )

    async def _release(self, *, timeout_s: float) -> None:
        """Stop the runtime and dispose the engine; used by stop() and failed starts."""
        self._phase = "stopping"
        try:
            try:
                runtime, self._runtime = self._runtime, None
                if runtime is not None:
                    await runtime.stop(timeout_s=timeout_s)
            finally:
                # Before the engine is disposed: a delete in progress finishes
                # its current batch first.
                await self._stop_retention()
        finally:
            self._running_revision = None
            self._running_config = None
            self._stores = None
            if self._engine is not None:
                self._engine.dispose()
            self._phase = "stopped"
            self._stopped.set()


def _reconcile_upgrade(store: ConfigStore, config_file: Path | None) -> None:
    """Make the file the active configuration of a database 0.2.0 wrote.

    0.2.0 upserted the file on every start and never removed rows, so items
    deleted from the file stayed in the database. Running ``replace_all`` once
    archives them (their history is kept) and keeps stored secrets the file
    leaves out. Without a readable file nothing changes and a warning says so.
    Runs after ``encrypt_plaintext_secrets``; ``load_active`` decrypts.
    """
    current, _ = store.load_active()
    active = _counts(current)
    if config_file is None:
        LOGGER.warning(
            "configuration_upgrade_unreconciled",
            extra={"reason": "no_config_file", **active},
        )
        return
    try:
        config = keep_stored_values(load_collector_config(config_file), current=current)
        store.replace_all(config)
    except ConfigurationError as error:
        LOGGER.warning(
            "configuration_upgrade_unreconciled",
            extra={"reason": error.code, **active},
        )
        return
    archived = {
        f"archived_{kind}": len(
            {item.id for item in getattr(current, kind)}
            - {item.id for item in getattr(config, kind)}
        )
        for kind in ("connections", "tag_groups", "tags")
    }
    LOGGER.info(
        "configuration_upgrade_reconciled",
        extra={"config_file": str(config_file), **_counts(config), **archived},
    )


def _counts(config: CollectorConfig) -> dict[str, int]:
    return {
        "connections": len(config.connections),
        "tag_groups": len(config.tag_groups),
        "tags": len(config.tags),
    }


def _mask_database_url(database_url: str) -> str:
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid database URL>"


def _consume_result(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()
