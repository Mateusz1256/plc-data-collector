"""Core framework-independent domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from plc_gateway.domain.exceptions import ConfigurationError


class Quality(StrEnum):
    """Quality of a tag reading."""

    GOOD = "good"
    UNCERTAIN = "uncertain"
    BAD = "bad"


class ValueType(StrEnum):
    """Supported normalized value types."""

    NUMERIC = "numeric"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    TEXT = "text"
    BINARY = "binary"


class WorkerState(StrEnum):
    """Runtime state of a worker or component."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    FAILED = "failed"


class OverlapPolicy(StrEnum):
    """Policy used when a tag group poll overlaps the previous cycle."""

    SKIP = "skip"


class PollStatus(StrEnum):
    """Status of a poll execution."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TagValue:
    """A normalized tag value with an explicit value type."""

    value_type: ValueType
    numeric_value: float | None = None
    integer_value: int | None = None
    boolean_value: bool | None = None
    text_value: str | None = None
    binary_value: bytes | None = None

    @classmethod
    def numeric(cls, value: float | int) -> Self:
        """Create a numeric floating-point tag value."""
        if isinstance(value, bool):
            raise ConfigurationError("Numeric tag values cannot be booleans.")
        return cls(value_type=ValueType.NUMERIC, numeric_value=float(value))

    @classmethod
    def integer(cls, value: int) -> Self:
        """Create an integer tag value."""
        if isinstance(value, bool):
            raise ConfigurationError("Integer tag values cannot be booleans.")
        return cls(value_type=ValueType.INTEGER, integer_value=value)

    @classmethod
    def boolean(cls, value: bool) -> Self:
        """Create a boolean tag value."""
        return cls(value_type=ValueType.BOOLEAN, boolean_value=value)

    @classmethod
    def text(cls, value: str) -> Self:
        """Create a text tag value."""
        return cls(value_type=ValueType.TEXT, text_value=value)

    @classmethod
    def binary(cls, value: bytes) -> Self:
        """Create a binary tag value."""
        return cls(value_type=ValueType.BINARY, binary_value=value)

    @property
    def raw(self) -> float | int | bool | str | bytes:
        """Return the concrete Python value."""
        if self.value_type is ValueType.NUMERIC and self.numeric_value is not None:
            return self.numeric_value
        if self.value_type is ValueType.INTEGER and self.integer_value is not None:
            return self.integer_value
        if self.value_type is ValueType.BOOLEAN and self.boolean_value is not None:
            return self.boolean_value
        if self.value_type is ValueType.TEXT and self.text_value is not None:
            return self.text_value
        if self.value_type is ValueType.BINARY and self.binary_value is not None:
            return self.binary_value
        raise ConfigurationError("Tag value does not contain a matching payload.")

    def __post_init__(self) -> None:
        """Validate that exactly one correctly typed payload is populated."""
        values = {
            ValueType.NUMERIC: self.numeric_value,
            ValueType.INTEGER: self.integer_value,
            ValueType.BOOLEAN: self.boolean_value,
            ValueType.TEXT: self.text_value,
            ValueType.BINARY: self.binary_value,
        }
        populated_count = sum(value is not None for value in values.values())
        if populated_count != 1:
            raise ConfigurationError("TagValue requires exactly one payload.")

        value = values[self.value_type]
        if value is None:
            raise ConfigurationError("TagValue payload must match value_type.")

        if self.value_type is ValueType.NUMERIC:
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ConfigurationError("Numeric tag value must be int or float.")
            object.__setattr__(self, "numeric_value", float(value))
        elif self.value_type is ValueType.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigurationError("Integer tag value must be int.")
        elif self.value_type is ValueType.BOOLEAN:
            if not isinstance(value, bool):
                raise ConfigurationError("Boolean tag value must be bool.")
        elif self.value_type is ValueType.TEXT:
            if not isinstance(value, str):
                raise ConfigurationError("Text tag value must be str.")
        elif self.value_type is ValueType.BINARY and not isinstance(value, bytes):
            raise ConfigurationError("Binary tag value must be bytes.")


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """Domain representation of a configured communication connection."""

    id: str
    protocol: str
    endpoint: str
    enabled: bool = True
    timeout_ms: int = 3000
    protocol_options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate connection invariants."""
        _set_stripped(self, "id", self.id)
        _set_stripped(self, "protocol", self.protocol)
        _set_stripped(self, "endpoint", self.endpoint)
        _require_positive_int(self.timeout_ms, "timeout_ms")
        object.__setattr__(
            self,
            "protocol_options",
            MappingProxyType(dict(self.protocol_options)),
        )


@dataclass(frozen=True, slots=True)
class TagGroupConfig:
    """Domain representation of a cyclic tag group."""

    id: str
    connection_id: str
    interval_ms: int
    timeout_ms: int = 3000
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP
    enabled: bool = True

    def __post_init__(self) -> None:
        """Validate tag group invariants."""
        _set_stripped(self, "id", self.id)
        _set_stripped(self, "connection_id", self.connection_id)
        _require_positive_int(self.interval_ms, "interval_ms")
        _require_positive_int(self.timeout_ms, "timeout_ms")


@dataclass(frozen=True, slots=True)
class TagConfig:
    """Domain representation of a configured tag."""

    id: str
    tag_group_id: str
    name: str
    address: str
    value_type: ValueType
    enabled: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate tag invariants."""
        _set_stripped(self, "id", self.id)
        _set_stripped(self, "tag_group_id", self.tag_group_id)
        _set_stripped(self, "name", self.name)
        _set_stripped(self, "address", self.address)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class TagRequest:
    """A protocol-independent request to read a single tag."""

    tag_id: str
    address: str
    value_type: ValueType
    timeout_ms: int | None = None

    def __post_init__(self) -> None:
        """Validate tag request invariants."""
        _set_stripped(self, "tag_id", self.tag_id)
        _set_stripped(self, "address", self.address)
        if self.timeout_ms is not None:
            _require_positive_int(self.timeout_ms, "timeout_ms")


@dataclass(frozen=True, slots=True)
class TagResult:
    """A protocol-independent result of reading one tag."""

    tag_id: str
    quality: Quality
    received_at: datetime
    source_timestamp: datetime | None = None
    value: TagValue | None = None
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def success(
        cls,
        *,
        tag_id: str,
        value: TagValue,
        source_timestamp: datetime,
        received_at: datetime,
        quality: Quality = Quality.GOOD,
    ) -> Self:
        """Create a successful tag read result."""
        return cls(
            tag_id=tag_id,
            quality=quality,
            received_at=received_at,
            source_timestamp=source_timestamp,
            value=value,
        )

    @classmethod
    def failure(
        cls,
        *,
        tag_id: str,
        error_code: str,
        error_message: str,
        received_at: datetime,
        source_timestamp: datetime | None = None,
        quality: Quality = Quality.BAD,
    ) -> Self:
        """Create a failed tag read result."""
        return cls(
            tag_id=tag_id,
            quality=quality,
            received_at=received_at,
            source_timestamp=source_timestamp,
            error_code=error_code,
            error_message=error_message,
        )

    @property
    def is_success(self) -> bool:
        """Return whether this result represents a successful read."""
        return self.value is not None

    def __post_init__(self) -> None:
        """Validate result invariants and normalize timestamps to UTC."""
        _set_stripped(self, "tag_id", self.tag_id)
        object.__setattr__(
            self,
            "received_at",
            _require_utc(self.received_at, "received_at"),
        )
        if self.source_timestamp is not None:
            object.__setattr__(
                self,
                "source_timestamp",
                _require_utc(self.source_timestamp, "source_timestamp"),
            )

        if self.value is not None:
            if self.quality is Quality.BAD:
                raise ConfigurationError("Successful TagResult cannot be bad quality.")
            if self.error_code is not None or self.error_message is not None:
                raise ConfigurationError("Successful TagResult cannot contain errors.")
            return

        if self.quality is not Quality.BAD:
            raise ConfigurationError("Failed TagResult must have bad quality.")
        _set_optional_stripped(self, "error_code", self.error_code)
        _set_optional_stripped(self, "error_message", self.error_message)
        if self.error_code is None or self.error_message is None:
            raise ConfigurationError("Failed TagResult requires error details.")


@dataclass(frozen=True, slots=True)
class PollExecution:
    """Runtime record for one tag group poll execution."""

    execution_id: str
    connection_id: str
    tag_group_id: str
    status: PollStatus
    started_at: datetime
    finished_at: datetime | None = None
    requested_tags: int = 0
    successful_tags: int = 0
    failed_tags: int = 0
    error_message: str | None = None

    def __post_init__(self) -> None:
        """Validate poll execution invariants."""
        _set_stripped(self, "execution_id", self.execution_id)
        _set_stripped(self, "connection_id", self.connection_id)
        _set_stripped(self, "tag_group_id", self.tag_group_id)
        object.__setattr__(
            self,
            "started_at",
            _require_utc(self.started_at, "started_at"),
        )
        if self.finished_at is not None:
            object.__setattr__(
                self,
                "finished_at",
                _require_utc(self.finished_at, "finished_at"),
            )
            if self.finished_at < self.started_at:
                raise ConfigurationError("finished_at cannot be before started_at.")
        elif self.status is not PollStatus.RUNNING:
            raise ConfigurationError("Finished poll statuses require finished_at.")

        _require_non_negative_int(self.requested_tags, "requested_tags")
        _require_non_negative_int(self.successful_tags, "successful_tags")
        _require_non_negative_int(self.failed_tags, "failed_tags")
        if self.successful_tags + self.failed_tags > self.requested_tags:
            raise ConfigurationError("Poll result counts exceed requested_tags.")
        _set_optional_stripped(self, "error_message", self.error_message)


@dataclass(frozen=True, slots=True)
class RuntimeComponentStatus:
    """Observable runtime status for a component."""

    component_id: str
    state: WorkerState
    updated_at: datetime
    message: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        """Validate runtime status invariants."""
        _set_stripped(self, "component_id", self.component_id)
        object.__setattr__(
            self,
            "updated_at",
            _require_utc(self.updated_at, "updated_at"),
        )
        _set_optional_stripped(self, "message", self.message)
        _set_optional_stripped(self, "error_code", self.error_code)
        _set_optional_stripped(self, "error_message", self.error_message)


def _set_stripped(instance: object, field_name: str, value: str) -> None:
    stripped = value.strip()
    if not stripped:
        raise ConfigurationError(f"{field_name} cannot be empty.")
    object.__setattr__(instance, field_name, stripped)


def _set_optional_stripped(
    instance: object,
    field_name: str,
    value: str | None,
) -> None:
    if value is None:
        return
    stripped = value.strip()
    object.__setattr__(instance, field_name, stripped or None)


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"{field_name} must be a positive integer.")


def _require_non_negative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or value < 0:
        raise ConfigurationError(f"{field_name} must be a non-negative integer.")


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfigurationError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)
