"""In-memory cache of the most recent value per tag, filled on every poll."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from plc_data_collector.domain import PollExecution, TagResult
from plc_data_collector.runtime.connection_worker import WorkerPollResult


@dataclass(frozen=True, slots=True)
class LiveValue:
    """Latest result for one tag, with the Connection and Tag Group it came from."""

    tag_id: str
    connection_id: str
    tag_group_id: str
    result: TagResult


@dataclass(frozen=True, slots=True)
class GroupPoll:
    """Latest poll execution for one Tag Group."""

    tag_group_id: str
    execution: PollExecution


@dataclass(frozen=True, slots=True)
class Freshness:
    """How fresh a value is relative to its Tag Group interval."""

    fraction: float
    stale: bool


def freshness(age_s: float, interval_s: float) -> Freshness:
    """Return the freshness bar fraction and stale flag for a value of a given age."""
    if interval_s <= 0:
        raise ValueError("interval_s must be positive.")
    return Freshness(
        fraction=max(0.0, 1 - age_s / (4 * interval_s)),
        stale=age_s > 2 * interval_s,
    )


class LiveValueCache:
    """Keeps the latest result per tag and the latest poll per Tag Group.

    Used from a single asyncio event loop, so it needs no locking.
    """

    def __init__(self) -> None:
        self._values: dict[str, LiveValue] = {}
        self._polls: dict[str, GroupPoll] = {}

    def update(self, poll_result: WorkerPollResult) -> None:
        """Store every tag result of a poll, replacing the previous one per tag."""
        execution = poll_result.execution
        self._polls[execution.tag_group_id] = GroupPoll(
            tag_group_id=execution.tag_group_id,
            execution=execution,
        )
        for result in poll_result.results:
            self._values[result.tag_id] = LiveValue(
                tag_id=result.tag_id,
                connection_id=execution.connection_id,
                tag_group_id=execution.tag_group_id,
                result=result,
            )

    def get(self, tag_id: str) -> LiveValue | None:
        """Return the latest value for a tag, if any."""
        return self._values.get(tag_id)

    def values(self) -> Mapping[str, LiveValue]:
        """Return a copy of all latest values keyed by tag ID."""
        return dict(self._values)

    def last_poll(self, tag_group_id: str) -> PollExecution | None:
        """Return the latest poll execution of a Tag Group, if any."""
        group_poll = self._polls.get(tag_group_id)
        return group_poll.execution if group_poll is not None else None

    def retain(self, tag_ids: Collection[str], tag_group_ids: Collection[str]) -> None:
        """Drop every value and poll that is not in the given tag and group IDs."""
        keep_tags = set(tag_ids)
        keep_groups = set(tag_group_ids)
        self._values = {
            tag_id: live for tag_id, live in self._values.items() if tag_id in keep_tags
        }
        self._polls = {
            group_id: poll
            for group_id, poll in self._polls.items()
            if group_id in keep_groups
        }
