"""FastAPI application factory for read-only runtime endpoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from fastapi import FastAPI, Response, status

from plc_data_collector._version import get_version
from plc_data_collector.api.state import RuntimeApiState
from plc_data_collector.license_info import load_license_report

if TYPE_CHECKING:
    from plc_data_collector.panel.host import CollectorHost

RuntimeStateProvider = Callable[[], RuntimeApiState]

PANEL_SESSION_COOKIE = "pdc_session"
PANEL_SESSION_MAX_AGE_S = 12 * 60 * 60


@dataclass(frozen=True)
class WebOptions:
    """What the app needs to serve the Panel.

    An unset or empty ``admin_password`` turns the Panel off;
    ``session_secret`` signs the session cookie. Neither appears in ``repr``.
    """

    host: CollectorHost
    admin_password: str | None = field(repr=False)
    session_secret: bytes = field(repr=False)


def create_api_app(
    state: RuntimeApiState | RuntimeStateProvider | None = None,
    *,
    web: WebOptions | None = None,
) -> FastAPI:
    """Create the PLC Data Collector API application.

    ``state`` is either a fixed snapshot or a callable that returns a fresh
    snapshot; a callable is invoked on every request. With ``web``, the app
    also serves the Panel at ``/panel`` and redirects ``/`` to it; without
    it, the app serves only the read-only runtime endpoints.
    """
    app = FastAPI(
        title="PLC Data Collector",
        version=get_version(),
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.runtime_api_state = RuntimeApiState() if state is None else state

    @app.get("/health/live")
    async def health_live() -> dict[str, object]:
        """Return liveness independent of individual PLC connectivity."""
        return {
            "status": "live",
            "application": "plc-data-collector",
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
            "spool": _encode(runtime_state.spool_metrics),
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
        license_report = load_license_report()
        return {
            "application": "plc-data-collector",
            "version": license_report.build.version,
            "build": _encode(license_report.build),
            "license": _encode(license_report.project),
            "third_party_notices_file": license_report.third_party_notices_file,
            "dependency_count": len(license_report.dependencies),
        }

    if web is not None:
        _mount_panel(app, web)
    return app


def _mount_panel(app: FastAPI, web: WebOptions) -> None:
    # Imported here so the API without the Panel needs no Jinja or sessions.
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.sessions import SessionMiddleware

    from plc_data_collector.panel.app import (
        DatabaseNotReady,
        create_panel_router,
        cross_site_request_handler,
        database_not_ready_handler,
        login_required_handler,
    )
    from plc_data_collector.panel.auth import CrossSiteRequest, LoginRequired
    from plc_data_collector.panel.routes_connections import (
        create_connections_router,
    )

    app.state.panel_web = web
    # SameSite=Strict is the CSRF defence for the Panel's forms.
    app.add_middleware(
        SessionMiddleware,
        secret_key=web.session_secret.hex(),
        session_cookie=PANEL_SESSION_COOKIE,
        max_age=PANEL_SESSION_MAX_AGE_S,
        same_site="strict",
    )
    app.mount(
        "/panel/static",
        StaticFiles(packages=[("plc_data_collector.panel", "static")]),
        name="panel_static",
    )
    app.include_router(create_panel_router(), prefix="/panel")
    app.include_router(create_connections_router(), prefix="/panel")
    app.add_exception_handler(LoginRequired, login_required_handler)
    app.add_exception_handler(DatabaseNotReady, database_not_ready_handler)
    app.add_exception_handler(CrossSiteRequest, cross_site_request_handler)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/panel/live", status_code=status.HTTP_303_SEE_OTHER)


def _runtime_state(app: FastAPI) -> RuntimeApiState:
    state = app.state.runtime_api_state
    if not isinstance(state, RuntimeApiState) and callable(state):
        state = state()
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
