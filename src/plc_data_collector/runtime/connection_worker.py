"""Isolated runtime worker owning one protocol connection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    PollExecution,
    PollStatus,
    RuntimeComponentStatus,
    TagConfig,
    TagGroupConfig,
    TagRequest,
    TagResult,
    TransientCommunicationError,
    WorkerState,
)
from plc_data_collector.protocols import CommunicationDriver, DriverRegistry
from plc_data_collector.runtime.retry import RetryPolicy, run_with_retry

LOGGER = logging.getLogger(__name__)
Sleeper = Callable[[float], Awaitable[None]]
RandomFloat = Callable[[], float]
CONNECTION_UNAVAILABLE_CODE = "connection_unavailable"
# Upper bound for the Poll Result summary error; Tag errors keep full detail.
POLL_ERROR_MESSAGE_MAX_LENGTH = 512
_TRUNCATION_MARKER = "..."
DEFAULT_CONNECT_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    initial_delay_s=0.1,
    max_delay_s=2.0,
    jitter_ratio=0.2,
)
DEFAULT_POLL_RETRY_POLICY = RetryPolicy(
    max_attempts=2,
    initial_delay_s=0.05,
    max_delay_s=1.0,
    jitter_ratio=0.2,
)


@dataclass(frozen=True, slots=True)
class WorkerPollResult:
    """Result of one worker-owned tag group poll."""

    execution: PollExecution
    results: tuple[TagResult, ...]


@dataclass(frozen=True, slots=True)
class WorkerRuntimeMetrics:
    """Observable connection worker counters."""

    component_id: str
    connect_attempts: int
    connect_successes: int
    connect_failures: int
    connect_retry_attempts: int
    poll_attempts: int
    poll_successes: int
    poll_failures: int
    poll_retry_attempts: int
    tag_successes: int
    tag_failures: int
    reconnect_attempts: int = 0
    last_poll_correlation_id: str | None = None
    last_reconnect_correlation_id: str | None = None
    last_error: str | None = None


class ConnectionWorker:
    """Runtime worker that owns one connection and one driver instance."""

    def __init__(
        self,
        connection: ConnectionConfig,
        registry: DriverRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
        connect_retry_policy: RetryPolicy = DEFAULT_CONNECT_RETRY_POLICY,
        poll_retry_policy: RetryPolicy = DEFAULT_POLL_RETRY_POLICY,
        retry_sleep: Sleeper | None = None,
        jitter: RandomFloat | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a worker and its single owned driver instance."""
        self.connection = connection
        self._driver = registry.create_driver(connection)
        self._clock = _utc_now if clock is None else clock
        self._connect_retry_policy = connect_retry_policy
        self._poll_retry_policy = poll_retry_policy
        self._retry_sleep = retry_sleep
        self._jitter = jitter
        self._logger = LOGGER if logger is None else logger
        self._state = WorkerState.STOPPED
        self._message: str | None = None
        self._error_code: str | None = None
        self._error_message: str | None = None
        self._heartbeat_at: datetime | None = None
        self._connect_attempts = 0
        self._connect_successes = 0
        self._connect_failures = 0
        self._connect_retry_attempts = 0
        self._poll_attempts = 0
        self._poll_successes = 0
        self._poll_failures = 0
        self._poll_retry_attempts = 0
        self._tag_successes = 0
        self._tag_failures = 0
        self._reconnect_attempts = 0
        self._reconnect_needed = False
        # Tag Groups poll concurrently; only one of them may reconnect at a time.
        self._reconnect_lock = asyncio.Lock()
        self._last_poll_correlation_id: str | None = None
        self._last_reconnect_correlation_id: str | None = None
        self._last_error: str | None = None

    @property
    def driver(self) -> CommunicationDriver:
        """Return the worker-owned driver instance for diagnostics and tests."""
        return self._driver

    def status(self) -> RuntimeComponentStatus:
        """Return observable worker state."""
        return RuntimeComponentStatus(
            component_id=self.component_id,
            state=self._state,
            updated_at=self._heartbeat_at or self._now(),
            message=self._message,
            error_code=self._error_code,
            error_message=self._error_message,
        )

    def metrics(self) -> WorkerRuntimeMetrics:
        """Return observable worker runtime metrics."""
        return WorkerRuntimeMetrics(
            component_id=self.component_id,
            connect_attempts=self._connect_attempts,
            connect_successes=self._connect_successes,
            connect_failures=self._connect_failures,
            connect_retry_attempts=self._connect_retry_attempts,
            poll_attempts=self._poll_attempts,
            poll_successes=self._poll_successes,
            poll_failures=self._poll_failures,
            poll_retry_attempts=self._poll_retry_attempts,
            tag_successes=self._tag_successes,
            tag_failures=self._tag_failures,
            reconnect_attempts=self._reconnect_attempts,
            last_poll_correlation_id=self._last_poll_correlation_id,
            last_reconnect_correlation_id=self._last_reconnect_correlation_id,
            last_error=self._last_error,
        )

    @property
    def component_id(self) -> str:
        """Return stable runtime component identifier."""
        return f"connection-worker:{self.connection.id}"

    async def connect(self) -> None:
        """Connect the owned driver and update worker state."""
        correlation_id = str(uuid4())
        self._last_reconnect_correlation_id = correlation_id
        if self._state in (WorkerState.FAILED, WorkerState.DEGRADED):
            # Reconnecting: keep the outage and its error visible until it works.
            self._message = "Reconnecting."
        else:
            self._set_state(WorkerState.STARTING, message="Connecting.")
        self._logger.info(
            "worker_connect_started",
            extra={
                "operation": "connect",
                "correlation_id": correlation_id,
                "component_id": self.component_id,
                "connection_id": self.connection.id,
                "protocol": self.connection.protocol,
            },
        )
        try:
            await run_with_retry(
                self._connect_once,
                self._connect_retry_policy,
                operation_name="connect",
                correlation_id=correlation_id,
                component_id=self.component_id,
                logger=self._logger,
                sleep=self._retry_sleep,
                random_float=self._jitter,
                on_retry=self._record_connect_retry,
            )
        except asyncio.CancelledError:
            await self._disconnect_after_cancellation()
            raise
        except Exception as error:
            self._connect_failures += 1
            self._record_failure(error, state=WorkerState.FAILED)
            self._logger.warning(
                "worker_connect_failed",
                extra={
                    "operation": "connect",
                    "correlation_id": correlation_id,
                    "component_id": self.component_id,
                    "connection_id": self.connection.id,
                    "protocol": self.connection.protocol,
                    "error_code": _error_code(error),
                },
            )
            raise
        self._connect_successes += 1
        self._set_state(WorkerState.RUNNING, message="Connected.")
        self._logger.info(
            "worker_connect_succeeded",
            extra={
                "operation": "connect",
                "correlation_id": correlation_id,
                "component_id": self.component_id,
                "connection_id": self.connection.id,
                "protocol": self.connection.protocol,
            },
        )

    async def disconnect(self) -> None:
        """Disconnect the owned driver and mark the worker stopped."""
        self._set_state(WorkerState.STOPPING, message="Disconnecting.")
        try:
            await self._driver.disconnect()
        except asyncio.CancelledError:
            await self._disconnect_after_cancellation()
            raise
        except Exception as error:
            self._record_failure(error, state=WorkerState.FAILED)
            raise
        self._set_state(WorkerState.STOPPED, message="Disconnected.")

    async def poll_group(
        self,
        group: TagGroupConfig,
        tags: tuple[TagConfig, ...],
    ) -> WorkerPollResult:
        """Poll a tag group through the owned driver."""
        self._validate_poll_inputs(group, tags)
        started_at = self._now()
        execution_id = str(uuid4())
        self._last_poll_correlation_id = execution_id
        self._poll_attempts += 1
        requests = tuple(
            _tag_request_from_config(tag, group.timeout_ms) for tag in tags
        )
        self._logger.info(
            "worker_poll_started",
            extra={
                "operation": "poll_group",
                "correlation_id": execution_id,
                "component_id": self.component_id,
                "connection_id": self.connection.id,
                "tag_group_id": group.id,
                "requested_tags": len(tags),
            },
        )

        if self._needs_reconnect() and not await self._reconnect_once(group):
            # The worker is still FAILED (or another Tag Group is reconnecting
            # right now); keep that state and report the Tags as unavailable.
            results = tuple(
                _unavailable_result(tag, self._last_error, self._now()) for tag in tags
            )
            return self._finish_poll(
                group, execution_id, started_at, len(tags), results, update_state=False
            )

        try:
            read_results, _ = await run_with_retry(
                lambda: self._driver.read(requests),
                self._poll_retry_policy,
                operation_name="poll_group",
                correlation_id=execution_id,
                component_id=self.component_id,
                logger=self._logger,
                sleep=self._retry_sleep,
                random_float=self._jitter,
                on_retry=self._record_poll_retry,
            )
            results = tuple(read_results)
            self._reconnect_needed = False
            if results and not any(result.is_success for result in results):
                # Drivers such as OPC UA report a dead link as per-Tag failures.
                self._reconnect_needed = not await self._is_healthy()
        except asyncio.CancelledError:
            await self._disconnect_after_cancellation()
            raise
        except Exception as error:
            self._reconnect_needed = isinstance(error, TransientCommunicationError)
            self._record_failure(error, state=WorkerState.DEGRADED)
            results = tuple(_failure_result(tag, error, self._now()) for tag in tags)

        return self._finish_poll(group, execution_id, started_at, len(tags), results)

    def _finish_poll(
        self,
        group: TagGroupConfig,
        execution_id: str,
        started_at: datetime,
        requested_tags: int,
        results: tuple[TagResult, ...],
        *,
        update_state: bool = True,
    ) -> WorkerPollResult:
        finished_at = self._now()
        execution = PollExecution(
            execution_id=execution_id,
            connection_id=self.connection.id,
            tag_group_id=group.id,
            status=_poll_status(results),
            started_at=started_at,
            finished_at=finished_at,
            requested_tags=requested_tags,
            successful_tags=sum(result.is_success for result in results),
            failed_tags=sum(not result.is_success for result in results),
            error_message=_poll_error_message(results),
        )
        self._record_poll_metrics(execution)
        if not update_state:
            pass
        elif execution.status is PollStatus.SUCCESS:
            self._set_state(WorkerState.RUNNING, message="Poll succeeded.")
        elif execution.status is PollStatus.PARTIAL_FAILURE:
            self._set_state(WorkerState.DEGRADED, message="Poll partially failed.")
        else:
            self._set_state(WorkerState.DEGRADED, message="Poll failed.")
        self._logger.info(
            "worker_poll_finished",
            extra={
                "operation": "poll_group",
                "correlation_id": execution_id,
                "component_id": self.component_id,
                "connection_id": self.connection.id,
                "tag_group_id": group.id,
                "poll_status": execution.status.value,
                "successful_tags": execution.successful_tags,
                "failed_tags": execution.failed_tags,
            },
        )
        return WorkerPollResult(execution=execution, results=results)

    async def _is_healthy(self) -> bool:
        try:
            return await self._driver.health_check()
        except asyncio.CancelledError:
            await self._disconnect_after_cancellation()
            raise
        except Exception:
            return False

    def _needs_reconnect(self) -> bool:
        return self._reconnect_needed or self._state in (
            WorkerState.FAILED,
            WorkerState.STOPPED,
        )

    async def _reconnect_once(self, group: TagGroupConfig) -> bool:
        """Reconnect unless another Tag Group is already doing it.

        A Tag Group that finds a reconnect in progress skips this cycle instead
        of waiting, so it never opens a second session or closes the new one.
        """
        if self._reconnect_lock.locked():
            self._logger.debug(
                "worker_reconnect_in_progress",
                extra={
                    "operation": "reconnect",
                    "correlation_id": self._last_poll_correlation_id,
                    "component_id": self.component_id,
                    "connection_id": self.connection.id,
                    "tag_group_id": group.id,
                },
            )
            return False
        async with self._reconnect_lock:
            if not self._needs_reconnect():
                return True
            return await self._reconnect(group)

    async def _reconnect(self, group: TagGroupConfig) -> bool:
        """Best-effort disconnect, then connect again; return whether it worked."""
        self._reconnect_attempts += 1
        extra = {
            "operation": "reconnect",
            "correlation_id": self._last_poll_correlation_id,
            "component_id": self.component_id,
            "connection_id": self.connection.id,
            "protocol": self.connection.protocol,
            "tag_group_id": group.id,
        }
        self._logger.info("worker_reconnect_started", extra=extra)
        try:
            await self._driver.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.debug("worker_reconnect_disconnect_ignored", extra=extra)
        try:
            await self.connect()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._logger.warning(
                "worker_reconnect_failed",
                extra={**extra, "error_code": _error_code(error)},
            )
            return False
        self._reconnect_needed = False
        self._logger.info("worker_reconnect_succeeded", extra=extra)
        return True

    async def health_check(self) -> RuntimeComponentStatus:
        """Run driver health check and return updated worker status."""
        try:
            healthy = await self._driver.health_check()
        except asyncio.CancelledError:
            await self._disconnect_after_cancellation()
            raise
        except Exception as error:
            self._record_failure(error, state=WorkerState.DEGRADED)
            return self.status()

        if healthy:
            self._set_state(WorkerState.RUNNING, message="Healthy.")
        else:
            self._set_state(WorkerState.DEGRADED, message="Health check failed.")
        return self.status()

    def heartbeat(self) -> RuntimeComponentStatus:
        """Refresh heartbeat timestamp and return current status."""
        self._heartbeat_at = self._now()
        return self.status()

    def _validate_poll_inputs(
        self,
        group: TagGroupConfig,
        tags: tuple[TagConfig, ...],
    ) -> None:
        if group.connection_id != self.connection.id:
            raise ConfigurationError(
                "Tag group belongs to a different connection.",
                code="worker_group_connection_mismatch",
                details={
                    "connection_id": self.connection.id,
                    "tag_group_id": group.id,
                    "tag_group_connection_id": group.connection_id,
                },
            )
        invalid_tags = [tag.id for tag in tags if tag.tag_group_id != group.id]
        if invalid_tags:
            raise ConfigurationError(
                "Tags must belong to the polled tag group.",
                code="worker_tag_group_mismatch",
                details={"tag_group_id": group.id, "tag_ids": invalid_tags},
            )

    async def _disconnect_after_cancellation(self) -> None:
        self._set_state(WorkerState.STOPPING, message="Cancellation requested.")
        try:
            await self._driver.disconnect()
        except Exception as error:
            self._record_failure(error, state=WorkerState.FAILED)
        else:
            self._set_state(WorkerState.STOPPED, message="Cancelled.")

    def _record_failure(self, error: Exception, *, state: WorkerState) -> None:
        self._last_error = str(error) or error.__class__.__name__
        self._set_state(
            state,
            message="Worker operation failed.",
            error_code=_error_code(error),
            error_message=str(error),
        )

    async def _connect_once(self) -> None:
        self._connect_attempts += 1
        await self._driver.connect()

    def _record_connect_retry(
        self,
        _error: Exception,
        _delay_s: float,
        _retry_number: int,
    ) -> None:
        self._connect_retry_attempts += 1

    def _record_poll_retry(
        self,
        _error: Exception,
        _delay_s: float,
        _retry_number: int,
    ) -> None:
        self._poll_retry_attempts += 1

    def _record_poll_metrics(self, execution: PollExecution) -> None:
        if execution.status is PollStatus.SUCCESS:
            self._poll_successes += 1
        else:
            self._poll_failures += 1
        self._tag_successes += execution.successful_tags
        self._tag_failures += execution.failed_tags
        self._last_error = execution.error_message

    def _set_state(
        self,
        state: WorkerState,
        *,
        message: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._state = state
        self._message = message
        self._error_code = error_code
        self._error_message = error_message
        self._heartbeat_at = self._now()

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)


def _tag_request_from_config(tag: TagConfig, timeout_ms: int) -> TagRequest:
    return TagRequest(
        tag_id=tag.id,
        address=tag.address,
        value_type=tag.value_type,
        timeout_ms=timeout_ms,
    )


def _failure_result(
    tag: TagConfig,
    error: Exception,
    received_at: datetime,
) -> TagResult:
    return TagResult.failure(
        tag_id=tag.id,
        error_code=_error_code(error),
        error_message=str(error) or error.__class__.__name__,
        received_at=received_at,
    )


def _unavailable_result(
    tag: TagConfig,
    last_error: str | None,
    received_at: datetime,
) -> TagResult:
    return TagResult.failure(
        tag_id=tag.id,
        error_code=CONNECTION_UNAVAILABLE_CODE,
        error_message=last_error or "Connection is unavailable.",
        received_at=received_at,
    )


def _poll_status(results: tuple[TagResult, ...]) -> PollStatus:
    if not results:
        return PollStatus.SUCCESS
    failed = sum(not result.is_success for result in results)
    if failed == 0:
        return PollStatus.SUCCESS
    if failed == len(results):
        return PollStatus.FAILED
    return PollStatus.PARTIAL_FAILURE


def _poll_error_message(results: tuple[TagResult, ...]) -> str | None:
    # Distinct messages only: a fully failed group repeats one error per Tag.
    errors = dict.fromkeys(
        result.error_message for result in results if result.error_message
    )
    if not errors:
        return None
    message = "; ".join(errors)
    if len(message) <= POLL_ERROR_MESSAGE_MAX_LENGTH:
        return message
    keep = POLL_ERROR_MESSAGE_MAX_LENGTH - len(_TRUNCATION_MARKER)
    return message[:keep] + _TRUNCATION_MARKER


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return error.__class__.__name__


def _utc_now() -> datetime:
    return datetime.now(UTC)
