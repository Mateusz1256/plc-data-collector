"""Read and prune stored Tag readings for the data API and retention."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column, Engine, Row, RowMapping, Table, delete, func, select

from plc_data_collector.domain import Quality, TagValue, ValueType
from plc_data_collector.persistence.schema import poll_executions, tag_readings

_VALUE_COLUMNS = {
    ValueType.NUMERIC: "numeric_value",
    ValueType.INTEGER: "integer_value",
    ValueType.BOOLEAN: "boolean_value",
    ValueType.TEXT: "text_value",
    ValueType.BINARY: "binary_value",
}


@dataclass(frozen=True, slots=True)
class TagReadingRow:
    """One stored reading of a Tag; ``value`` is None for a failed read."""

    received_at: datetime
    source_timestamp: datetime | None
    quality: Quality
    value: TagValue | None
    error_code: str | None


class ReadingQueryRepository:
    """Range queries over ``tag_readings`` and batched deletes for retention.

    Every query filters on ``received_at`` (and ``tag_id`` for history), so the
    ``(tag_id, received_at)`` and ``received_at`` indexes serve it. Poll
    executions are pruned by ``started_at``, which has its own index.
    """

    def __init__(self, engine: Engine) -> None:
        """Create a repository over a migrated database engine."""
        self._engine = engine

    def history(
        self, tag_id: str, start: datetime, end: datetime, limit: int
    ) -> tuple[list[TagReadingRow], bool]:
        """Return up to ``limit`` readings in ``[start, end)``, oldest first.

        The second value is True when more readings exist in the range.
        """
        _require_aware(start, "start")
        _require_aware(end, "end")
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        statement = (
            select(
                tag_readings.c.received_at,
                tag_readings.c.source_timestamp,
                tag_readings.c.quality,
                tag_readings.c.value_type,
                tag_readings.c.numeric_value,
                tag_readings.c.integer_value,
                tag_readings.c.boolean_value,
                tag_readings.c.text_value,
                tag_readings.c.binary_value,
                tag_readings.c.error_code,
            )
            .where(
                tag_readings.c.tag_id == tag_id,
                tag_readings.c.received_at >= start,
                tag_readings.c.received_at < end,
            )
            # event_id breaks ties, so pages are stable for equal timestamps.
            .order_by(tag_readings.c.received_at, tag_readings.c.event_id)
            .limit(limit + 1)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).all()
        truncated = len(rows) > limit
        return [_row(row) for row in rows[:limit]], truncated

    def count_older_than(self, cutoff: datetime) -> int:
        """Return how many readings were received before ``cutoff``."""
        _require_aware(cutoff, "cutoff")
        with self._engine.connect() as connection:
            return int(
                connection.scalar(
                    select(func.count())
                    .select_from(tag_readings)
                    .where(tag_readings.c.received_at < cutoff)
                )
                or 0
            )

    def delete_older_than(
        self,
        cutoff: datetime,
        batch_size: int = 10_000,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Delete readings received before ``cutoff`` and return how many.

        Deletes ``batch_size`` rows per transaction, so locks stay short and a
        large backlog never runs as one huge transaction. ``should_stop`` is
        checked before each batch; when it returns True the delete ends early.
        """
        return _delete_in_batches(
            self._engine,
            tag_readings,
            tag_readings.c.event_id,
            tag_readings.c.received_at,
            cutoff,
            batch_size,
            should_stop,
        )

    def delete_poll_executions_older_than(
        self,
        cutoff: datetime,
        batch_size: int = 10_000,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Delete poll executions started before ``cutoff`` and return how many.

        Batched like ``delete_older_than``.
        """
        return _delete_in_batches(
            self._engine,
            poll_executions,
            poll_executions.c.execution_id,
            poll_executions.c.started_at,
            cutoff,
            batch_size,
            should_stop,
        )


def _delete_in_batches(
    engine: Engine,
    table: Table,
    key: Column[str],
    timestamp: Column[datetime],
    cutoff: datetime,
    batch_size: int,
    should_stop: Callable[[], bool] | None,
) -> int:
    """Delete rows with ``timestamp < cutoff``, one transaction per batch.

    ``DELETE ... WHERE key IN (SELECT key ... LIMIT n)`` works on SQLite and
    PostgreSQL alike.
    """
    _require_aware(cutoff, "cutoff")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")
    batch = select(key).where(timestamp < cutoff).limit(batch_size).scalar_subquery()
    statement = delete(table).where(key.in_(batch))
    total = 0
    while should_stop is None or not should_stop():
        with engine.begin() as connection:
            deleted = connection.execute(statement).rowcount or 0
        total += deleted
        if deleted < batch_size:
            break
    return total


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")


def _row(row: Row[tuple[object, ...]]) -> TagReadingRow:
    values = row._mapping
    return TagReadingRow(
        received_at=values["received_at"],
        source_timestamp=values["source_timestamp"],
        quality=Quality(values["quality"]),
        value=_value(values),
        error_code=values["error_code"],
    )


def _value(values: RowMapping) -> TagValue | None:
    value_type = ValueType(values["value_type"])
    raw = values[_VALUE_COLUMNS[value_type]]
    if raw is None:
        return None
    if value_type is ValueType.NUMERIC:
        return TagValue.numeric(float(raw))
    if value_type is ValueType.INTEGER:
        return TagValue.integer(int(raw))
    if value_type is ValueType.BOOLEAN:
        return TagValue.boolean(bool(raw))
    if value_type is ValueType.TEXT:
        return TagValue.text(str(raw))
    return TagValue.binary(bytes(raw))
