"""Durable SQLite spool for poll results awaiting database recovery."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from plc_gateway.domain import (
    PollExecution,
    PollStatus,
    Quality,
    StorageError,
    TagResult,
    TagValue,
    ValueType,
)
from plc_gateway.runtime import WorkerPollResult


@dataclass(frozen=True, slots=True)
class SpoolEntry:
    """One due durable spool entry."""

    execution_id: str
    poll_result: WorkerPollResult
    retry_count: int
    next_retry_at: datetime


@dataclass(frozen=True, slots=True)
class SpoolMetrics:
    """Observable durable spool state."""

    max_items: int
    stored_items: int
    full: bool
    rejected_items: int
    replayed_items: int
    failed_replays: int


class DurableSqliteSpool:
    """SQLite-backed spool preserving poll results across process restarts."""

    def __init__(self, path: Path | str, *, max_items: int) -> None:
        """Create or open a bounded durable spool."""
        if max_items <= 0:
            raise ValueError("max_items must be positive.")
        self._path = Path(path)
        self._max_items = max_items
        self._rejected_items = 0
        self._replayed_items = 0
        self._failed_replays = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def append(self, poll_results: Iterable[WorkerPollResult]) -> int:
        """Persist poll results and return newly spooled item count."""
        items = list(poll_results)
        if not items:
            return 0

        now = _utc_now()
        inserted = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = _count_items(connection)
                existing_ids = _existing_ids(
                    connection,
                    (item.execution.execution_id for item in items),
                )
                new_items = [
                    item
                    for item in items
                    if item.execution.execution_id not in existing_ids
                ]
                available = self._max_items - existing
                if len(new_items) > available:
                    self._rejected_items += len(new_items) - max(available, 0)
                    raise StorageError(
                        "Durable spool is full.",
                        code="durable_spool_full",
                        details={
                            "max_items": self._max_items,
                            "stored_items": existing,
                            "incoming_items": len(new_items),
                        },
                    )
                for item in new_items:
                    connection.execute(
                        """
                        INSERT INTO spooled_poll_results (
                            execution_id,
                            payload_json,
                            retry_count,
                            next_retry_at,
                            created_at
                        ) VALUES (?, ?, 0, ?, ?)
                        """,
                        (
                            item.execution.execution_id,
                            json.dumps(_poll_result_to_json(item), sort_keys=True),
                            _datetime_to_json(now),
                            _datetime_to_json(now),
                        ),
                    )
                    inserted += 1
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()
        return inserted

    def fetch_due(self, *, limit: int, now: datetime | None = None) -> list[SpoolEntry]:
        """Return due entries ordered by creation time."""
        if limit <= 0:
            raise ValueError("limit must be positive.")
        due_at = _normalize_datetime(_utc_now() if now is None else now)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT execution_id, payload_json, retry_count, next_retry_at
                FROM spooled_poll_results
                WHERE next_retry_at <= ?
                ORDER BY created_at, execution_id
                LIMIT ?
                """,
                (_datetime_to_json(due_at), limit),
            ).fetchall()
        return [
            SpoolEntry(
                execution_id=str(row["execution_id"]),
                poll_result=_poll_result_from_json(
                    json.loads(str(row["payload_json"]))
                ),
                retry_count=int(row["retry_count"]),
                next_retry_at=_datetime_from_json(str(row["next_retry_at"])),
            )
            for row in rows
        ]

    def mark_replayed(self, execution_ids: Iterable[str]) -> int:
        """Delete entries after confirmed database write."""
        ids = tuple(execution_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM spooled_poll_results "
                f"WHERE execution_id IN ({placeholders})",
                ids,
            )
            deleted = cursor.rowcount
        self._replayed_items += deleted
        return deleted

    def mark_failed(
        self,
        execution_ids: Iterable[str],
        *,
        retry_delay_s: float,
        now: datetime | None = None,
    ) -> int:
        """Record failed replay attempts and schedule the next retry."""
        if retry_delay_s < 0:
            raise ValueError("retry_delay_s must be non-negative.")
        ids = tuple(execution_ids)
        if not ids:
            return 0
        next_retry_at = _normalize_datetime(
            (_utc_now() if now is None else now) + timedelta(seconds=retry_delay_s)
        )
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE spooled_poll_results
                SET retry_count = retry_count + 1,
                    next_retry_at = ?
                WHERE execution_id IN ({placeholders})
                """,
                (_datetime_to_json(next_retry_at), *ids),
            )
            updated = cursor.rowcount
        self._failed_replays += updated
        return updated

    def metrics(self) -> SpoolMetrics:
        """Return observable durable spool metrics."""
        stored_items = self.count()
        return SpoolMetrics(
            max_items=self._max_items,
            stored_items=stored_items,
            full=stored_items >= self._max_items,
            rejected_items=self._rejected_items,
            replayed_items=self._replayed_items,
            failed_replays=self._failed_replays,
        )

    def count(self) -> int:
        """Return number of currently spooled poll results."""
        with self._connect() as connection:
            return _count_items(connection)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS spooled_poll_results (
                    execution_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_spooled_poll_results_next_retry_at
                ON spooled_poll_results (next_retry_at, created_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        return connection


def _count_items(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute("SELECT COUNT(*) FROM spooled_poll_results").fetchone()[0]
    )


def _existing_ids(
    connection: sqlite3.Connection,
    execution_ids: Iterable[str],
) -> set[str]:
    ids = tuple(execution_ids)
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        "SELECT execution_id FROM spooled_poll_results "
        f"WHERE execution_id IN ({placeholders})",
        ids,
    ).fetchall()
    return {str(row["execution_id"]) for row in rows}


def _poll_result_to_json(poll_result: WorkerPollResult) -> dict[str, object]:
    execution = poll_result.execution
    return {
        "execution": {
            "execution_id": execution.execution_id,
            "connection_id": execution.connection_id,
            "tag_group_id": execution.tag_group_id,
            "status": execution.status.value,
            "started_at": _datetime_to_json(execution.started_at),
            "finished_at": _optional_datetime_to_json(execution.finished_at),
            "requested_tags": execution.requested_tags,
            "successful_tags": execution.successful_tags,
            "failed_tags": execution.failed_tags,
            "error_message": execution.error_message,
        },
        "results": [_tag_result_to_json(result) for result in poll_result.results],
    }


def _poll_result_from_json(payload: dict[str, object]) -> WorkerPollResult:
    execution_payload = _require_dict(payload["execution"], "execution")
    results_payload = _require_list(payload["results"], "results")
    execution = PollExecution(
        execution_id=_require_str(execution_payload["execution_id"], "execution_id"),
        connection_id=_require_str(execution_payload["connection_id"], "connection_id"),
        tag_group_id=_require_str(execution_payload["tag_group_id"], "tag_group_id"),
        status=PollStatus(_require_str(execution_payload["status"], "status")),
        started_at=_datetime_from_json(
            _require_str(execution_payload["started_at"], "started_at")
        ),
        finished_at=_optional_datetime_from_json(execution_payload.get("finished_at")),
        requested_tags=_require_int(
            execution_payload["requested_tags"],
            "requested_tags",
        ),
        successful_tags=_require_int(
            execution_payload["successful_tags"],
            "successful_tags",
        ),
        failed_tags=_require_int(execution_payload["failed_tags"], "failed_tags"),
        error_message=_optional_str(
            execution_payload.get("error_message"),
            "error_message",
        ),
    )
    return WorkerPollResult(
        execution=execution,
        results=tuple(
            _tag_result_from_json(_require_dict(item, "result"))
            for item in results_payload
        ),
    )


def _tag_result_to_json(result: TagResult) -> dict[str, object]:
    return {
        "tag_id": result.tag_id,
        "quality": result.quality.value,
        "received_at": _datetime_to_json(result.received_at),
        "source_timestamp": _optional_datetime_to_json(result.source_timestamp),
        "value": _tag_value_to_json(result.value) if result.value is not None else None,
        "error_code": result.error_code,
        "error_message": result.error_message,
    }


def _tag_result_from_json(payload: dict[str, object]) -> TagResult:
    value_payload = payload.get("value")
    return TagResult(
        tag_id=_require_str(payload["tag_id"], "tag_id"),
        quality=Quality(_require_str(payload["quality"], "quality")),
        received_at=_datetime_from_json(
            _require_str(payload["received_at"], "received_at")
        ),
        source_timestamp=_optional_datetime_from_json(payload.get("source_timestamp")),
        value=(
            _tag_value_from_json(_require_dict(value_payload, "value"))
            if value_payload is not None
            else None
        ),
        error_code=_optional_str(payload.get("error_code"), "error_code"),
        error_message=_optional_str(payload.get("error_message"), "error_message"),
    )


def _tag_value_to_json(value: TagValue) -> dict[str, object]:
    payload: dict[str, object] = {
        "value_type": value.value_type.value,
        "numeric_value": value.numeric_value,
        "integer_value": value.integer_value,
        "boolean_value": value.boolean_value,
        "text_value": value.text_value,
        "binary_value": (
            base64.b64encode(value.binary_value).decode("ascii")
            if value.binary_value is not None
            else None
        ),
    }
    return payload


def _tag_value_from_json(payload: dict[str, object]) -> TagValue:
    value_type = ValueType(_require_str(payload["value_type"], "value_type"))
    if value_type is ValueType.NUMERIC:
        value = payload.get("numeric_value")
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise StorageError("Invalid spooled numeric value.")
        return TagValue.numeric(value)
    if value_type is ValueType.INTEGER:
        value = payload.get("integer_value")
        if not isinstance(value, int) or isinstance(value, bool):
            raise StorageError("Invalid spooled integer value.")
        return TagValue.integer(value)
    if value_type is ValueType.BOOLEAN:
        value = payload.get("boolean_value")
        if not isinstance(value, bool):
            raise StorageError("Invalid spooled boolean value.")
        return TagValue.boolean(value)
    if value_type is ValueType.TEXT:
        return TagValue.text(_require_str(payload.get("text_value"), "text_value"))
    if value_type is ValueType.BINARY:
        encoded = _require_str(payload.get("binary_value"), "binary_value")
        return TagValue.binary(base64.b64decode(encoded.encode("ascii")))
    raise StorageError(f"Unsupported spooled value type '{value_type}'.")


def _require_dict(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StorageError(f"Invalid spooled payload field '{field_name}'.")
    return value


def _require_list(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise StorageError(f"Invalid spooled payload field '{field_name}'.")
    return value


def _require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise StorageError(f"Invalid spooled payload field '{field_name}'.")
    return value


def _optional_str(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, field_name)


def _require_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise StorageError(f"Invalid spooled payload field '{field_name}'.")
    return value


def _datetime_to_json(value: datetime) -> str:
    return _normalize_datetime(value).isoformat()


def _optional_datetime_to_json(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _datetime_to_json(value)


def _datetime_from_json(value: str) -> datetime:
    return _normalize_datetime(datetime.fromisoformat(value))


def _optional_datetime_from_json(value: object) -> datetime | None:
    if value is None:
        return None
    return _datetime_from_json(_require_str(value, "datetime"))


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageError("Spooled timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
