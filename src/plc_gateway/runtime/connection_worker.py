"""Isolated runtime worker owning one protocol connection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from plc_gateway.domain import (
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
from plc_gateway.protocols import CommunicationDriver, DriverRegistry


@dataclass(frozen=True, slots=True)
class WorkerPollResult:
    """Result of one worker-owned tag group poll."""

    execution: PollExecution
    results: tuple[TagResult, ...]


class ConnectionWorker:
    """Runtime worker that owns one connection and one driver instance."""

    def __init__(
        self,
        connection: ConnectionConfig,
        registry: DriverRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a worker and its single owned driver instance."""
        self.connection = connection
        self._driver = registry.create_driver(connection)
        self._clock = _utc_now if clock is None else clock
        self._state = WorkerState.STOPPED
        self._message: str | None = None
        self._error_code: str | None = None
        self._error_message: str | None = None
        self._heartbeat_at: datetime | None = None

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

    @property
    def component_id(self) -> str:
        """Return stable runtime component identifier."""
        return f"connection-worker:{self.connection.id}"

    async def connect(self) -> None:
        """Connect the owned driver and update worker state."""
        self._set_state(WorkerState.STARTING, message="Connecting.")
        try:
            await self._driver.connect()
        except asyncio.CancelledError:
            await self._disconnect_after_cancellation()
            raise
        except Exception as error:
            self._record_failure(error, state=WorkerState.FAILED)
            raise
        self._set_state(WorkerState.RUNNING, message="Connected.")

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
        requests = tuple(
            _tag_request_from_config(tag, group.timeout_ms) for tag in tags
        )

        try:
            results = tuple(await self._driver.read(requests))
        except asyncio.CancelledError:
            await self._disconnect_after_cancellation()
            raise
        except Exception as error:
            self._record_failure(error, state=WorkerState.DEGRADED)
            results = tuple(_failure_result(tag, error, self._now()) for tag in tags)

        finished_at = self._now()
        execution = PollExecution(
            execution_id=execution_id,
            connection_id=self.connection.id,
            tag_group_id=group.id,
            status=_poll_status(results),
            started_at=started_at,
            finished_at=finished_at,
            requested_tags=len(tags),
            successful_tags=sum(result.is_success for result in results),
            failed_tags=sum(not result.is_success for result in results),
            error_message=_poll_error_message(results),
        )
        if execution.status is PollStatus.SUCCESS:
            self._set_state(WorkerState.RUNNING, message="Poll succeeded.")
        elif execution.status is PollStatus.PARTIAL_FAILURE:
            self._set_state(WorkerState.DEGRADED, message="Poll partially failed.")
        else:
            self._set_state(WorkerState.DEGRADED, message="Poll failed.")
        return WorkerPollResult(execution=execution, results=results)

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
        self._set_state(
            state,
            message="Worker operation failed.",
            error_code=_error_code(error),
            error_message=str(error),
        )

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
    errors = [result.error_message for result in results if result.error_message]
    if not errors:
        return None
    return "; ".join(errors)


def _error_code(error: Exception) -> str:
    if isinstance(error, TransientCommunicationError) and error.code is not None:
        return error.code
    return error.__class__.__name__


def _utc_now() -> datetime:
    return datetime.now(UTC)
