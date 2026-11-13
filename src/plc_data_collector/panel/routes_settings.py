"""Panel Settings page: API keys, data retention, configuration export and import.

A new API key is shown once, in the response to the create POST itself (with
``Cache-Control: no-store``). It is never put in the session, a redirect URL or
a log, so reloading or opening Settings again cannot show it.

Import is two steps. ``/settings/import`` parses the upload and shows what
would change; the review page carries the raw JSON in a hidden field, and
``/settings/import/confirm`` validates it again from scratch, because the
browser may send anything.

Saving retention wakes the supervisor's retention job, so old readings are
deleted right away instead of at the next hourly run.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
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
from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import ConfigurationError
from plc_data_collector.panel import auth
from plc_data_collector.panel.app import AdminRequired, PanelStores, SameSiteRequired
from plc_data_collector.panel.rendering import flash, format_age, render
from plc_data_collector.panel.routes_live import diff_sections, utc_now
from plc_data_collector.persistence import ApiKeyInfo, RetentionSettings

IMPORTED_MESSAGE = "Configuration imported. Apply changes to start using it."
TOO_LARGE_MESSAGE = (
    "The file is larger than 5 MB. Check that you chose a configuration file."
)


DAYS_ERROR = "Enter a number of days from 1 to 3650."
RETENTION_OFF_SAVED = (
    "Retention saved. Readings are kept until you delete them yourself."
)
_DAYS_PATTERN = re.compile(r"[0-9]{1,4}")


def parse_retention_days(raw: str) -> int | None:
    """Return the days typed in the form, or None unless they are 1 to 3650."""
    text = raw.strip()
    if _DAYS_PATTERN.fullmatch(text) is None:
        return None
    days = int(text)
    return days if 1 <= days <= 3650 else None


def _days_text(days: int) -> str:
    return f"{days} day" if days == 1 else f"{days} days"


def _retention_preview(stores: Stores, raw_days: str) -> dict[str, str | None]:
    """Return the preview line for the days typed, or the validation error."""
    days = parse_retention_days(raw_days)
    if days is None:
        return {"error": DAYS_ERROR, "message": None}
    count = stores.readings.count_older_than(utc_now() - timedelta(days=days))
    if count == 0:
        message = f"No readings are older than {_days_text(days)}."
    else:
        noun = "reading" if count == 1 else "readings"
        message = (
            f"About {count:,} {noun} older than {_days_text(days)} "
            "will be deleted after you save."
        )
    return {"error": None, "message": message}


def _retention_form(stores: Stores) -> dict[str, object]:
    settings = stores.settings.retention()
    days = str(settings.days)
    return {
        "enabled": settings.enabled,
        "days": days,
        "preview": _retention_preview(stores, days),
    }


def _import_error(error: ConfigurationError) -> str:
    if error.code == "import_too_large":
        return TOO_LARGE_MESSAGE
    return f"{error} Fix the file and try again."


def _date(value: datetime) -> str:
    return f"{value.day} {value:%b %Y}"


def _last_used(value: datetime | None) -> str:
    if value is None:
        return "Never"
    age = (utc_now() - value).total_seconds()
    if age < 86400:
        return f"{format_age(age)} ago"
    return _date(value)


def _key_row(info: ApiKeyInfo) -> dict[str, object]:
    return {
        "id": info.id,
        "name": info.name,
        "prefix": info.prefix,
        "created": _date(info.created_at),
        "last_used": _last_used(info.last_used_at),
        "revoked": None if info.revoked_at is None else _date(info.revoked_at),
    }


def _settings_page(
    request: Request,
    stores: Stores,
    *,
    import_error: str | None = None,
    key_error: str | None = None,
    key_name: str = "",
    revealed: dict[str, str] | None = None,
    retention: dict[str, object] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    response = render(
        request,
        "settings.html",
        {
            "nav": "settings",
            "title": "Settings",
            "import_error": import_error,
            "key_error": key_error,
            "key_name": key_name,
            "revealed": revealed,
            "api_keys": [_key_row(info) for info in stores.api_keys.list()],
            "retention": retention or _retention_form(stores),
        },
        status_code=status_code,
    )
    if revealed is not None:
        response.headers["Cache-Control"] = "no-store"
    return response


def _key_error(error: ConfigurationError) -> str:
    if error.code == "duplicate_api_key_name":
        return "An active key with that name already exists. Choose another name."
    return "Enter an application name of 1 to 128 characters."


def create_settings_router() -> APIRouter:
    """Create the Settings routes; include them with the ``/panel`` prefix."""
    router = APIRouter()

    @router.get("/settings", include_in_schema=False, dependencies=[AdminRequired])
    def settings(request: Request, stores: PanelStores) -> HTMLResponse:
        return _settings_page(request, stores)

    @router.post(
        "/settings/api-keys",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    def create_api_key(
        request: Request, stores: PanelStores, name: Annotated[str, Form()] = ""
    ) -> HTMLResponse:
        try:
            info, key = stores.api_keys.create(name)
        except ConfigurationError as error:
            return _settings_page(
                request,
                stores,
                key_error=_key_error(error),
                key_name=name,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return _settings_page(request, stores, revealed={"name": info.name, "key": key})

    @router.post(
        "/settings/api-keys/{key_id}/revoke",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    def revoke_api_key(request: Request, stores: PanelStores, key_id: str) -> Response:
        stores.api_keys.revoke(key_id)
        flash(
            request, "API key revoked. Applications using it can no longer read data."
        )
        return RedirectResponse(
            "/panel/settings", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.get(
        "/settings/retention/preview",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    def retention_preview(
        request: Request, stores: PanelStores, days: str = ""
    ) -> HTMLResponse:
        # Always 200: htmx does not swap 4xx responses, and the validation
        # message has to reach the page.
        return render(
            request,
            "_retention.html",
            {"preview": _retention_preview(stores, days)},
        )

    @router.post(
        "/settings/retention",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def save_retention(
        request: Request,
        stores: PanelStores,
        days: Annotated[str, Form()] = "",
        enabled: Annotated[str, Form()] = "",
    ) -> Response:
        turned_on = bool(enabled)
        saved = await asyncio.to_thread(_save_retention, stores, turned_on, days)
        if saved is None:
            return await asyncio.to_thread(
                _settings_page,
                request,
                stores,
                retention={
                    "enabled": True,
                    "days": days,
                    "preview": {"error": DAYS_ERROR, "message": None},
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        # On the event loop: wakes the retention job, which deletes in its
        # own thread, so this response does not wait for the delete.
        auth.web_options(request).host.trigger_retention()
        if saved.enabled:
            flash(
                request,
                "Retention saved. Readings older than "
                f"{_days_text(saved.days)} are being deleted now.",
            )
        else:
            flash(request, RETENTION_OFF_SAVED)
        return RedirectResponse(
            "/panel/settings", status_code=status.HTTP_303_SEE_OTHER
        )

    def _save_retention(
        stores: Stores, enabled: bool, raw_days: str
    ) -> RetentionSettings | None:
        """Store the setting; None when retention is on and the days are invalid.

        Turning retention off with an invalid number keeps the stored days,
        because the days field is hidden while the toggle is off.
        """
        days = parse_retention_days(raw_days)
        if days is None:
            if enabled:
                return None
            days = stores.settings.retention().days
        settings = RetentionSettings(enabled=enabled, days=days)
        stores.settings.set_retention(settings)
        return settings

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
                stores,
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
                stores,
                import_error=_import_error(error),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        stores.config.replace_all(new)
        flash(request, IMPORTED_MESSAGE)
        return RedirectResponse(
            "/panel/settings", status_code=status.HTTP_303_SEE_OTHER
        )

    return router
