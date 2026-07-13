"""Asyncio-based non-overlapping scheduler for tag group polling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from time import monotonic

from plc_gateway.domain import ConfigurationError, OverlapPolicy, TagGroupConfig

MonotonicClock = Callable[[], float]
SchedulerSleep = Callable[[float], Awaitable[None]]
PollHandler = Callable[[TagGroupConfig], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PollScheduleState:
    """Observable scheduler state for one tag group."""

    tag_group_id: str
    interval_ms: int
    running: bool
    started_cycles: int
    missed_cycles: int
    failed_cycles: int
    next_due_at: float
    last_started_at: float | None = None
    last_finished_at: float | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class PollSchedulerSnapshot:
    """Point-in-time scheduler state for diagnostics."""

    running: bool
    groups: tuple[PollScheduleState, ...]


@dataclass(slots=True)
class _GroupSchedule:
    group: TagGroupConfig
    next_due_at: float
    active_task: asyncio.Task[None] | None = None
    started_cycles: int = 0
    missed_cycles: int = 0
    failed_cycles: int = 0
    last_started_at: float | None = None
    last_finished_at: float | None = None
    last_error: str | None = None


class PollScheduler:
    """Schedule tag groups at fixed intervals without overlapping cycles."""

    def __init__(
        self,
        groups: Iterable[TagGroupConfig],
        handler: PollHandler,
        *,
        clock: MonotonicClock = monotonic,
        sleep: SchedulerSleep = asyncio.sleep,
    ) -> None:
        """Create a scheduler for enabled tag groups."""
        self._handler = handler
        self._clock = clock
        self._sleep = sleep
        self._stop_requested = asyncio.Event()
        self._running = False
        self._schedules = self._build_schedules(groups)

    async def run(self) -> None:
        """Run the scheduler until stopped or cancelled."""
        if self._running:
            raise RuntimeError("PollScheduler is already running.")
        self._running = True
        self._stop_requested.clear()
        try:
            while not self._stop_requested.is_set():
                now = self._clock()
                self._start_due_cycles(now)
                delay = self._seconds_until_next_due(now)
                await self._sleep(delay)
        except asyncio.CancelledError:
            await self.cancel_active_cycles()
            raise
        finally:
            self._running = False

    def stop(self) -> None:
        """Request scheduler loop shutdown without cancelling active cycles."""
        self._stop_requested.set()

    async def cancel_active_cycles(self) -> None:
        """Cancel all active poll cycles and wait for their cancellation."""
        tasks = [
            schedule.active_task
            for schedule in self._schedules.values()
            if schedule.active_task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def snapshot(self) -> PollSchedulerSnapshot:
        """Return observable scheduler state."""
        return PollSchedulerSnapshot(
            running=self._running,
            groups=tuple(
                PollScheduleState(
                    tag_group_id=schedule.group.id,
                    interval_ms=schedule.group.interval_ms,
                    running=schedule.active_task is not None,
                    started_cycles=schedule.started_cycles,
                    missed_cycles=schedule.missed_cycles,
                    failed_cycles=schedule.failed_cycles,
                    next_due_at=schedule.next_due_at,
                    last_started_at=schedule.last_started_at,
                    last_finished_at=schedule.last_finished_at,
                    last_error=schedule.last_error,
                )
                for schedule in sorted(
                    self._schedules.values(),
                    key=lambda item: item.group.id,
                )
            ),
        )

    def _start_due_cycles(self, now: float) -> None:
        for schedule in self._schedules.values():
            if now < schedule.next_due_at:
                continue

            due_count = _due_count(
                now=now,
                next_due_at=schedule.next_due_at,
                interval_seconds=schedule.group.interval_ms / 1000,
            )
            schedule.next_due_at += due_count * (schedule.group.interval_ms / 1000)

            if schedule.active_task is not None:
                schedule.missed_cycles += due_count
                continue

            schedule.started_cycles += 1
            schedule.last_started_at = now
            schedule.last_error = None
            if due_count > 1:
                schedule.missed_cycles += due_count - 1
            task = asyncio.create_task(self._run_cycle(schedule))
            schedule.active_task = task

    async def _run_cycle(self, schedule: _GroupSchedule) -> None:
        try:
            await self._handler(schedule.group)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            schedule.failed_cycles += 1
            schedule.last_error = str(error)
        finally:
            schedule.last_finished_at = self._clock()
            schedule.active_task = None

    def _seconds_until_next_due(self, now: float) -> float:
        if not self._schedules:
            return 0.1
        next_due = min(schedule.next_due_at for schedule in self._schedules.values())
        return max(0.0, next_due - now)

    def _build_schedules(
        self,
        groups: Iterable[TagGroupConfig],
    ) -> dict[str, _GroupSchedule]:
        start_time = self._clock()
        schedules: dict[str, _GroupSchedule] = {}
        for group in groups:
            if not group.enabled:
                continue
            if group.overlap_policy is not OverlapPolicy.SKIP:
                raise ConfigurationError(
                    f"Unsupported overlap policy '{group.overlap_policy}'.",
                    code="unsupported_overlap_policy",
                    details={"tag_group_id": group.id},
                )
            if group.id in schedules:
                raise ConfigurationError(
                    f"Duplicate scheduled tag group '{group.id}'.",
                    code="duplicate_scheduled_tag_group",
                    details={"tag_group_id": group.id},
                )
            schedules[group.id] = _GroupSchedule(group=group, next_due_at=start_time)
        return schedules


def _due_count(*, now: float, next_due_at: float, interval_seconds: float) -> int:
    if now < next_due_at:
        return 0
    elapsed = now - next_due_at
    return int(elapsed // interval_seconds) + 1
