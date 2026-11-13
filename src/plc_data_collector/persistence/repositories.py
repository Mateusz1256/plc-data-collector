"""SQLAlchemy Core repositories for PLC Data Collector persistence."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.engine import CursorResult

from plc_data_collector.domain import (
    PollExecution,
    RuntimeComponentStatus,
    StorageError,
    TagValue,
)
from plc_data_collector.persistence.records import TagReadingRecord
from plc_data_collector.persistence.schema import (
    poll_executions,
    runtime_components,
    tag_readings,
    tags,
)
from plc_data_collector.persistence.sql import insert_for as _insert
from plc_data_collector.runtime import WorkerPollResult


class ReadingRepository:
    """Repository for poll executions and tag readings."""

    def __init__(self, engine: Engine) -> None:
        """Create a reading repository."""
        self._engine = engine

    def save_poll_execution(self, execution: PollExecution) -> None:
        """Persist one poll execution idempotently by execution_id."""
        with self._engine.begin() as connection:
            _save_poll_execution(connection, execution)

    def save_tag_readings(self, records: Iterable[TagReadingRecord]) -> int:
        """Persist tag readings, ignoring duplicate event IDs."""
        inserted = 0
        with self._engine.begin() as connection:
            for record in records:
                result = _save_tag_reading(connection, record)
                inserted += result.rowcount or 0
        return inserted

    def save_poll_results(self, poll_results: Iterable[WorkerPollResult]) -> int:
        """Persist poll executions and readings in one transaction."""
        inserted_readings = 0
        with self._engine.begin() as connection:
            for poll_result in poll_results:
                _save_poll_execution(connection, poll_result.execution)
                for index, result in enumerate(poll_result.results):
                    record = TagReadingRecord(
                        event_id=_event_id(poll_result.execution.execution_id, index),
                        connection_id=poll_result.execution.connection_id,
                        tag_group_id=poll_result.execution.tag_group_id,
                        result=result,
                    )
                    inserted_readings += (
                        _save_tag_reading(connection, record).rowcount or 0
                    )
        return inserted_readings

    def count_tag_readings(self) -> int:
        """Return the number of persisted tag readings."""
        with self._engine.connect() as connection:
            return int(
                connection.scalar(select(func.count()).select_from(tag_readings))
            )

    def count_poll_executions(self) -> int:
        """Return the number of persisted poll executions."""
        with self._engine.connect() as connection:
            return int(
                connection.scalar(select(func.count()).select_from(poll_executions))
            )


class RuntimeStatusRepository:
    """Repository for observable runtime component status."""

    def __init__(self, engine: Engine) -> None:
        """Create a runtime status repository."""
        self._engine = engine

    def upsert_status(self, status: RuntimeComponentStatus) -> None:
        """Insert or update runtime component status."""
        with self._engine.begin() as connection:
            statement = _insert(connection, runtime_components).values(
                component_id=status.component_id,
                state=status.state.value,
                updated_at=status.updated_at,
                message=status.message,
                error_code=status.error_code,
                error_message=status.error_message,
            )
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[runtime_components.c.component_id],
                    set_={
                        "state": statement.excluded.state,
                        "updated_at": statement.excluded.updated_at,
                        "message": statement.excluded.message,
                        "error_code": statement.excluded.error_code,
                        "error_message": statement.excluded.error_message,
                    },
                )
            )


def _reading_values(
    connection: Connection, record: TagReadingRecord
) -> dict[str, object]:
    result = record.result
    value = result.value
    values: dict[str, object] = {
        "event_id": record.event_id,
        "connection_id": record.connection_id,
        "tag_group_id": record.tag_group_id,
        "tag_id": result.tag_id,
        "source_timestamp": result.source_timestamp,
        "received_at": result.received_at,
        "quality": result.quality.value,
        "value_type": _reading_value_type(connection, record),
        "numeric_value": None,
        "integer_value": None,
        "boolean_value": None,
        "text_value": None,
        "binary_value": None,
        "error_code": result.error_code,
        "error_message": result.error_message,
    }
    if value is not None:
        values.update(_tag_value_columns(value))
    return values


def _reading_value_type(connection: Connection, record: TagReadingRecord) -> str:
    value = record.result.value
    if value is not None:
        return value.value_type.value

    configured_value_type = connection.scalar(
        select(tags.c.value_type).where(tags.c.id == record.result.tag_id)
    )
    if isinstance(configured_value_type, str) and configured_value_type:
        return configured_value_type
    raise StorageError(
        "Cannot persist failed tag reading without configured value_type.",
        code="missing_tag_value_type",
        details={"tag_id": record.result.tag_id},
    )


def _save_poll_execution(connection: Connection, execution: PollExecution) -> None:
    statement = _insert(connection, poll_executions).values(
        execution_id=execution.execution_id,
        connection_id=execution.connection_id,
        tag_group_id=execution.tag_group_id,
        status=execution.status.value,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        requested_tags=execution.requested_tags,
        successful_tags=execution.successful_tags,
        failed_tags=execution.failed_tags,
        error_message=execution.error_message,
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[poll_executions.c.execution_id],
            set_={
                "status": statement.excluded.status,
                "finished_at": statement.excluded.finished_at,
                "requested_tags": statement.excluded.requested_tags,
                "successful_tags": statement.excluded.successful_tags,
                "failed_tags": statement.excluded.failed_tags,
                "error_message": statement.excluded.error_message,
            },
        )
    )


def _save_tag_reading(
    connection: Connection,
    record: TagReadingRecord,
) -> CursorResult[object]:
    statement = _insert(connection, tag_readings).values(
        **_reading_values(connection, record),
    )
    return connection.execute(
        statement.on_conflict_do_nothing(
            index_elements=[tag_readings.c.event_id],
        )
    )


def _event_id(execution_id: str, result_index: int) -> str:
    return f"{execution_id}:{result_index}"


def _tag_value_columns(value: TagValue) -> dict[str, object]:
    return {
        "numeric_value": value.numeric_value,
        "integer_value": value.integer_value,
        "boolean_value": value.boolean_value,
        "text_value": value.text_value,
        "binary_value": value.binary_value,
    }
