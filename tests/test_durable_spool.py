from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from plc_gateway.domain import (
    PollExecution,
    PollStatus,
    StorageError,
    TagResult,
    TagValue,
)
from plc_gateway.persistence import DurableSqliteSpool
from plc_gateway.runtime import WorkerPollResult


def timestamp() -> datetime:
    return datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def make_poll_result(
    execution_id: str,
    *,
    tag_id: str = "temperature",
) -> WorkerPollResult:
    now = timestamp()
    return WorkerPollResult(
        execution=PollExecution(
            execution_id=execution_id,
            connection_id="mock_connection",
            tag_group_id="fast",
            status=PollStatus.SUCCESS,
            started_at=now,
            finished_at=now,
            requested_tags=1,
            successful_tags=1,
        ),
        results=(
            TagResult.success(
                tag_id=tag_id,
                value=TagValue.numeric(20.5),
                source_timestamp=now,
                received_at=now,
            ),
        ),
    )


def test_durable_spool_survives_process_restart(tmp_path: Path) -> None:
    spool_path = tmp_path / "spool.db"
    first = DurableSqliteSpool(spool_path, max_items=10)
    first.append([make_poll_result("poll-1")])

    restarted = DurableSqliteSpool(spool_path, max_items=10)
    due = restarted.fetch_due(limit=10)

    assert restarted.count() == 1
    assert due[0].execution_id == "poll-1"
    assert due[0].poll_result.results[0].value is not None
    assert due[0].poll_result.results[0].value.numeric_value == 20.5


def test_durable_spool_uses_execution_id_as_unique_event_id(tmp_path: Path) -> None:
    spool = DurableSqliteSpool(tmp_path / "spool.db", max_items=10)
    item = make_poll_result("poll-1")

    assert spool.append([item]) == 1
    assert spool.append([item]) == 0

    assert spool.count() == 1


def test_durable_spool_enforces_limit_and_reports_alarm(tmp_path: Path) -> None:
    spool = DurableSqliteSpool(tmp_path / "spool.db", max_items=1)
    spool.append([make_poll_result("poll-1")])

    with pytest.raises(StorageError, match="full"):
        spool.append([make_poll_result("poll-2")])

    metrics = spool.metrics()
    assert metrics.full is True
    assert metrics.stored_items == 1
    assert metrics.rejected_items == 1


def test_durable_spool_retry_count_and_next_retry_are_updated(
    tmp_path: Path,
) -> None:
    spool = DurableSqliteSpool(tmp_path / "spool.db", max_items=10)
    now = timestamp()
    spool.append([make_poll_result("poll-1")])

    assert spool.mark_failed(["poll-1"], retry_delay_s=60, now=now) == 1

    assert spool.fetch_due(limit=10, now=now) == []
    due = spool.fetch_due(limit=10, now=now + timedelta(seconds=60))
    assert due[0].retry_count == 1
    assert due[0].next_retry_at == now + timedelta(seconds=60)


def test_durable_spool_deletes_only_after_confirmed_replay(tmp_path: Path) -> None:
    spool = DurableSqliteSpool(tmp_path / "spool.db", max_items=10)
    spool.append([make_poll_result("poll-1"), make_poll_result("poll-2")])

    assert spool.mark_replayed(["poll-1"]) == 1

    due = spool.fetch_due(limit=10)
    assert [entry.execution_id for entry in due] == ["poll-2"]
    assert spool.metrics().replayed_items == 1
