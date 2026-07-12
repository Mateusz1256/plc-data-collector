from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from plc_gateway.domain import (
    ConfigurationError,
    ConnectionConfig,
    PollExecution,
    PollStatus,
    Quality,
    RuntimeComponentStatus,
    TagConfig,
    TagGroupConfig,
    TagRequest,
    TagResult,
    TagValue,
    ValueType,
    WorkerState,
)


def utc_now() -> datetime:
    return datetime(2026, 7, 12, 10, 0, tzinfo=UTC)


def test_tag_value_exposes_typed_raw_values() -> None:
    assert TagValue.numeric(12).raw == 12.0
    assert TagValue.integer(12).raw == 12
    assert TagValue.boolean(False).raw is False
    assert TagValue.text("ready").raw == "ready"
    assert TagValue.binary(b"\x01").raw == b"\x01"


def test_tag_value_rejects_missing_or_mismatched_payload() -> None:
    with pytest.raises(ConfigurationError, match="exactly one payload"):
        TagValue(value_type=ValueType.TEXT)

    with pytest.raises(ConfigurationError, match="payload must match"):
        TagValue(value_type=ValueType.TEXT, integer_value=1)

    with pytest.raises(ConfigurationError, match="cannot be booleans"):
        TagValue.integer(True)


def test_tag_group_rejects_invalid_interval() -> None:
    with pytest.raises(ConfigurationError, match="interval_ms"):
        TagGroupConfig(
            id="fast",
            connection_id="opcua_demo",
            interval_ms=0,
        )


def test_tag_config_rejects_empty_address() -> None:
    with pytest.raises(ConfigurationError, match="address"):
        TagConfig(
            id="temperature",
            tag_group_id="fast",
            name="Temperature",
            address=" ",
            value_type=ValueType.NUMERIC,
        )


def test_tag_request_rejects_empty_address() -> None:
    with pytest.raises(ConfigurationError, match="address"):
        TagRequest(
            tag_id="temperature",
            address="",
            value_type=ValueType.NUMERIC,
        )


def test_tag_result_represents_success_and_failure() -> None:
    timestamp = utc_now()

    success = TagResult.success(
        tag_id="temperature",
        value=TagValue.numeric(20.5),
        source_timestamp=timestamp,
        received_at=timestamp,
    )
    failure = TagResult.failure(
        tag_id="pressure",
        error_code="timeout",
        error_message="Read timed out",
        received_at=timestamp,
    )

    assert success.is_success is True
    assert success.value == TagValue.numeric(20.5)
    assert success.error_code is None
    assert failure.is_success is False
    assert failure.quality is Quality.BAD
    assert failure.error_code == "timeout"


def test_tag_result_rejects_invalid_success_and_error_shapes() -> None:
    timestamp = utc_now()

    with pytest.raises(ConfigurationError, match="Successful"):
        TagResult.success(
            tag_id="temperature",
            value=TagValue.numeric(20.5),
            source_timestamp=timestamp,
            received_at=timestamp,
            quality=Quality.BAD,
        )

    with pytest.raises(ConfigurationError, match="bad quality"):
        TagResult(
            tag_id="temperature",
            quality=Quality.GOOD,
            received_at=timestamp,
            error_code="timeout",
            error_message="Read timed out",
        )


def test_timestamps_must_be_timezone_aware_and_are_normalized_to_utc() -> None:
    local_time = datetime(
        2026,
        7,
        12,
        12,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )

    result = TagResult.success(
        tag_id="temperature",
        value=TagValue.numeric(20.5),
        source_timestamp=local_time,
        received_at=local_time,
    )

    assert result.received_at.tzinfo is UTC
    assert result.received_at.hour == 10
    with pytest.raises(ConfigurationError, match="timezone-aware"):
        TagResult.success(
            tag_id="temperature",
            value=TagValue.numeric(20.5),
            source_timestamp=datetime(2026, 7, 12, 10, 0),
            received_at=utc_now(),
        )


def test_poll_execution_validates_counts_and_finished_time() -> None:
    started_at = utc_now()

    execution = PollExecution(
        execution_id="poll-1",
        connection_id="opcua_demo",
        tag_group_id="fast",
        status=PollStatus.SUCCESS,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=10),
        requested_tags=2,
        successful_tags=2,
    )

    assert execution.status is PollStatus.SUCCESS
    with pytest.raises(ConfigurationError, match="exceed"):
        PollExecution(
            execution_id="poll-2",
            connection_id="opcua_demo",
            tag_group_id="fast",
            status=PollStatus.PARTIAL_FAILURE,
            started_at=started_at,
            finished_at=started_at + timedelta(milliseconds=10),
            requested_tags=1,
            successful_tags=1,
            failed_tags=1,
        )


def test_connection_and_runtime_status_validate_common_fields() -> None:
    connection = ConnectionConfig(
        id=" opcua_demo ",
        protocol=" opcua ",
        endpoint=" opc.tcp://127.0.0.1:4840 ",
    )
    status = RuntimeComponentStatus(
        component_id="worker:opcua_demo",
        state=WorkerState.RUNNING,
        updated_at=utc_now(),
    )

    assert connection.id == "opcua_demo"
    assert connection.protocol == "opcua"
    assert status.state is WorkerState.RUNNING
