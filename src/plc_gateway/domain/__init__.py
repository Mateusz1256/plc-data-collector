"""Domain models and exceptions for PLC Collector."""

from plc_gateway.domain.exceptions import (
    ConfigurationError,
    GatewayError,
    PermanentProtocolError,
    StorageError,
    TransientCommunicationError,
)
from plc_gateway.domain.models import (
    ConnectionConfig,
    OverlapPolicy,
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

__all__ = [
    "ConfigurationError",
    "ConnectionConfig",
    "GatewayError",
    "OverlapPolicy",
    "PermanentProtocolError",
    "PollExecution",
    "PollStatus",
    "Quality",
    "RuntimeComponentStatus",
    "StorageError",
    "TagConfig",
    "TagGroupConfig",
    "TagRequest",
    "TagResult",
    "TagValue",
    "TransientCommunicationError",
    "ValueType",
    "WorkerState",
]
