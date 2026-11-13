"""Panel Settings page: configuration export and import.

Import is two steps. ``/settings/import`` parses the upload and shows what
would change; the review page carries the raw JSON in a hidden field, and
``/settings/import/confirm`` validates it again from scratch, because the
browser may send anything.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.config_transfer import (
    MAX_IMPORT_BYTES,
    diff_configs,
    export_config,
    export_filename,
    parse_import,
)
from plc_data_collector.domain import ConfigurationError
from plc_data_collector.panel.app import AdminRequired, PanelStores, SameSiteRequired
from plc_data_collector.panel.rendering import flash, render
from plc_data_collector.panel.routes_live import diff_sections, utc_now

IMPORTED_MESSAGE = "Configuration imported. Apply changes to start using it."
TOO_LARGE_MESSAGE = (
    "The file is larger than 5 MB. Check that you chose a configuration file."
)


def _import_error(error: ConfigurationError) -> str:
    if error.code == "import_too_large":
        return TOO_LARGE_MESSAGE
    return f"{error} Fix the file and try again."


def _settings_page(
    request: Request, *, import_error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    return render(
        request,
        "settings.html",
        {"nav": "settings", "title": "Settings", "import_error": import_error},
        status_code=status_code,
    )


def create_settings_router() -> APIRouter:
    """Create the Settings routes; include them with the ``/panel`` prefix."""
    router = APIRouter()

    @router.get("/settings", include_in_schema=False, dependencies=[AdminRequired])
    def settings(request: Request) -> HTMLResponse:
        return _settings_page(request)

    @router.get(
        "/settings/export", include_in_schema=False, dependencies=[AdminRequired]
    )
    def export(stores: PanelStores) -> Response:
        config, _ = stores.config.load_active()
        body = json.dumps(export_config(config), indent=2, ensure_ascii=False)
        filename = export_filename(utc_now())
        return Response(
            body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.post(
        "/settings/import",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def import_review(
        request: Request,
        stores: PanelStores,
        config_file: Annotated[UploadFile, File()],
    ) -> HTMLResponse:
        data = await config_file.read(MAX_IMPORT_BYTES + 1)
        return await asyncio.to_thread(_review, request, stores, data)

    def _review(request: Request, stores: PanelStores, data: bytes) -> HTMLResponse:
        current, _ = stores.config.load_active()
        try:
            new = parse_import(data, current=current)
        except ConfigurationError as error:
            return _settings_page(
                request,
                import_error=_import_error(error),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return render(
            request,
            "import_review.html",
            {
                "nav": "settings",
                "title": "Review import",
                "sections": diff_sections(diff_configs(current, new)),
                "config_json": data.decode("utf-8"),
            },
        )

    @router.post(
        "/settings/import/confirm",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    def import_confirm(
        request: Request,
        stores: PanelStores,
        config_json: Annotated[str, Form()] = "",
    ) -> Response:
        current: CollectorConfig = stores.config.load_active()[0]
        try:
            new = parse_import(config_json.encode("utf-8"), current=current)
        except ConfigurationError as error:
            return _settings_page(
                request,
                import_error=_import_error(error),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        stores.config.replace_all(new)
        flash(request, IMPORTED_MESSAGE)
        return RedirectResponse(
            "/panel/settings", status_code=status.HTTP_303_SEE_OTHER
        )

    return router
