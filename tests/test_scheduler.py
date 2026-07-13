from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from plc_gateway.domain import TagGroupConfig
from plc_gateway.runtime import PollScheduler


class ManualClock:
    """Deterministic monotonic clock for scheduler tests."""

    def __init__(self) -> None:
        """Create a clock starting at monotonic time zero."""
        self.value = 0.0

    def __call__(self) -> float:
        """Return the current monotonic time."""
        return self.value

    def advance(self, seconds: float) -> None:
        """Advance monotonic time."""
        self.value += seconds


class AdvancingSleeper:
    """Deterministic scheduler sleeper that advances a manual clock."""

    def __init__(self, clock: ManualClock, *, stop_after: int) -> None:
        """Create a sleeper that requests scheduler stop after N sleeps."""
        self._clock = clock
        self._stop_after = stop_after
        self._sleep_count = 0
        self.delays: list[float] = []
        self.stop: Callable[[], None] | None = None

    async def __call__(self, delay: float) -> None:
        """Record delay, advance time, and yield to scheduled poll tasks."""
        self.delays.append(delay)
        self._sleep_count += 1
        await asyncio.sleep(0)
        self._clock.advance(delay)
        if self.stop is not None and self._sleep_count >= self._stop_after:
            self.stop()


def make_group(group_id: str = "fast", *, interval_ms: int = 1000) -> TagGroupConfig:
    return TagGroupConfig(
        id=group_id,
        connection_id="mock_connection",
        interval_ms=interval_ms,
        timeout_ms=500,
    )


@pytest.mark.asyncio
async def test_scheduler_runs_groups_at_fixed_intervals_without_drift() -> None:
    clock = ManualClock()
    starts: list[float] = []

    async def handler(_: TagGroupConfig) -> None:
        starts.append(clock())

    sleeper = AdvancingSleeper(clock, stop_after=3)
    scheduler = PollScheduler(
        [make_group()],
        handler,
        clock=clock,
        sleep=sleeper,
    )
    sleeper.stop = scheduler.stop

    await scheduler.run()
    await asyncio.sleep(0)

    assert starts == [0.0, 1.0, 2.0]
    assert sleeper.delays == [1.0, 1.0, 1.0]
    state = scheduler.snapshot().groups[0]
    assert state.started_cycles == 3
    assert state.missed_cycles == 0


@pytest.mark.asyncio
async def test_scheduler_skips_overlapping_cycles_and_records_misses() -> None:
    clock = ManualClock()
    release_handler = asyncio.Event()
    starts: list[float] = []

    async def handler(_: TagGroupConfig) -> None:
        starts.append(clock())
        await release_handler.wait()

    sleeper = AdvancingSleeper(clock, stop_after=3)
    scheduler = PollScheduler(
        [make_group()],
        handler,
        clock=clock,
        sleep=sleeper,
    )
    sleeper.stop = scheduler.stop

    await scheduler.run()

    state = scheduler.snapshot().groups[0]
    assert starts == [0.0]
    assert state.running is True
    assert state.started_cycles == 1
    assert state.missed_cycles == 2

    await scheduler.cancel_active_cycles()
    assert scheduler.snapshot().groups[0].running is False


@pytest.mark.asyncio
async def test_scheduler_cancel_active_cycles_cancels_handler() -> None:
    clock = ManualClock()
    started = asyncio.Event()
    cancelled = False

    async def handler(_: TagGroupConfig) -> None:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    sleeper = AdvancingSleeper(clock, stop_after=1)
    scheduler = PollScheduler(
        [make_group()],
        handler,
        clock=clock,
        sleep=sleeper,
    )
    sleeper.stop = scheduler.stop

    await scheduler.run()
    await started.wait()
    await scheduler.cancel_active_cycles()

    assert cancelled is True
    assert scheduler.snapshot().groups[0].running is False


@pytest.mark.asyncio
async def test_scheduler_records_handler_failures_without_stopping() -> None:
    clock = ManualClock()
    calls = 0

    async def handler(_: TagGroupConfig) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")

    sleeper = AdvancingSleeper(clock, stop_after=2)
    scheduler = PollScheduler(
        [make_group()],
        handler,
        clock=clock,
        sleep=sleeper,
    )
    sleeper.stop = scheduler.stop

    await scheduler.run()
    await asyncio.sleep(0)

    state = scheduler.snapshot().groups[0]
    assert calls == 2
    assert state.started_cycles == 2
    assert state.failed_cycles == 1
    assert state.last_error is None


def test_scheduler_ignores_disabled_groups() -> None:
    scheduler = PollScheduler(
        [
            TagGroupConfig(
                id="disabled",
                connection_id="mock_connection",
                interval_ms=1000,
                enabled=False,
            )
        ],
        _unused_handler,
    )

    assert scheduler.snapshot().groups == ()


async def _unused_handler(_: TagGroupConfig) -> None:
    return None
