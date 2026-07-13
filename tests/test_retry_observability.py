from __future__ import annotations

import logging

import pytest

from plc_gateway.domain import (
    ConfigurationError,
    PermanentProtocolError,
    StorageError,
    TransientCommunicationError,
)
from plc_gateway.runtime import ErrorClass, RetryPolicy, classify_error
from plc_gateway.runtime.retry import run_with_retry


async def no_sleep(_: float) -> None:
    """Deterministic retry sleeper."""


def test_retry_policy_uses_capped_exponential_backoff_and_jitter() -> None:
    policy = RetryPolicy(
        max_attempts=4,
        initial_delay_s=1.0,
        max_delay_s=3.0,
        multiplier=2.0,
        jitter_ratio=0.25,
    )

    assert policy.delay_for_retry(1, random_value=0.5) == 1.0
    assert policy.delay_for_retry(2, random_value=1.0) == 2.5
    assert policy.delay_for_retry(3, random_value=0.0) == 2.25


def test_error_classification_separates_retryable_errors() -> None:
    assert classify_error(ConfigurationError("bad config")) is ErrorClass.CONFIGURATION
    assert classify_error(PermanentProtocolError("bad address")) is ErrorClass.PERMANENT
    assert (
        classify_error(TransientCommunicationError("timeout")) is ErrorClass.TRANSIENT
    )
    assert classify_error(StorageError("database unavailable")) is ErrorClass.TRANSIENT
    assert classify_error(ValueError("bad value")) is ErrorClass.UNKNOWN


@pytest.mark.asyncio
async def test_run_with_retry_retries_transient_errors_deterministically(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0
    retry_delays: list[float] = []
    logger = logging.getLogger("tests.retry")
    caplog.set_level(logging.INFO, logger="tests.retry")

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TransientCommunicationError("timeout", code="timeout")
        return "ok"

    result, outcome = await run_with_retry(
        operation,
        RetryPolicy(
            max_attempts=3,
            initial_delay_s=0.1,
            max_delay_s=1.0,
            jitter_ratio=0,
        ),
        operation_name="test_operation",
        correlation_id="corr-1",
        component_id="component-1",
        logger=logger,
        sleep=no_sleep,
        on_retry=lambda _error, delay, _retry: retry_delays.append(delay),
    )

    assert result == "ok"
    assert outcome.attempts == 3
    assert outcome.retries == 2
    assert retry_delays == [0.1, 0.2]
    assert {
        getattr(record, "correlation_id", None)
        for record in caplog.records
        if getattr(record, "operation", None) == "test_operation"
    } == {"corr-1"}


@pytest.mark.asyncio
async def test_run_with_retry_does_not_retry_configuration_errors() -> None:
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise ConfigurationError("invalid", code="invalid_config")

    with pytest.raises(ConfigurationError):
        await run_with_retry(
            operation,
            RetryPolicy(max_attempts=3, initial_delay_s=0, max_delay_s=0),
            operation_name="test_operation",
            correlation_id="corr-2",
            component_id="component-1",
            logger=logging.getLogger("tests.retry"),
            sleep=no_sleep,
        )

    assert attempts == 1
