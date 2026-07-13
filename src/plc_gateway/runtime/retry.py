"""Retry policy and deterministic backoff helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from plc_gateway.domain import (
    ConfigurationError,
    PermanentProtocolError,
    StorageError,
    TransientCommunicationError,
)

Sleeper = Callable[[float], Awaitable[None]]
RandomFloat = Callable[[], float]
RetryCallback = Callable[[Exception, float, int], None]


class ErrorClass(StrEnum):
    """Error class used by retry decisions and logs."""

    CONFIGURATION = "configuration"
    PERMANENT = "permanent"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff policy with capped delay and optional jitter."""

    max_attempts: int = 3
    initial_delay_s: float = 0.1
    max_delay_s: float = 5.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        """Validate retry policy bounds."""
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive.")
        if self.initial_delay_s < 0:
            raise ValueError("initial_delay_s must be non-negative.")
        if self.max_delay_s < 0:
            raise ValueError("max_delay_s must be non-negative.")
        if self.max_delay_s < self.initial_delay_s:
            raise ValueError("max_delay_s must be at least initial_delay_s.")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1.")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1.")

    def delay_for_retry(self, retry_number: int, *, random_value: float = 0.5) -> float:
        """Return capped exponential delay for a 1-based retry number."""
        if retry_number <= 0:
            raise ValueError("retry_number must be positive.")
        if not 0 <= random_value <= 1:
            raise ValueError("random_value must be between 0 and 1.")

        base_delay = self.initial_delay_s * self.multiplier ** (retry_number - 1)
        capped_delay = min(base_delay, self.max_delay_s)
        if capped_delay == 0 or self.jitter_ratio == 0:
            return capped_delay

        jitter_span = capped_delay * self.jitter_ratio
        jitter_offset = (random_value * 2 - 1) * jitter_span
        return max(0.0, min(self.max_delay_s, capped_delay + jitter_offset))


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """Observable retry outcome for an operation."""

    attempts: int
    retries: int
    exhausted: bool
    last_error_class: ErrorClass | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None


def classify_error(error: Exception) -> ErrorClass:
    """Classify errors for retry and observability."""
    if isinstance(error, ConfigurationError):
        return ErrorClass.CONFIGURATION
    if isinstance(error, PermanentProtocolError):
        return ErrorClass.PERMANENT
    if isinstance(error, TransientCommunicationError | StorageError | TimeoutError):
        return ErrorClass.TRANSIENT
    return ErrorClass.UNKNOWN


def is_retryable(error: Exception) -> bool:
    """Return whether the error should be retried by default."""
    return classify_error(error) is ErrorClass.TRANSIENT


async def run_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    operation_name: str,
    correlation_id: str,
    component_id: str,
    logger: logging.Logger,
    sleep: Sleeper | None = None,
    random_float: RandomFloat | None = None,
    on_retry: RetryCallback | None = None,
) -> tuple[T, RetryOutcome]:
    """Run an async operation with retry and structured log context."""
    sleeper = asyncio.sleep if sleep is None else sleep
    jitter_source = _default_random if random_float is None else random_float
    attempts = 0
    retries = 0
    last_error: Exception | None = None

    while attempts < policy.max_attempts:
        attempts += 1
        try:
            result = await operation()
        except Exception as error:
            last_error = error
            error_class = classify_error(error)
            if not is_retryable(error) or attempts >= policy.max_attempts:
                logger.warning(
                    "retry_operation_failed",
                    extra={
                        "operation": operation_name,
                        "correlation_id": correlation_id,
                        "component_id": component_id,
                        "attempt": attempts,
                        "max_attempts": policy.max_attempts,
                        "error_class": error_class.value,
                        "error_code": _error_code(error),
                    },
                )
                raise

            retries += 1
            delay_s = policy.delay_for_retry(
                retries,
                random_value=jitter_source(),
            )
            if on_retry is not None:
                on_retry(error, delay_s, retries)
            logger.info(
                "retry_operation_scheduled",
                extra={
                    "operation": operation_name,
                    "correlation_id": correlation_id,
                    "component_id": component_id,
                    "attempt": attempts,
                    "retry": retries,
                    "delay_s": delay_s,
                    "error_class": error_class.value,
                    "error_code": _error_code(error),
                },
            )
            await sleeper(delay_s)
        else:
            if retries:
                logger.info(
                    "retry_operation_recovered",
                    extra={
                        "operation": operation_name,
                        "correlation_id": correlation_id,
                        "component_id": component_id,
                        "attempt": attempts,
                        "retries": retries,
                    },
                )
            return (
                result,
                RetryOutcome(attempts=attempts, retries=retries, exhausted=False),
            )

    raise RuntimeError("retry loop exited without result") from last_error


def failed_outcome(error: Exception, *, attempts: int, retries: int) -> RetryOutcome:
    """Build a retry outcome for a failed operation."""
    return RetryOutcome(
        attempts=attempts,
        retries=retries,
        exhausted=True,
        last_error_class=classify_error(error),
        last_error_code=_error_code(error),
        last_error_message=str(error) or error.__class__.__name__,
    )


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return error.__class__.__name__


def _default_random() -> float:
    return 0.5
