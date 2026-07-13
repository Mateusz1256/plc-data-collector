"""SQLAlchemy Core repositories for PLC Gateway persistence."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult

from plc_gateway.domain import (
    ConnectionConfig,
    PollExecution,
    RuntimeComponentStatus,
    TagConfig,
    TagGroupConfig,
    TagValue,
)
from plc_gateway.persistence.records import TagReadingRecord
from plc_gateway.persistence.schema import (
    connections,
    poll_executions,
    runtime_components,
    tag_groups,
    tag_readings,
    tags,
)
from plc_gateway.runtime import WorkerPollResult


class ConfigurationRepository:
    """Repository for persisted configuration tables."""

    def __init__(self, engine: Engine) -> None:
        """Create a configuration repository."""
        self._engine = engine

    def upsert_connections(self, configs: Iterable[ConnectionConfig]) -> None:
        """Insert or update connection configurations."""
        with self._engine.begin() as connection:
            for config in configs:
                statement = sqlite_insert(connections).values(
                    id=config.id,
                    protocol=config.protocol,
                    endpoint=config.endpoint,
                    enabled=config.enabled,
                    timeout_ms=config.timeout_ms,
                    protocol_options=dict(config.protocol_options),
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[connections.c.id],
                        set_={
                            "protocol": statement.excluded.protocol,
                            "endpoint": statement.excluded.endpoint,
                            "enabled": statement.excluded.enabled,
                            "timeout_ms": statement.excluded.timeout_ms,
                            "protocol_options": statement.excluded.protocol_options,
                        },
                    )
                )

    def upsert_tag_groups(self, configs: Iterable[TagGroupConfig]) -> None:
        """Insert or update tag group configurations."""
        with self._engine.begin() as connection:
            for config in configs:
                statement = sqlite_insert(tag_groups).values(
                    id=config.id,
                    connection_id=config.connection_id,
                    interval_ms=config.interval_ms,
                    timeout_ms=config.timeout_ms,
                    overlap_policy=config.overlap_policy.value,
                    enabled=config.enabled,
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[tag_groups.c.id],
                        set_={
                            "connection_id": statement.excluded.connection_id,
                            "interval_ms": statement.excluded.interval_ms,
                            "timeout_ms": statement.excluded.timeout_ms,
                            "overlap_policy": statement.excluded.overlap_policy,
                            "enabled": statement.excluded.enabled,
                        },
                    )
                )

    def upsert_tags(self, configs: Iterable[TagConfig]) -> None:
        """Insert or update tag configurations."""
        with self._engine.begin() as connection:
            for config in configs:
                statement = sqlite_insert(tags).values(
                    id=config.id,
                    tag_group_id=config.tag_group_id,
                    name=config.name,
                    address=config.address,
                    value_type=config.value_type.value,
                    enabled=config.enabled,
                    metadata_json=dict(config.metadata),
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[tags.c.id],
                        set_={
                            "tag_group_id": statement.excluded.tag_group_id,
                            "name": statement.excluded.name,
                            "address": statement.excluded.address,
                            "value_type": statement.excluded.value_type,
                            "enabled": statement.excluded.enabled,
                            "metadata_json": statement.excluded.metadata_json,
                        },
                    )
                )


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
            statement = sqlite_insert(runtime_components).values(
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


def _reading_values(record: TagReadingRecord) -> dict[str, object]:
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
        "value_type": value.value_type.value if value is not None else None,
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


def _save_poll_execution(connection: Connection, execution: PollExecution) -> None:
    statement = sqlite_insert(poll_executions).values(
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
    statement = sqlite_insert(tag_readings).values(
        **_reading_values(record),
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
