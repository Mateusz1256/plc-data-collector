"""Panel router factory: sign-in, sign-out and the page shell."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from plc_data_collector.app.stores import Stores
from plc_data_collector.panel import auth
from plc_data_collector.panel.auth import (
    CrossSiteRequest,
    LoginRequired,
    LoginThrottle,
)
from plc_data_collector.panel.rendering import render

DATABASE_NOT_READY_MESSAGE = "Starting: waiting for the database."


class DatabaseNotReady(Exception):
    """Raised by ``panel_stores`` while the host has no database stores yet."""


def panel_stores(request: Request) -> Stores:
    """FastAPI dependency: return the host's stores or show the starting page."""
    stores = auth.web_options(request).host.stores
    if stores is None:
        raise DatabaseNotReady
    return stores


AdminRequired = Depends(auth.require_admin)
# Every Panel POST uses this, on top of the SameSite=Strict cookie.
SameSiteRequired = Depends(auth.require_same_site)
PanelStores = Annotated[Stores, Depends(panel_stores)]


def create_panel_router(throttle: LoginThrottle | None = None) -> APIRouter:
    """Create the Panel router; include it with the ``/panel`` prefix.

    The app must set ``app.state.panel_web`` to its ``WebOptions``, use a
    session middleware, and register ``login_required_handler`` and
    ``database_not_ready_handler``.
    """
    login_throttle = throttle or LoginThrottle(max_failures=5, window_s=300)
    router = APIRouter()

    @router.get("", include_in_schema=False)
    @router.get("/", include_in_schema=False)
    async def panel_home() -> RedirectResponse:
        return RedirectResponse(
            auth.DEFAULT_NEXT, status_code=status.HTTP_303_SEE_OTHER
        )

    @router.get("/login", include_in_schema=False)
    async def login_page(request: Request, next: str | None = None) -> HTMLResponse:
        return _login(request, next_path=auth.safe_next(next))

    @router.post("/login", include_in_schema=False, dependencies=[SameSiteRequired])
    async def login(
        request: Request,
        password: Annotated[str, Form()] = "",
        next: Annotated[str, Form()] = "",
    ) -> Response:
        next_path = auth.safe_next(next)
        options = auth.web_options(request)
        if not auth.panel_enabled(options):
            return _login(request, next_path=next_path, status_code=403)
        client = auth.client_address(request)
        if login_throttle.blocked(client):
            return _login(
                request, next_path=next_path, error="throttled", status_code=429
            )
        if not auth.password_matches(options.admin_password, password):
            login_throttle.record_failure(client)
            await auth.failure_sleep(auth.FAILURE_DELAY_S)
            return _login(
                request, next_path=next_path, error="wrong_password", status_code=401
            )
        login_throttle.reset(client)
        auth.sign_in(request)
        return RedirectResponse(next_path, status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/sign-out", include_in_schema=False, dependencies=[SameSiteRequired])
    async def sign_out(request: Request) -> RedirectResponse:
        auth.sign_out(request)
        return RedirectResponse(auth.LOGIN_PATH, status_code=status.HTTP_303_SEE_OTHER)

    return router


def _login(
    request: Request,
    *,
    next_path: str,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    options = auth.web_options(request)
    return render(
        request,
        "login.html",
        {
            "next": next_path,
            "error": error,
            "panel_off": not auth.panel_enabled(options),
        },
        status_code=status_code,
    )


async def login_required_handler(request: Request, error: Exception) -> Response:
    """Send a signed-out browser to the sign-in page.

    htmx requests get ``HX-Redirect``, so htmx loads the sign-in page as a
    whole page instead of swapping it into a fragment.
    """
    if not isinstance(error, LoginRequired):
        raise error
    if request.headers.get("HX-Request") == "true":
        return Response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"HX-Redirect": error.login_url},
        )
    return RedirectResponse(error.login_url, status_code=status.HTTP_303_SEE_OTHER)


async def cross_site_request_handler(request: Request, error: Exception) -> Response:
    """Refuse a Panel form sent from another site."""
    if not isinstance(error, CrossSiteRequest):
        raise error
    return PlainTextResponse(
        "This form was sent from another site, so it was refused. "
        "Open the Panel directly and try again.",
        status_code=status.HTTP_403_FORBIDDEN,
    )


async def database_not_ready_handler(request: Request, error: Exception) -> Response:
    """Show the starting page while the database is not ready."""
    return render(
        request,
        "base.html",
        {
            "title": "Starting",
            "message": DATABASE_NOT_READY_MESSAGE,
            "show_nav": False,
        },
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
