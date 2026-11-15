from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from plc_data_collector.domain import (
    PollExecution,
    PollStatus,
    Quality,
    TagResult,
    TagValue,
)
from plc_data_collector.runtime import LiveValueCache, WorkerPollResult, freshness

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def make_execution(
    execution_id: str = "poll-1",
    tag_group_id: str = "fast",
    started_at: datetime = T0,
) -> PollExecution:
    return PollExecution(
        execution_id=execution_id,
        connection_id="opcua_line_1",
        tag_group_id=tag_group_id,
        status=PollStatus.SUCCESS,
        started_at=started_at,
        finished_at=started_at,
        requested_tags=1,
    )


def good(tag_id: str, value: float, at: datetime = T0) -> TagResult:
    return TagResult.success(
        tag_id=tag_id,
        value=TagValue.numeric(value),
        source_timestamp=at,
        received_at=at,
    )


def bad(tag_id: str, at: datetime = T0) -> TagResult:
    return TagResult.failure(
        tag_id=tag_id,
        error_code="bad_node_id",
        error_message="Node does not exist.",
        received_at=at,
    )


def poll(
    results: tuple[TagResult, ...],
    *,
    execution: PollExecution | None = None,
) -> WorkerPollResult:
    return WorkerPollResult(execution=execution or make_execution(), results=results)


def test_update_keeps_latest_per_tag() -> None:
    cache = LiveValueCache()
    cache.update(poll((good("temp", 20.0),), execution=make_execution("poll-1")))
    cache.update(
        poll(
            (good("temp", 21.5, T0 + timedelta(seconds=1)),),
            execution=make_execution("poll-2", started_at=T0 + timedelta(seconds=1)),
        )
    )

    live = cache.get("temp")
    assert live is not None
    assert live.tag_id == "temp"
    assert live.connection_id == "opcua_line_1"
    assert live.tag_group_id == "fast"
    assert live.result.value == TagValue.numeric(21.5)
    assert cache.get("missing") is None
    assert set(cache.values()) == {"temp"}


def test_values_returns_a_copy() -> None:
    cache = LiveValueCache()
    cache.update(poll((good("temp", 20.0),)))

    snapshot = cache.values()
    cache.update(poll((good("pressure", 3.0),)))

    assert "pressure" not in snapshot
    assert set(cache.values()) == {"temp", "pressure"}


def test_failed_result_replaces_good_one() -> None:
    cache = LiveValueCache()
    cache.update(poll((good("temp", 20.0),)))
    cache.update(poll((bad("temp", T0 + timedelta(seconds=1)),)))

    live = cache.get("temp")
    assert live is not None
    assert live.result.quality is Quality.BAD
    assert live.result.error_code == "bad_node_id"
    assert live.result.value is None


def test_last_poll_tracks_latest_execution_per_group() -> None:
    cache = LiveValueCache()
    assert cache.last_poll("fast") is None

    first = make_execution("poll-1", "fast", T0)
    second = make_execution("poll-2", "fast", T0 + timedelta(seconds=1))
    slow = make_execution("poll-3", "slow", T0)
    cache.update(poll((), execution=first))
    cache.update(poll((), execution=second))
    cache.update(poll((), execution=slow))

    assert cache.last_poll("fast") == second
    assert cache.last_poll("slow") == slow
    assert cache.last_poll("unknown") is None


def test_retain_prunes() -> None:
    cache = LiveValueCache()
    cache.update(poll((good("temp", 20.0), good("pressure", 3.0))))
    cache.update(poll((good("flow", 1.0),), execution=make_execution("poll-2", "slow")))

    cache.retain(tag_ids={"temp"}, tag_group_ids={"fast"})

    assert set(cache.values()) == {"temp"}
    assert cache.last_poll("fast") is not None
    assert cache.last_poll("slow") is None


@pytest.mark.parametrize(
    ("age_s", "interval_s", "fraction", "stale"),
    [
        (0, 1, 1.0, False),
        (2, 1, 0.5, False),
        (2.5, 1, 0.375, True),
        (5, 1, 0.0, True),
        (10, 1, 0.0, True),
    ],
)
def test_freshness(
    age_s: float, interval_s: float, fraction: float, stale: bool
) -> None:
    result = freshness(age_s, interval_s)

    assert result.fraction == pytest.approx(fraction)
    assert result.stale is stale


def test_freshness_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError, match="interval_s"):
        freshness(1.0, 0)
