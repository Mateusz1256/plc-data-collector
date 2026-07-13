"""Runtime orchestration primitives."""

from plc_gateway.runtime.connection_worker import (
    ConnectionWorker,
    WorkerPollResult,
    WorkerRuntimeMetrics,
)
from plc_gateway.runtime.reading_queue import (
    ReadingQueue,
    ReadingQueueMetrics,
    ReadingQueueShutdown,
)
from plc_gateway.runtime.retry import (
    ErrorClass,
    RetryOutcome,
    RetryPolicy,
    classify_error,
)
from plc_gateway.runtime.scheduler import (
    PollScheduler,
    PollSchedulerSnapshot,
    PollScheduleState,
)

__all__ = [
    "ConnectionWorker",
    "ErrorClass",
    "PollScheduleState",
    "PollScheduler",
    "PollSchedulerSnapshot",
    "ReadingQueue",
    "ReadingQueueMetrics",
    "ReadingQueueShutdown",
    "RetryOutcome",
    "RetryPolicy",
    "WorkerPollResult",
    "WorkerRuntimeMetrics",
    "classify_error",
]
