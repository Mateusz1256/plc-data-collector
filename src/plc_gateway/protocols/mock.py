"""Deterministic mock communication driver."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from plc_gateway.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagRequest,
    TagResult,
    TagValue,
    TransientCommunicationError,
    ValueType,
)
from plc_gateway.protocols.driver import DriverCapabilities

MOCK_PROTOCOL: Final = "mock"

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MockTagBehavior:
    """Deterministic behavior configured for one mock tag."""

    value: object | None = None
    sequence: tuple[object, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        """Validate tag behavior shape."""
        has_constant = self.value is not None
        has_sequence = bool(self.sequence)
        has_error = self.error_code is not None or self.error_message is not None
        enabled_modes = sum((has_constant, has_sequence, has_error))
        if enabled_modes > 1:
            raise ConfigurationError(
                "Mock tag behavior must define only one of value, sequence, or error.",
                code="invalid_mock_tag_behavior",
            )
        if has_error and (self.error_code is None or self.error_message is None):
            raise ConfigurationError(
                "Mock tag error behavior requires error_code and error_message.",
                code="invalid_mock_tag_behavior",
            )


@dataclass(frozen=True, slots=True)
class MockDriverConfig:
    """Validated mock driver options."""

    delay_ms: int = 0
    connect_failures_before_success: int = 0
    timeout_on_read: bool = False
    timeout_on_connect: bool = False
    tag_behaviors: Mapping[str, MockTagBehavior] = field(default_factory=dict)

    @classmethod
    def from_protocol_options(
        cls,
        options: Mapping[str, object],
    ) -> MockDriverConfig:
        """Build mock options from a connection's protocol_options mapping."""
        delay_ms = _read_non_negative_int(options, "delay_ms", default=0)
        connect_failures = _read_non_negative_int(
            options,
            "connect_failures_before_success",
            default=0,
        )
        return cls(
            delay_ms=delay_ms,
            connect_failures_before_success=connect_failures,
            timeout_on_read=_read_bool(options, "timeout_on_read", default=False),
            timeout_on_connect=_read_bool(options, "timeout_on_connect", default=False),
            tag_behaviors=_read_tag_behaviors(options),
        )

    def __post_init__(self) -> None:
        """Validate mock driver configuration invariants."""
        _require_non_negative(self.delay_ms, "delay_ms")
        _require_non_negative(
            self.connect_failures_before_success,
            "connect_failures_before_success",
        )


class MockDriver:
    """Deterministic protocol driver for tests and local development."""

    def __init__(
        self,
        connection: ConnectionConfig,
        *,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        """Create a mock driver for one connection."""
        self._connection = connection
        self._config = MockDriverConfig.from_protocol_options(
            connection.protocol_options
        )
        self._clock = _utc_now if clock is None else clock
        self._sleeper = asyncio.sleep if sleeper is None else sleeper
        self._connected = False
        self._remaining_connect_failures = self._config.connect_failures_before_success
        self._sequence_offsets: dict[str, int] = {}

    @property
    def capabilities(self) -> DriverCapabilities:
        """Return mock driver capabilities."""
        return DriverCapabilities(protocol=MOCK_PROTOCOL, supports_batch_read=True)

    async def connect(self) -> None:
        """Open the mock connection or raise a configured transient failure."""
        await self._apply_delay(
            timeout_ms=self._connection.timeout_ms,
            force_timeout=self._config.timeout_on_connect,
            operation="connect",
        )
        if self._remaining_connect_failures > 0:
            self._remaining_connect_failures -= 1
            self._connected = False
            raise TransientCommunicationError(
                "Mock connection failed.",
                code="mock_connection_failed",
                details={"connection_id": self._connection.id},
            )
        self._connected = True

    async def disconnect(self) -> None:
        """Close the mock connection."""
        self._connected = False

    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]:
        """Read configured mock tag values as a deterministic batch."""
        self._require_connected()
        await self._apply_delay(
            timeout_ms=_batch_timeout_ms(tags, self._connection.timeout_ms),
            force_timeout=self._config.timeout_on_read,
            operation="read",
        )
        received_at = self._clock().astimezone(UTC)
        return [self._read_tag(tag, received_at) for tag in tags]

    async def health_check(self) -> bool:
        """Return whether the mock connection is currently connected."""
        return self._connected

    def _read_tag(self, tag: TagRequest, received_at: datetime) -> TagResult:
        behavior = self._config.tag_behaviors.get(tag.tag_id)
        if behavior is not None and behavior.error_code is not None:
            return TagResult.failure(
                tag_id=tag.tag_id,
                error_code=behavior.error_code,
                error_message=behavior.error_message or "Mock tag failure.",
                received_at=received_at,
            )

        raw_value = self._next_raw_value(tag, behavior)
        return TagResult.success(
            tag_id=tag.tag_id,
            value=_tag_value_from_raw(tag.value_type, raw_value),
            source_timestamp=received_at,
            received_at=received_at,
        )

    def _next_raw_value(
        self,
        tag: TagRequest,
        behavior: MockTagBehavior | None,
    ) -> object:
        if behavior is None:
            return _default_raw_value(tag.value_type)
        if behavior.sequence:
            index = self._sequence_offsets.get(tag.tag_id, 0)
            self._sequence_offsets[tag.tag_id] = index + 1
            return behavior.sequence[index % len(behavior.sequence)]
        if behavior.value is not None:
            return behavior.value
        return _default_raw_value(tag.value_type)

    async def _apply_delay(
        self,
        *,
        timeout_ms: int,
        force_timeout: bool,
        operation: str,
    ) -> None:
        if force_timeout or self._config.delay_ms > timeout_ms:
            raise TransientCommunicationError(
                f"Mock {operation} timed out.",
                code="mock_timeout",
                details={
                    "connection_id": self._connection.id,
                    "operation": operation,
                    "timeout_ms": timeout_ms,
                    "delay_ms": self._config.delay_ms,
                },
            )
        if self._config.delay_ms > 0:
            await self._sleeper(self._config.delay_ms / 1000)

    def _require_connected(self) -> None:
        if not self._connected:
            raise TransientCommunicationError(
                "Mock driver is not connected.",
                code="mock_not_connected",
                details={"connection_id": self._connection.id},
            )


def create_mock_driver(connection: ConnectionConfig) -> MockDriver:
    """Create a mock driver for registry registration."""
    return MockDriver(connection)


def _read_tag_behaviors(
    options: Mapping[str, object],
) -> dict[str, MockTagBehavior]:
    tags = options.get("tags", {})
    if not isinstance(tags, Mapping):
        raise ConfigurationError(
            "Mock protocol option 'tags' must be an object.",
            code="invalid_mock_options",
        )

    behaviors: dict[str, MockTagBehavior] = {}
    for tag_id, raw_behavior in tags.items():
        if not isinstance(tag_id, str) or not tag_id.strip():
            raise ConfigurationError(
                "Mock tag behavior keys must be non-empty strings.",
                code="invalid_mock_options",
            )
        if not isinstance(raw_behavior, Mapping):
            raise ConfigurationError(
                f"Mock tag behavior for '{tag_id}' must be an object.",
                code="invalid_mock_options",
                details={"tag_id": tag_id},
            )
        behaviors[tag_id.strip()] = _read_tag_behavior(raw_behavior)
    return behaviors


def _read_tag_behavior(raw_behavior: Mapping[str, object]) -> MockTagBehavior:
    error_code = _optional_str(raw_behavior.get("error_code"), "error_code")
    error_message = _optional_str(raw_behavior.get("error_message"), "error_message")
    sequence = raw_behavior.get("sequence", ())
    if sequence == ():
        sequence_values: tuple[object, ...] = ()
    elif isinstance(sequence, list):
        if not sequence:
            raise ConfigurationError(
                "Mock tag sequence cannot be empty.",
                code="invalid_mock_options",
            )
        sequence_values = tuple(sequence)
    else:
        raise ConfigurationError(
            "Mock tag sequence must be an array.",
            code="invalid_mock_options",
        )

    return MockTagBehavior(
        value=raw_behavior.get("value"),
        sequence=sequence_values,
        error_code=error_code,
        error_message=error_message,
    )


def _tag_value_from_raw(value_type: ValueType, raw_value: object) -> TagValue:
    if value_type is ValueType.NUMERIC:
        if isinstance(raw_value, bool) or not isinstance(raw_value, (float, int)):
            raise ConfigurationError("Mock numeric value must be int or float.")
        return TagValue.numeric(raw_value)
    if value_type is ValueType.INTEGER:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ConfigurationError("Mock integer value must be int.")
        return TagValue.integer(raw_value)
    if value_type is ValueType.BOOLEAN:
        if not isinstance(raw_value, bool):
            raise ConfigurationError("Mock boolean value must be bool.")
        return TagValue.boolean(raw_value)
    if value_type is ValueType.TEXT:
        if not isinstance(raw_value, str):
            raise ConfigurationError("Mock text value must be str.")
        return TagValue.text(raw_value)
    if value_type is ValueType.BINARY:
        if isinstance(raw_value, str):
            return TagValue.binary(raw_value.encode())
        if not isinstance(raw_value, bytes):
            raise ConfigurationError("Mock binary value must be bytes or str.")
        return TagValue.binary(raw_value)
    raise ConfigurationError(f"Unsupported mock value type '{value_type}'.")


def _default_raw_value(value_type: ValueType) -> float | int | bool | str | bytes:
    if value_type is ValueType.NUMERIC:
        return 0.0
    if value_type is ValueType.INTEGER:
        return 0
    if value_type is ValueType.BOOLEAN:
        return False
    if value_type is ValueType.TEXT:
        return ""
    if value_type is ValueType.BINARY:
        return b""
    raise ConfigurationError(f"Unsupported mock value type '{value_type}'.")


def _batch_timeout_ms(tags: Sequence[TagRequest], default_timeout_ms: int) -> int:
    tag_timeouts = [tag.timeout_ms for tag in tags if tag.timeout_ms is not None]
    if not tag_timeouts:
        return default_timeout_ms
    return min(tag_timeouts)


def _read_non_negative_int(
    options: Mapping[str, object],
    key: str,
    *,
    default: int,
) -> int:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(
            f"Mock protocol option '{key}' must be a non-negative integer.",
            code="invalid_mock_options",
            details={"field": key},
        )
    _require_non_negative(value, key)
    return value


def _read_bool(
    options: Mapping[str, object],
    key: str,
    *,
    default: bool,
) -> bool:
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(
            f"Mock protocol option '{key}' must be a boolean.",
            code="invalid_mock_options",
            details={"field": key},
        )
    return value


def _optional_str(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError(
            f"Mock tag field '{field_name}' must be a string.",
            code="invalid_mock_options",
            details={"field": field_name},
        )
    stripped = value.strip()
    return stripped or None


def _require_non_negative(value: int, field_name: str) -> None:
    if value < 0:
        raise ConfigurationError(
            f"Mock protocol option '{field_name}' must be non-negative.",
            code="invalid_mock_options",
            details={"field": field_name},
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)
