from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from plc_data_collector.domain import (
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


def test_uncertain_result_keeps_value_code_and_message() -> None:
    timestamp = utc_now()

    result = TagResult.uncertain(
        tag_id="energy",
        value=TagValue.numeric(12.5),
        error_code="mbus_value_rounded",
        error_message="Value was rounded to fit a float.",
        source_timestamp=timestamp,
        received_at=timestamp,
    )

    assert result.quality is Quality.UNCERTAIN
    assert result.value == TagValue.numeric(12.5)
    assert result.error_code == "mbus_value_rounded"
    assert result.error_message == "Value was rounded to fit a float."
    assert result.is_success is True


def test_uncertain_result_without_reason_is_allowed() -> None:
    timestamp = utc_now()

    result = TagResult.success(
        tag_id="energy",
        value=TagValue.numeric(1.0),
        source_timestamp=timestamp,
        received_at=timestamp,
        quality=Quality.UNCERTAIN,
    )

    assert result.error_code is None and result.error_message is None


def test_good_result_with_a_reason_is_refused() -> None:
    timestamp = utc_now()

    with pytest.raises(ConfigurationError, match="good"):
        TagResult(
            tag_id="energy",
            quality=Quality.GOOD,
            received_at=timestamp,
            value=TagValue.numeric(1.0),
            error_code="x",
            error_message="y",
        )


@pytest.mark.parametrize(("error_code", "error_message"), [("x", None), (None, "y")])
def test_uncertain_result_with_half_a_reason_is_refused(
    error_code: str | None, error_message: str | None
) -> None:
    with pytest.raises(ConfigurationError, match="both"):
        TagResult(
            tag_id="energy",
            quality=Quality.UNCERTAIN,
            received_at=utc_now(),
            value=TagValue.numeric(1.0),
            error_code=error_code,
            error_message=error_message,
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


@pytest.mark.parametrize(
    "build",
    [
        lambda v: ConnectionConfig(id="c", protocol="p", endpoint="e", timeout_ms=v),
        lambda v: TagGroupConfig(id="g", connection_id="c", interval_ms=v),
        lambda v: TagGroupConfig(
            id="g", connection_id="c", interval_ms=1, timeout_ms=v
        ),
    ],
)
def test_millisecond_fields_fit_a_database_integer(build: Any) -> None:
    build(2_147_483_647)
    with pytest.raises(ConfigurationError, match="2147483647"):
        build(2_147_483_648)


def test_tag_config_protocol_options_default_and_read_only() -> None:
    tag = TagConfig(
        id="t", tag_group_id="g", name="T", address="hr:1", value_type=ValueType.INTEGER
    )
    assert dict(tag.protocol_options) == {}
    tag2 = TagConfig(
        id="t",
        tag_group_id="g",
        name="T",
        address="hr:1",
        value_type=ValueType.INTEGER,
        protocol_options={"data_type": "int16"},
    )
    with pytest.raises(TypeError):
        tag2.protocol_options["x"] = 1  # type: ignore[index]


def test_tag_request_carries_protocol_options() -> None:
    req = TagRequest(
        tag_id="t",
        address="hr:1",
        value_type=ValueType.INTEGER,
        protocol_options={"data_type": "int16"},
    )
    assert req.protocol_options["data_type"] == "int16"
    with pytest.raises(TypeError):
        req.protocol_options["x"] = 1  # type: ignore[index]
