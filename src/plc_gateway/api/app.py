"""FastAPI application factory for read-only runtime endpoints."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum

from fastapi import FastAPI, Response, status

from plc_gateway._version import get_version
from plc_gateway.api.state import RuntimeApiState


def create_api_app(state: RuntimeApiState | None = None) -> FastAPI:
    """Create the read-only PLC Gateway API application."""
    app = FastAPI(
        title="PLC Gateway",
        version=get_version(),
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.runtime_api_state = state or RuntimeApiState()

    @app.get("/health/live")
    async def health_live() -> dict[str, object]:
        """Return liveness independent of individual PLC connectivity."""
        return {
            "status": "live",
            "application": "plc-gateway",
            "version": get_version(),
        }

    @app.get("/health/ready")
    async def health_ready(response: Response) -> dict[str, object]:
        """Return readiness for critical application prerequisites."""
        runtime_state = _runtime_state(app)
        if not runtime_state.ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if runtime_state.ready else "not_ready",
            "ready": runtime_state.ready,
            "degraded": runtime_state.degraded,
            "configuration_loaded": runtime_state.configuration_loaded,
            "storage_available": runtime_state.storage_available,
            "critical_errors": list(runtime_state.critical_errors),
        }

    @app.get("/api/runtime/components")
    async def runtime_components() -> dict[str, object]:
        """Return runtime component statuses and runtime metrics."""
        runtime_state = _runtime_state(app)
        return {
            "components": _encode(runtime_state.components),
            "worker_metrics": _encode(runtime_state.worker_metrics),
            "queue": _encode(runtime_state.queue_metrics),
            "writer": _encode(runtime_state.writer_metrics),
        }

    @app.get("/api/runtime/workers")
    async def runtime_workers() -> dict[str, object]:
        """Return observable connection worker states."""
        runtime_state = _runtime_state(app)
        return {
            "workers": _encode(runtime_state.workers),
            "metrics": _encode(runtime_state.worker_metrics),
        }

    @app.get("/api/about")
    async def about() -> dict[str, object]:
        """Return build and license information."""
        return {
            "application": "plc-gateway",
            "version": get_version(),
            "license": {
                "status": "not_selected",
                "name": None,
            },
        }

    return app


def _runtime_state(app: FastAPI) -> RuntimeApiState:
    state = app.state.runtime_api_state
    if not isinstance(state, RuntimeApiState):
        raise RuntimeError("runtime_api_state must be RuntimeApiState.")
    return state


def _encode(value: object) -> object:
    if value is None:
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _encode(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value
