from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    PermanentProtocolError,
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
from plc_data_collector.protocols import DriverCapabilities, DriverRegistry
from plc_data_collector.runtime import ConnectionWorker
from plc_data_collector.runtime.connection_worker import (
    POLL_ERROR_MESSAGE_MAX_LENGTH,
    _poll_error_message,
)


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
        self.connect_configuration_failure = bool(
            connection.protocol_options.get("connect_configuration_failure", False)
        )
        self.connect_permanent_failure = bool(
            connection.protocol_options.get("connect_permanent_failure", False)
        )
        read_failures = connection.protocol_options.get("read_failures", 0)
        assert isinstance(read_failures, int)
        self.read_failures_remaining = read_failures
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
        if self.connect_configuration_failure:
            raise ConfigurationError(
                "invalid connection",
                code="fake_invalid_connection",
            )
        if self.connect_permanent_failure:
            raise PermanentProtocolError(
                "permanent connect failure",
                code="fake_permanent_connect_failure",
            )
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
        if self.read_failures_remaining > 0:
            self.read_failures_remaining -= 1
            raise TransientCommunicationError("read failed", code="fake_read_failed")
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


def make_tag(tag_id: str = "temperature", tag_group_id: str = "fast") -> TagConfig:
    return TagConfig(
        id=tag_id,
        tag_group_id=tag_group_id,
        name=tag_id.title(),
        address=f"fake://{tag_id}",
        value_type=ValueType.NUMERIC,
    )


async def no_sleep(_: float) -> None:
    """Deterministic retry sleeper."""


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
        retry_sleep=no_sleep,
        jitter=lambda: 0.5,
    )

    await worker.connect()
    assert worker.status().state is WorkerState.RUNNING
    metrics = worker.metrics()
    assert metrics.connect_attempts == 2
    assert metrics.connect_retry_attempts == 1
    assert metrics.connect_successes == 1


@pytest.mark.asyncio
async def test_worker_does_not_retry_configuration_connect_errors() -> None:
    worker = ConnectionWorker(
        make_connection(protocol_options={"connect_configuration_failure": True}),
        make_registry(),
        clock=FixedClock(),
        retry_sleep=no_sleep,
    )

    with pytest.raises(ConfigurationError, match="invalid connection"):
        await worker.connect()

    metrics = worker.metrics()
    assert worker.status().state is WorkerState.FAILED
    assert metrics.connect_attempts == 1
    assert metrics.connect_retry_attempts == 0
    assert metrics.connect_failures == 1


@pytest.mark.asyncio
async def test_worker_retries_transient_poll_errors_with_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = ConnectionWorker(
        make_connection(protocol_options={"read_failures": 1}),
        make_registry(),
        clock=FixedClock(),
        retry_sleep=no_sleep,
        jitter=lambda: 0.5,
    )
    caplog.set_level(
        logging.INFO, logger="plc_data_collector.runtime.connection_worker"
    )
    await worker.connect()

    result = await worker.poll_group(make_group(), (make_tag(),))

    metrics = worker.metrics()
    assert result.execution.status is PollStatus.SUCCESS
    assert metrics.poll_attempts == 1
    assert metrics.poll_retry_attempts == 1
    assert metrics.last_poll_correlation_id == result.execution.execution_id
    poll_records = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "poll_group"
    ]
    assert {getattr(record, "correlation_id", None) for record in poll_records} == {
        result.execution.execution_id
    }


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


class ScriptedDriver:
    """Fake driver whose connect and read outcomes are queued per call."""

    def __init__(
        self,
        connect_outcomes: Sequence[BaseException | None],
        read_outcomes: Sequence[Exception | str | None],
        health_outcomes: Sequence[bool | Exception] = (),
    ) -> None:
        """Queue outcomes; None means success, an Exception is raised."""
        self.connect_outcomes = list(connect_outcomes)
        self.read_outcomes = list(read_outcomes)
        self.health_outcomes = list(health_outcomes)
        self.connect_calls = 0
        self.read_calls = 0
        self.health_calls = 0
        self.disconnect_calls = 0

    @property
    def capabilities(self) -> DriverCapabilities:
        """Return fake capabilities."""
        return DriverCapabilities(protocol="fake")

    async def connect(self) -> None:
        """Pop the next connect outcome."""
        self.connect_calls += 1
        outcome = self.connect_outcomes.pop(0)
        if outcome is not None:
            raise outcome

    async def disconnect(self) -> None:
        """Count disconnects."""
        self.disconnect_calls += 1

    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]:
        """Pop the next read outcome; 'partial' fails the last tag only."""
        self.read_calls += 1
        outcome = self.read_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
        if outcome == "all_failed":
            return [
                TagResult.failure(
                    tag_id=tag.tag_id,
                    error_code="fake_tag_failed",
                    error_message="tag failed",
                    received_at=now,
                )
                for tag in tags
            ]
        results = [
            TagResult.success(
                tag_id=tag.tag_id,
                value=TagValue.numeric(1.0),
                source_timestamp=now,
                received_at=now,
            )
            for tag in tags
        ]
        if outcome == "partial":
            results[-1] = TagResult.failure(
                tag_id=tags[-1].tag_id,
                error_code="fake_tag_failed",
                error_message="tag failed",
                received_at=now,
            )
        return results

    async def health_check(self) -> bool:
        """Pop the next health outcome; default healthy."""
        self.health_calls += 1
        if not self.health_outcomes:
            return True
        outcome = self.health_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_scripted_worker(driver: ScriptedDriver) -> ConnectionWorker:
    registry = DriverRegistry()
    registry.register("fake", lambda _connection: driver)
    return ConnectionWorker(
        make_connection(),
        registry,
        clock=FixedClock(),
        retry_sleep=no_sleep,
        jitter=lambda: 0.5,
    )


def transient(code: str = "fake_transient") -> TransientCommunicationError:
    return TransientCommunicationError("transient", code=code)


@pytest.mark.asyncio
async def test_poll_reconnects_after_transient_failure() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[None, None],
        read_outcomes=[transient(), transient(), None],
    )
    worker = make_scripted_worker(driver)
    await worker.connect()

    first = await worker.poll_group(make_group(), (make_tag(),))
    second = await worker.poll_group(make_group(), (make_tag(),))

    assert first.execution.status is PollStatus.FAILED
    assert second.execution.status is PollStatus.SUCCESS
    assert worker.metrics().reconnect_attempts == 1
    assert driver.connect_calls == 2
    assert driver.disconnect_calls == 1
    assert worker.status().state is WorkerState.RUNNING


@pytest.mark.asyncio
async def test_poll_returns_failed_result_when_reconnect_fails() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[None, transient(), transient(), transient()],
        read_outcomes=[transient(), transient()],
    )
    worker = make_scripted_worker(driver)
    await worker.connect()
    await worker.poll_group(make_group(), (make_tag(),))
    reads_before = driver.read_calls

    result = await worker.poll_group(make_group(), (make_tag(), make_tag("pressure")))

    assert result.execution.status is PollStatus.FAILED
    assert [r.error_code for r in result.results] == ["connection_unavailable"] * 2
    assert driver.read_calls == reads_before
    assert worker.status().state is WorkerState.FAILED


@pytest.mark.asyncio
async def test_initial_connect_failure_recovers_on_next_poll() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[transient(), transient(), transient(), None],
        read_outcomes=[None],
    )
    worker = make_scripted_worker(driver)
    with pytest.raises(TransientCommunicationError):
        await worker.connect()
    assert worker.status().state is WorkerState.FAILED

    result = await worker.poll_group(make_group(), (make_tag(),))

    assert result.execution.status is PollStatus.SUCCESS
    assert worker.status().state is WorkerState.RUNNING
    assert worker.metrics().reconnect_attempts == 1
    assert driver.connect_calls == 4


@pytest.mark.asyncio
async def test_reconnect_keeps_failure_state_visible() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[transient() for _ in range(6)],
        read_outcomes=[],
    )
    worker = make_scripted_worker(driver)
    with pytest.raises(TransientCommunicationError):
        await worker.connect()
    states_during_reconnect: list[WorkerState] = []
    original_connect = driver.connect

    async def observing_connect() -> None:
        states_during_reconnect.append(worker.status().state)
        await original_connect()

    driver.connect = observing_connect  # type: ignore[method-assign]

    await worker.poll_group(make_group(), (make_tag(),))

    # The API must keep showing the outage while a reconnect is in progress.
    assert states_during_reconnect == [WorkerState.FAILED] * 3
    assert worker.status().state is WorkerState.FAILED
    assert worker.status().error_code == "fake_transient"


@pytest.mark.asyncio
async def test_permanent_error_does_not_reconnect() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[None],
        read_outcomes=[
            PermanentProtocolError("nope", code="fake_permanent"),
            PermanentProtocolError("nope", code="fake_permanent"),
        ],
    )
    worker = make_scripted_worker(driver)
    await worker.connect()

    await worker.poll_group(make_group(), (make_tag(),))
    await worker.poll_group(make_group(), (make_tag(),))

    assert worker.metrics().reconnect_attempts == 0
    assert driver.connect_calls == 1


@pytest.mark.asyncio
async def test_partial_failure_does_not_reconnect() -> None:
    driver = ScriptedDriver(connect_outcomes=[None], read_outcomes=["partial", None])
    worker = make_scripted_worker(driver)
    await worker.connect()

    first = await worker.poll_group(make_group(), (make_tag(), make_tag("pressure")))
    await worker.poll_group(make_group(), (make_tag(), make_tag("pressure")))

    assert first.execution.status is PollStatus.PARTIAL_FAILURE
    assert worker.metrics().reconnect_attempts == 0
    assert driver.connect_calls == 1


@pytest.mark.asyncio
async def test_cancellation_during_reconnect_propagates() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[None, asyncio.CancelledError()],
        read_outcomes=[transient(), transient()],
    )
    worker = make_scripted_worker(driver)
    await worker.connect()
    await worker.poll_group(make_group(), (make_tag(),))

    with pytest.raises(asyncio.CancelledError):
        await worker.poll_group(make_group(), (make_tag(),))


@pytest.mark.asyncio
async def test_all_tags_failed_and_unhealthy_triggers_reconnect() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[None, None],
        read_outcomes=["all_failed", None],
        health_outcomes=[False],
    )
    worker = make_scripted_worker(driver)
    await worker.connect()

    first = await worker.poll_group(make_group(), (make_tag(),))
    second = await worker.poll_group(make_group(), (make_tag(),))

    assert first.execution.status is PollStatus.FAILED
    assert second.execution.status is PollStatus.SUCCESS
    assert worker.metrics().reconnect_attempts == 1
    assert driver.health_calls == 1


@pytest.mark.asyncio
async def test_all_tags_failed_but_healthy_does_not_reconnect() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[None],
        read_outcomes=["all_failed", "all_failed"],
        health_outcomes=[True, True],
    )
    worker = make_scripted_worker(driver)
    await worker.connect()

    await worker.poll_group(make_group(), (make_tag(),))
    await worker.poll_group(make_group(), (make_tag(),))

    assert worker.metrics().reconnect_attempts == 0
    assert driver.connect_calls == 1


@pytest.mark.asyncio
async def test_health_check_raising_counts_as_unhealthy() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[None, None],
        read_outcomes=["all_failed", None],
        health_outcomes=[OSError("link down")],
    )
    worker = make_scripted_worker(driver)
    await worker.connect()

    await worker.poll_group(make_group(), (make_tag(),))
    second = await worker.poll_group(make_group(), (make_tag(),))

    assert second.execution.status is PollStatus.SUCCESS
    assert worker.metrics().reconnect_attempts == 1


@pytest.mark.asyncio
async def test_permanent_and_partial_failures_skip_health_check() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[None],
        read_outcomes=[PermanentProtocolError("nope", code="p"), "partial"],
    )
    worker = make_scripted_worker(driver)
    await worker.connect()

    await worker.poll_group(make_group(), (make_tag(),))
    await worker.poll_group(make_group(), (make_tag(), make_tag("pressure")))

    assert driver.health_calls == 0


class GatedConnectDriver(ScriptedDriver):
    """Scripted driver whose connect waits until the test opens a gate."""

    def __init__(
        self,
        connect_outcomes: Sequence[BaseException | None],
        read_outcomes: Sequence[Exception | str | None],
    ) -> None:
        """Create the driver with the gate open."""
        super().__init__(connect_outcomes, read_outcomes)
        self.gate = asyncio.Event()
        self.gate.set()
        self.connect_started = asyncio.Event()

    async def connect(self) -> None:
        """Signal the attempt, wait for the gate, then pop the outcome."""
        self.connect_started.set()
        await self.gate.wait()
        await super().connect()


def make_named_group(group_id: str) -> TagGroupConfig:
    return TagGroupConfig(
        id=group_id,
        connection_id="fake_connection",
        interval_ms=1000,
        timeout_ms=500,
    )


@pytest.mark.asyncio
async def test_two_tag_groups_share_one_reconnect() -> None:
    driver = GatedConnectDriver(
        connect_outcomes=[transient(), transient(), transient(), None],
        read_outcomes=[None, None, None],
    )
    worker = make_scripted_worker(driver)
    with pytest.raises(TransientCommunicationError):
        await worker.connect()
    assert driver.connect_calls == 3
    driver.gate.clear()
    driver.connect_started.clear()
    fast = make_named_group("fast")
    slow = make_named_group("slow")

    fast_poll = asyncio.create_task(worker.poll_group(fast, (make_tag(),)))
    await driver.connect_started.wait()
    # The fast group is mid-reconnect; the slow group must not start another.
    slow_result = await asyncio.wait_for(
        worker.poll_group(slow, (make_tag("level", "slow"),)), timeout=1.0
    )
    driver.gate.set()
    fast_result = await fast_poll
    fast_again, slow_again = await asyncio.gather(
        worker.poll_group(fast, (make_tag(),)),
        worker.poll_group(slow, (make_tag("level", "slow"),)),
    )

    assert [r.error_code for r in slow_result.results] == ["connection_unavailable"]
    assert fast_result.execution.status is PollStatus.SUCCESS
    assert fast_again.execution.status is PollStatus.SUCCESS
    assert slow_again.execution.status is PollStatus.SUCCESS
    assert worker.metrics().reconnect_attempts == 1
    assert driver.connect_calls == 4
    assert driver.disconnect_calls == 1


@pytest.mark.asyncio
async def test_concurrent_polls_after_outage_reconnect_once() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[transient(), transient(), transient(), None],
        read_outcomes=[None, None],
    )
    worker = make_scripted_worker(driver)
    with pytest.raises(TransientCommunicationError):
        await worker.connect()

    first, second = await asyncio.gather(
        worker.poll_group(make_named_group("fast"), (make_tag(),)),
        worker.poll_group(make_named_group("slow"), (make_tag("level", "slow"),)),
    )

    assert worker.metrics().reconnect_attempts == 1
    assert driver.connect_calls == 4
    statuses = {first.execution.status, second.execution.status}
    assert PollStatus.SUCCESS in statuses


@pytest.mark.asyncio
async def test_all_failed_poll_error_message_is_capped() -> None:
    driver = ScriptedDriver(
        connect_outcomes=[None],
        read_outcomes=["all_failed"],
        health_outcomes=[True],
    )
    worker = make_scripted_worker(driver)
    await worker.connect()
    tags = tuple(make_tag(f"tag_{index}") for index in range(100))

    result = await worker.poll_group(make_group(), tags)

    assert result.execution.failed_tags == 100
    assert result.execution.error_message == "tag failed"


def test_poll_error_message_is_truncated_to_module_limit() -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    results = tuple(
        TagResult.failure(
            tag_id=f"tag_{index}",
            error_code="fake_tag_failed",
            error_message=f"distinct failure number {index:03d}",
            received_at=now,
        )
        for index in range(100)
    )

    message = _poll_error_message(results)

    assert message is not None
    assert len(message) == POLL_ERROR_MESSAGE_MAX_LENGTH
    assert message.startswith("distinct failure number 000; ")
    assert message.endswith("...")
