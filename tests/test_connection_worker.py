from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from plc_gateway.domain import (
    ConnectionConfig,
    PollStatus,
    TagConfig,
    TagGroupConfig,
    TagRequest,
    TagResult,
    TagValue,
    TransientCommunicationError,
    ValueType,
    WorkerState,
)
from plc_gateway.protocols import DriverCapabilities, DriverRegistry
from plc_gateway.runtime import ConnectionWorker


class FixedClock:
    """Deterministic UTC clock for worker tests."""

    def __init__(self) -> None:
        """Create a fixed clock."""
        self.value = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        """Return the current clock value."""
        return self.value


class FakeWorkerDriver:
    """Configurable fake driver for connection worker tests."""

    instances: ClassVar[list[FakeWorkerDriver]] = []

    def __init__(self, connection: ConnectionConfig) -> None:
        """Create a fake driver for one connection."""
        self.connection = connection
        self.connected = False
        self.disconnect_calls = 0
        connect_failures = connection.protocol_options.get("connect_failures", 0)
        assert isinstance(connect_failures, int)
        self.connect_failures_remaining = connect_failures
        self.read_failure = bool(connection.protocol_options.get("read_failure", False))
        self.cancel_on_read = bool(
            connection.protocol_options.get("cancel_on_read", False)
        )
        FakeWorkerDriver.instances.append(self)

    @property
    def capabilities(self) -> DriverCapabilities:
        """Return fake capabilities."""
        return DriverCapabilities(protocol=self.connection.protocol)

    async def connect(self) -> None:
        """Connect or raise a configured transient failure."""
        if self.connect_failures_remaining > 0:
            self.connect_failures_remaining -= 1
            raise TransientCommunicationError(
                "connect failed",
                code="fake_connect_failed",
            )
        self.connected = True

    async def disconnect(self) -> None:
        """Disconnect the fake driver."""
        self.disconnect_calls += 1
        self.connected = False

    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]:
        """Return configured fake tag results."""
        if self.cancel_on_read:
            raise asyncio.CancelledError
        if self.read_failure:
            raise TransientCommunicationError("read failed", code="fake_read_failed")

        received_at = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
        results: list[TagResult] = []
        for tag in tags:
            if tag.tag_id == "broken":
                results.append(
                    TagResult.failure(
                        tag_id=tag.tag_id,
                        error_code="fake_tag_failed",
                        error_message="tag failed",
                        received_at=received_at,
                    )
                )
            else:
                results.append(
                    TagResult.success(
                        tag_id=tag.tag_id,
                        value=TagValue.numeric(1.0),
                        source_timestamp=received_at,
                        received_at=received_at,
                    )
                )
        return results

    async def health_check(self) -> bool:
        """Return connection health."""
        return self.connected


def make_registry() -> DriverRegistry:
    registry = DriverRegistry()
    registry.register("fake", FakeWorkerDriver)
    return registry


def make_connection(
    connection_id: str = "fake_connection",
    protocol_options: dict[str, object] | None = None,
) -> ConnectionConfig:
    return ConnectionConfig(
        id=connection_id,
        protocol="fake",
        endpoint="fake://local",
        timeout_ms=1000,
        protocol_options=protocol_options or {},
    )


def make_group(connection_id: str = "fake_connection") -> TagGroupConfig:
    return TagGroupConfig(
        id="fast",
        connection_id=connection_id,
        interval_ms=1000,
        timeout_ms=500,
    )


def make_tag(tag_id: str = "temperature") -> TagConfig:
    return TagConfig(
        id=tag_id,
        tag_group_id="fast",
        name=tag_id.title(),
        address=f"fake://{tag_id}",
        value_type=ValueType.NUMERIC,
    )


@pytest.fixture(autouse=True)
def clear_fake_driver_instances() -> None:
    FakeWorkerDriver.instances.clear()


@pytest.mark.asyncio
async def test_worker_owns_single_driver_instance() -> None:
    worker = ConnectionWorker(make_connection(), make_registry(), clock=FixedClock())

    assert len(FakeWorkerDriver.instances) == 1
    assert worker.driver is FakeWorkerDriver.instances[0]

    await worker.connect()
    await worker.disconnect()

    assert len(FakeWorkerDriver.instances) == 1
    assert worker.status().state is WorkerState.STOPPED


@pytest.mark.asyncio
async def test_worker_poll_group_returns_successful_execution() -> None:
    worker = ConnectionWorker(make_connection(), make_registry(), clock=FixedClock())
    await worker.connect()

    result = await worker.poll_group(make_group(), (make_tag(),))

    assert result.execution.status is PollStatus.SUCCESS
    assert result.execution.requested_tags == 1
    assert result.execution.successful_tags == 1
    assert result.results[0].is_success is True
    assert worker.status().state is WorkerState.RUNNING


@pytest.mark.asyncio
async def test_worker_poll_group_preserves_partial_tag_failures() -> None:
    worker = ConnectionWorker(make_connection(), make_registry(), clock=FixedClock())
    await worker.connect()

    result = await worker.poll_group(make_group(), (make_tag(), make_tag("broken")))

    assert result.execution.status is PollStatus.PARTIAL_FAILURE
    assert result.execution.successful_tags == 1
    assert result.execution.failed_tags == 1
    assert result.results[1].error_code == "fake_tag_failed"
    assert worker.status().state is WorkerState.DEGRADED


@pytest.mark.asyncio
async def test_worker_isolates_read_failure_as_failed_poll_results() -> None:
    worker = ConnectionWorker(
        make_connection(protocol_options={"read_failure": True}),
        make_registry(),
        clock=FixedClock(),
    )
    await worker.connect()

    result = await worker.poll_group(make_group(), (make_tag(), make_tag("pressure")))

    assert result.execution.status is PollStatus.FAILED
    assert result.execution.failed_tags == 2
    assert {tag_result.error_code for tag_result in result.results} == {
        "fake_read_failed"
    }
    assert worker.status().state is WorkerState.DEGRADED


@pytest.mark.asyncio
async def test_worker_failure_does_not_stop_other_worker() -> None:
    failing_worker = ConnectionWorker(
        make_connection("failing", {"read_failure": True}),
        make_registry(),
        clock=FixedClock(),
    )
    healthy_worker = ConnectionWorker(
        make_connection("healthy"),
        make_registry(),
        clock=FixedClock(),
    )
    await failing_worker.connect()
    await healthy_worker.connect()

    failed = await failing_worker.poll_group(make_group("failing"), (make_tag(),))
    healthy = await healthy_worker.poll_group(make_group("healthy"), (make_tag(),))

    assert failed.execution.status is PollStatus.FAILED
    assert healthy.execution.status is PollStatus.SUCCESS
    assert healthy_worker.status().state is WorkerState.RUNNING


@pytest.mark.asyncio
async def test_worker_connect_lifecycle_is_reconnect_ready() -> None:
    worker = ConnectionWorker(
        make_connection(protocol_options={"connect_failures": 1}),
        make_registry(),
        clock=FixedClock(),
    )

    with pytest.raises(TransientCommunicationError, match="connect failed"):
        await worker.connect()
    assert worker.status().state is WorkerState.FAILED

    await worker.connect()
    assert worker.status().state is WorkerState.RUNNING


@pytest.mark.asyncio
async def test_worker_health_check_updates_observable_state() -> None:
    worker = ConnectionWorker(make_connection(), make_registry(), clock=FixedClock())

    degraded = await worker.health_check()
    assert degraded.state is WorkerState.DEGRADED

    await worker.connect()
    healthy = await worker.health_check()
    assert healthy.state is WorkerState.RUNNING
    assert worker.heartbeat().updated_at == FixedClock()()


@pytest.mark.asyncio
async def test_worker_cancellation_disconnects_driver() -> None:
    worker = ConnectionWorker(
        make_connection(protocol_options={"cancel_on_read": True}),
        make_registry(),
        clock=FixedClock(),
    )
    await worker.connect()
    driver = worker.driver

    with pytest.raises(asyncio.CancelledError):
        await worker.poll_group(make_group(), (make_tag(),))

    assert isinstance(driver, FakeWorkerDriver)
    assert driver.connected is False
    assert driver.disconnect_calls == 1
    assert worker.status().state is WorkerState.STOPPED
