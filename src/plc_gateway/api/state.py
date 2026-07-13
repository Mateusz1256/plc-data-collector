"""Runtime state exposed by the read-only API."""

from __future__ import annotations

from dataclasses import dataclass, field

from plc_gateway.domain import RuntimeComponentStatus
from plc_gateway.persistence import DatabaseWriterMetrics
from plc_gateway.runtime import ReadingQueueMetrics


@dataclass(frozen=True, slots=True)
class RuntimeApiState:
    """Snapshot of runtime state visible through the API."""

    configuration_loaded: bool = True
    storage_available: bool = True
    components: tuple[RuntimeComponentStatus, ...] = ()
    workers: tuple[RuntimeComponentStatus, ...] = ()
    queue_metrics: ReadingQueueMetrics | None = None
    writer_metrics: DatabaseWriterMetrics | None = None
    critical_errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        """Return whether critical runtime prerequisites are available."""
        return (
            self.configuration_loaded
            and self.storage_available
            and not self.critical_errors
        )

    @property
    def degraded(self) -> bool:
        """Return whether non-critical components are unhealthy."""
        return any(component.state.value != "running" for component in self.workers)
