"""Panel pages for M-Bus Connections: Scan bus and Read meter.

Both use the Connection's **saved** settings and take the bus the way every
M-Bus Panel action does (``borrow_bus``): the running worker's bus when its
endpoint and baud rate are the same, otherwise a temporary bus while the
Connection does not run, otherwise a refusal with its own message. A scan
runs in the background (``host.mbus_scans``) and the page polls its progress
every second until it finishes. Read meter reads one meter once; choosing
records and saving them happen on the kept reading, never on the bus.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.mbus_access import BusAccessRefused, borrow_bus
from plc_data_collector.app.mbus_scan import ScanJob, ScanKind
from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import ConfigurationError, ConnectionConfig, TagConfig
from plc_data_collector.panel import audit, auth
from plc_data_collector.panel.app import AdminRequired, PanelStores, SameSiteRequired
from plc_data_collector.panel.host import CollectorHost
from plc_data_collector.panel.mbus_read import (
    EXPIRED_MESSAGE,
    MAX_SAVE_ROWS,
    ReadingStore,
    RecordRow,
    StoredReading,
    check_save,
    default_prefix,
    meter_id_of,
    parse_shown_ids,
    record_rows,
    scan_rows,
    shown_ids_text,
    status_text,
)
from plc_data_collector.panel.mbus_tag_form import group_battery_warning
from plc_data_collector.panel.rendering import flash, plural, render
from plc_data_collector.panel.routes_connections import SAVED_MESSAGE
from plc_data_collector.panel.tag_forms import group_choices
from plc_data_collector.protocols.mbus_bus import (
    BusTransportError,
    MeterGarbled,
    MeterNoReply,
)
from plc_data_collector.protocols.mbus_tags import (
    MBUS_PROTOCOL,
    canonical_meter_address,
    display_meter_address,
    parse_meter_address,
)

NOT_FOUND_MESSAGE = "This connection doesn't exist or isn't an M-Bus connection."
KIND_MESSAGE = "Choose a primary or a secondary scan."
METER_REQUIRED_MESSAGE = "Enter the meter address."
NO_REPLY_MESSAGE = (
    "Meter {meter} did not answer. Check the address, the wiring and the baud "
    "rate (mbus_no_reply)."
)
GARBLED_MESSAGE = (
    "Meter {meter} sent a garbled reply. Two meters may share the address, or "
    "the line is noisy (mbus_garbled_reply)."
)
IDS_TAKEN_MESSAGE = "Some IDs were taken meanwhile. Check the names and save again."
# Per row: the tick and the unit; plus the page's own fields.
SAVE_MAX_FIELDS = 2 * MAX_SAVE_ROWS + 1000
_KINDS: tuple[ScanKind, ...] = ("primary", "secondary")


def create_mbus_router() -> APIRouter:
    """Create the Scan bus and Read meter routes; include them under ``/panel``."""
    router = APIRouter()
    readings = ReadingStore()

    # Scan bus

    @router.get(
        "/connections/{connection_id}/mbus/scan",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    async def scan_page(
        request: Request, connection_id: str, stores: PanelStores
    ) -> HTMLResponse:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request)
        return await _scan_page(request, stores, connection)

    @router.post(
        "/connections/{connection_id}/mbus/scan",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def start_scan(
        request: Request, connection_id: str, stores: PanelStores
    ) -> Response:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request)
        data = await _form_data(request)
        kind = next((k for k in _KINDS if k == data.get("kind")), None)
        if kind is None:
            return await _scan_page(
                request,
                stores,
                connection,
                error=KIND_MESSAGE,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            _host(request).mbus_scans.start(connection, kind)
        except BusAccessRefused as error:
            return await _scan_page(
                request,
                stores,
                connection,
                error=error.message,
                status_code=status.HTTP_409_CONFLICT,
            )
        except ConfigurationError as error:
            return await _scan_page(
                request,
                stores,
                connection,
                error=error.message,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return _see_other(_scan_url(connection_id))

    @router.get(
        "/connections/{connection_id}/mbus/scan/progress",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    async def scan_progress(
        request: Request, connection_id: str, stores: PanelStores
    ) -> HTMLResponse:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request)
        context = await _scan_context(stores, connection, _host(request))
        return render(request, "_mbus_scan_progress.html", context)

    @router.post(
        "/connections/{connection_id}/mbus/scan/cancel",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def cancel_scan(
        request: Request, connection_id: str, stores: PanelStores
    ) -> Response:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request)
        job = _host(request).mbus_scans.get(connection_id)
        if job is not None:
            job.cancel()
        return _see_other(_scan_url(connection_id))

    # Read meter

    @router.get(
        "/connections/{connection_id}/mbus/read",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    async def read_page(
        request: Request, connection_id: str, stores: PanelStores, meter: str = ""
    ) -> HTMLResponse:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request)
        return await _read_page(request, stores, connection, meter=meter.strip())

    @router.post(
        "/connections/{connection_id}/mbus/read",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def read_meter(
        request: Request, connection_id: str, stores: PanelStores
    ) -> HTMLResponse:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request)
        typed = (await _form_data(request)).get("meter", "").strip()

        async def refused(message: str, code: int) -> HTMLResponse:
            return await _read_page(
                request,
                stores,
                connection,
                meter=typed,
                error=message,
                status_code=code,
            )

        if not typed:
            return await refused(METER_REQUIRED_MESSAGE, status.HTTP_400_BAD_REQUEST)
        try:
            address = parse_meter_address(typed)
        except ConfigurationError as error:
            return await refused(error.message, status.HTTP_400_BAD_REQUEST)
        display = display_meter_address(address)
        try:
            async with (
                borrow_bus(_host(request), connection) as bus,
                bus.turn() as turn,
            ):
                reading = await turn.read_meter(address)
        except BusAccessRefused as error:
            return await refused(error.message, status.HTTP_409_CONFLICT)
        except MeterNoReply:
            return await refused(
                NO_REPLY_MESSAGE.format(meter=display), status.HTTP_200_OK
            )
        except MeterGarbled:
            return await refused(
                GARBLED_MESSAGE.format(meter=display), status.HTTP_200_OK
            )
        except BusTransportError as error:
            return await refused(f"{error.message} ({error.code})", status.HTTP_200_OK)
        except ConfigurationError as error:
            return await refused(error.message, status.HTTP_400_BAD_REQUEST)
        meter = canonical_meter_address(address)
        token, stored = readings.save(connection_id, meter, reading)
        prefix = default_prefix(meter, reading.header)
        rows = await _rows(stores, connection, stored, prefix)
        return await _read_page(
            request,
            stores,
            connection,
            meter=meter,
            token=token,
            stored=stored,
            prefix=prefix,
            rows=rows,
        )

    @router.post(
        "/connections/{connection_id}/mbus/read/save",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def save_records(
        request: Request, connection_id: str, stores: PanelStores
    ) -> Response:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request)
        posted = await request.form(max_fields=SAVE_MAX_FIELDS)
        fields = {k: v for k, v in posted.items() if isinstance(v, str)}
        selected = [v for v in posted.getlist("select") if isinstance(v, str)]
        token = fields.get("token", "")
        stored = readings.load(token, connection_id)
        if stored is None:
            return await _read_page(
                request,
                stores,
                connection,
                error=EXPIRED_MESSAGE,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        prefix = fields.get("prefix", "").strip() or default_prefix(
            stored.meter, stored.reading.header
        )
        group_id = fields.get("tag_group", "")
        units = {
            key[5:]: value for key, value in fields.items() if key.startswith("unit_")
        }
        rows = await _rows(stores, connection, stored, prefix)
        kept = stored  # narrowed, for the helper below

        async def show(
            shown: list[RecordRow],
            *,
            error: str | None = None,
            problems: dict[int, str] | None = None,
            status_code: int = status.HTTP_200_OK,
        ) -> HTMLResponse:
            return await _read_page(
                request,
                stores,
                connection,
                meter=kept.meter,
                token=token,
                stored=kept,
                prefix=prefix,
                rows=_with_choices(shown, selected, units),
                group_id=group_id,
                error=error,
                problems=problems,
                status_code=status_code,
            )

        if fields.get("action") != "save":
            return await show(rows)
        config = await _load_config(stores)
        check = check_save(
            rows,
            selected=selected,
            units=units,
            shown_ids=parse_shown_ids(fields.get("shown_ids", "")),
            group_id=group_id,
            group_ids=[gid for gid, _ in group_choices(config, connection_id)],
            meter=stored.meter,
            meter_id=meter_id_of(stored.reading.header),
        )
        if check.tags is None:
            return await show(
                rows,
                error=check.error,
                problems=check.problems,
                status_code=(
                    status.HTTP_409_CONFLICT
                    if check.conflict
                    else status.HTTP_400_BAD_REQUEST
                ),
            )
        try:
            await asyncio.to_thread(stores.config.create_tags, check.tags)
        except ConfigurationError as error:
            return await show(
                await _rows(stores, connection, stored, prefix),
                error=(
                    IDS_TAKEN_MESSAGE if error.code == "duplicate_id" else error.message
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        await _audit_created(stores, check.tags)
        readings.delete(token)
        warning = group_battery_warning(
            await _load_config(stores), connection, group_id
        )
        message = f"Added {plural(len(check.tags), 'tag')}. {SAVED_MESSAGE}"
        flash(request, f"{message} {warning}" if warning else message)
        return _see_other(f"/panel/connections/{connection_id}")

    return router


# Scan pages


async def _scan_context(
    stores: Stores, connection: ConnectionConfig, host: CollectorHost
) -> dict[str, object]:
    job: ScanJob | None = host.mbus_scans.get(connection.id)
    rows = []
    if job is not None and job.progress.finished:
        rows = scan_rows(job.meters, await _connection_tags(stores, connection))
    return {
        "connection_id": connection.id,
        "job": job,
        "progress": None if job is None else job.progress,
        "notes": () if job is None else job.notes,
        "meters": rows,
        "progress_url": f"{_scan_url(connection.id)}/progress",
        "cancel_url": f"{_scan_url(connection.id)}/cancel",
        "read_url": _read_url(connection.id),
    }


async def _scan_page(
    request: Request,
    stores: Stores,
    connection: ConnectionConfig,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    context = await _scan_context(stores, connection, _host(request))
    job = context["job"]
    context.update(
        {
            "nav": "connections",
            "title": "Scan bus",
            "connection_name": connection.name,
            "scan_url": _scan_url(connection.id),
            "running": isinstance(job, ScanJob) and not job.progress.finished,
            "kind": job.kind if isinstance(job, ScanJob) else "secondary",
            "error": error,
        }
    )
    return render(request, "mbus_scan.html", context, status_code=status_code)


# Read meter pages


async def _rows(
    stores: Stores, connection: ConnectionConfig, stored: StoredReading, prefix: str
) -> list[RecordRow]:
    existing = await _connection_tags(stores, connection)
    taken = await asyncio.to_thread(stores.config.all_tag_ids)
    return record_rows(
        stored.reading,
        existing,
        prefix,
        meter=stored.meter,
        connection_id=connection.id,
        taken_ids=taken,
    )


def _with_choices(
    rows: list[RecordRow], selected: list[str], units: dict[str, str]
) -> list[RecordRow]:
    """The rows with the posted ticks and units, to show them again."""
    ticked = set(selected)
    return [
        replace(
            row,
            selected=str(row.index) in ticked,
            unit=(
                units[str(row.index)]
                if units.get(str(row.index)) in row.units
                else row.unit
            ),
        )
        if row.supported
        else row
        for row in rows
    ]


async def _read_page(
    request: Request,
    stores: Stores,
    connection: ConnectionConfig,
    *,
    meter: str = "",
    token: str | None = None,
    stored: StoredReading | None = None,
    prefix: str = "",
    rows: list[RecordRow] | None = None,
    group_id: str = "",
    error: str | None = None,
    problems: dict[int, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    config = await _load_config(stores)
    choices = group_choices(config, connection.id)
    header = None if stored is None else stored.reading.header
    context: dict[str, object] = {
        "nav": "connections",
        "title": "Read meter",
        "connection_id": connection.id,
        "connection_name": connection.name,
        "read_url": _read_url(connection.id),
        "meter": meter,
        "error": error,
        "token": token,
        "header": header,
        "status_text": None if header is None else status_text(header),
        "meter_display": (
            display_meter_address(parse_meter_address(stored.meter))
            if stored is not None
            else ""
        ),
        "prefix": prefix,
        "rows": rows or [],
        "shown_ids": shown_ids_text(rows or []),
        "problems": problems or {},
        "group_choices": choices,
        "selected_group": group_id if group_id else (choices[0][0] if choices else ""),
    }
    return render(request, "mbus_read.html", context, status_code=status_code)


# Helpers


async def _audit_created(stores: Stores, tags: list[TagConfig]) -> None:
    for tag in tags:
        await asyncio.to_thread(
            audit.configuration_changed, stores, "tag", tag.id, "created"
        )


async def _connection(stores: Stores, connection_id: str) -> ConnectionConfig | None:
    connection = await asyncio.to_thread(stores.config.get_connection, connection_id)
    if connection is None or connection.protocol != MBUS_PROTOCOL:
        return None
    return connection


async def _connection_tags(
    stores: Stores, connection: ConnectionConfig
) -> list[TagConfig]:
    config = await _load_config(stores)
    groups = {g.id for g in config.tag_groups if g.connection_id == connection.id}
    return [tag for tag in config.tags if tag.tag_group_id in groups]


async def _load_config(stores: Stores) -> CollectorConfig:
    config, _ = await asyncio.to_thread(stores.config.load_active)
    return config


async def _form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _host(request: Request) -> CollectorHost:
    return auth.web_options(request).host


def _scan_url(connection_id: str) -> str:
    return f"/panel/connections/{connection_id}/mbus/scan"


def _read_url(connection_id: str) -> str:
    return f"/panel/connections/{connection_id}/mbus/read"


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _not_found(request: Request) -> HTMLResponse:
    return render(
        request,
        "base.html",
        {"nav": "connections", "title": "Not found", "message": NOT_FOUND_MESSAGE},
        status_code=status.HTTP_404_NOT_FOUND,
    )
