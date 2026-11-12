"""Domain models and exceptions for PLC Data Collector."""

from plc_data_collector.domain.exceptions import (
    CollectorError,
    ConfigurationError,
    PermanentProtocolError,
    StorageError,
    TransientCommunicationError,
)
from plc_data_collector.domain.models import (
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
    "CollectorError",
    "ConfigurationError",
    "ConnectionConfig",
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
