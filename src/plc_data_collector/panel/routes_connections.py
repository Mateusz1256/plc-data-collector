"""Panel pages for Connections: list, add, edit, delete and Test connection."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.connection_test import probe_connection
from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    PollExecution,
    PollStatus,
    Quality,
    RuntimeComponentStatus,
    TagConfig,
    TagGroupConfig,
)
from plc_data_collector.panel import auth
from plc_data_collector.panel.app import AdminRequired, PanelStores, SameSiteRequired
from plc_data_collector.panel.host import CollectorHost
from plc_data_collector.panel.protocol_forms import (
    DEFAULT_TIMEOUT_MS,
    ID_HELP,
    FormErrors,
    ProtocolForm,
    connection_from_form,
    display_endpoint,
    form_for,
    picker_forms,
    validate_new_id,
)
from plc_data_collector.panel.rendering import (
    StateBadge,
    flash,
    format_age,
    plural,
    render,
    state_badge,
)

SAVED_MESSAGE = "Saved changes take effect after you apply them from the banner."
DELETED_MESSAGE = (
    "Connection deleted. Saved changes take effect after you apply them "
    "from the banner."
)
DUPLICATE_ID_ERROR = "A connection with this ID already exists. Choose another ID."
PICK_PROTOCOL_ERROR = "Choose a protocol that this version supports."
NOT_FOUND_MESSAGE = "This connection doesn't exist or was deleted."
WORKER_PREFIX = "connection-worker:"


@dataclass(frozen=True)
class ConnectionRow:
    """One row of the Connections list."""

    connection: ConnectionConfig
    protocol_label: str
    address: str
    badge: StateBadge
    detail: str | None
    collected: str
    last_poll: str
    last_poll_failed: bool

    @property
    def row_class(self) -> str:
        """Return the row highlight class; only failures are highlighted."""
        return "row-failed" if self.badge.kind == "failed" else ""


def create_connections_router() -> APIRouter:
    """Create the Connections routes; include them with the ``/panel`` prefix."""
    router = APIRouter()

    @router.get("/connections", include_in_schema=False, dependencies=[AdminRequired])
    async def list_connections(request: Request, stores: PanelStores) -> HTMLResponse:
        return _list_page(request, stores.config.load_active()[0])

    @router.get(
        "/connections/new", include_in_schema=False, dependencies=[AdminRequired]
    )
    async def new_connection(
        request: Request,
        stores: PanelStores,
        protocol: str | None = None,
        name: str = "",
        id: str = "",
    ) -> HTMLResponse:
        config = stores.config.load_active()[0]
        if protocol is None:
            return _list_page(request, config, adding={"name": name, "id": id})
        form = form_for(protocol)
        if not _offered(request, form):
            return _list_page(
                request,
                config,
                adding={"name": name, "id": id},
                picker_error=PICK_PROTOCOL_ERROR,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        values: dict[str, object] = {
            "name": name,
            "id": id,
            "timeout_ms": str(DEFAULT_TIMEOUT_MS),
            "enabled": True,
        }
        for field in form.fields:
            if field.kind == "bool":
                values[field.name] = bool(field.default)
            elif field.default is not None:
                values[field.name] = str(field.default)
        return _edit_page(request, form, values=values)

    @router.post(
        "/connections",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def create_connection(request: Request, stores: PanelStores) -> Response:
        data = await _form_data(request)
        form = form_for(data.get("protocol", ""))
        if not _offered(request, form):
            return _list_page(
                request,
                stores.config.load_active()[0],
                adding={"name": data.get("name", ""), "id": data.get("id", "")},
                picker_error=PICK_PROTOCOL_ERROR,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        errors: dict[str, str] = {}
        connection: ConnectionConfig | None = None
        try:
            connection = connection_from_form(form, data, existing=None)
        except FormErrors as error:
            errors = error.errors
        new_id = data.get("id", "").strip()
        if (
            validate_new_id(new_id) is None
            and stores.config.get_connection(new_id) is not None
        ):
            errors["id"] = DUPLICATE_ID_ERROR
            connection = None
        if connection is not None:
            try:
                stores.config.upsert_connection(connection)
            except ConfigurationError as error:
                errors["form"] = error.message
            else:
                flash(request, SAVED_MESSAGE)
                return _see_other(f"/panel/connections/{connection.id}")
        return _edit_page(
            request,
            form,
            values=_values_from_data(form, data),
            errors=errors,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @router.get(
        "/connections/{connection_id}",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    async def edit_connection(
        request: Request, connection_id: str, stores: PanelStores
    ) -> HTMLResponse:
        existing = stores.config.get_connection(connection_id)
        if existing is None:
            return _not_found(request)
        form = form_for(existing.protocol)
        return _edit_page(
            request,
            form,
            values=_values_from_connection(form, existing),
            existing=existing,
        )

    @router.post(
        "/connections/{connection_id}",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def save_connection(
        request: Request, connection_id: str, stores: PanelStores
    ) -> Response:
        existing = stores.config.get_connection(connection_id)
        if existing is None:
            return _not_found(request)
        form = form_for(existing.protocol)
        data = await _form_data(request)
        try:
            connection = connection_from_form(form, data, existing=existing)
            stores.config.upsert_connection(connection)
        except FormErrors as error:
            errors = error.errors
        except ConfigurationError as error:
            errors = {"form": error.message}
        else:
            flash(request, SAVED_MESSAGE)
            return _see_other(f"/panel/connections/{connection_id}")
        return _edit_page(
            request,
            form,
            values=_values_from_data(form, data),
            existing=existing,
            errors=errors,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @router.get(
        "/connections/{connection_id}/delete",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    async def confirm_delete(
        request: Request, connection_id: str, stores: PanelStores
    ) -> HTMLResponse:
        existing = stores.config.get_connection(connection_id)
        if existing is None:
            return _not_found(request)
        config = stores.config.load_active()[0]
        groups = [g for g in config.tag_groups if g.connection_id == connection_id]
        group_ids = {g.id for g in groups}
        tag_count = sum(1 for t in config.tags if t.tag_group_id in group_ids)
        form = form_for(existing.protocol)
        return _edit_page(
            request,
            form,
            values=_values_from_connection(form, existing),
            existing=existing,
            confirm_delete={"groups": len(groups), "tags": tag_count},
        )

    @router.post(
        "/connections/{connection_id}/delete",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def delete_connection(
        request: Request, connection_id: str, stores: PanelStores
    ) -> Response:
        if stores.config.get_connection(connection_id) is None:
            return _not_found(request)
        stores.config.archive_connection(connection_id)
        flash(request, DELETED_MESSAGE)
        return _see_other("/panel/connections")

    @router.post(
        "/connections/{connection_id}/test",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def test_connection_route(
        request: Request, connection_id: str, stores: PanelStores
    ) -> HTMLResponse:
        existing = stores.config.get_connection(connection_id)
        if existing is None:
            return _not_found(request)
        form = form_for(existing.protocol)
        data = await _form_data(request)
        try:
            # Tests what the form would save, so settings can be tried first.
            candidate = connection_from_form(form, data, existing=existing)
        except FormErrors as error:
            return render(
                request,
                "_connection_test.html",
                {
                    "ok": False,
                    "message": "Fix the form first: " + " ".join(error.errors.values()),
                },
            )
        config = stores.config.load_active()[0]
        result = await probe_connection(
            candidate,
            _host(request).registry,
            probe_tag=_probe_tag(config, connection_id),
        )
        return render(
            request,
            "_connection_test.html",
            {"ok": result.ok, "message": result.message},
        )

    return router


# Pages


def _list_page(
    request: Request,
    config: CollectorConfig,
    *,
    adding: Mapping[str, str] | None = None,
    picker_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    host = _host(request)
    rows = _connection_rows(host, config)
    picker = picker_forms(host.registry.registered_protocols())
    return render(
        request,
        "connections.html",
        {
            "nav": "connections",
            "title": "Connections",
            "rows": rows,
            "summary": (
                f"{plural(len(config.connections), 'connection')}, "
                f"{plural(len(config.tags), 'tag')}. "
                "One link to one controller or meter each."
            ),
            "adding": adding,
            "picker": picker,
            "default_protocol": next(
                (form.protocol for form in picker if form.available), None
            ),
            "picker_error": picker_error,
        },
        status_code=status_code,
    )


def _edit_page(
    request: Request,
    form: ProtocolForm,
    *,
    values: Mapping[str, object],
    existing: ConnectionConfig | None = None,
    errors: Mapping[str, str] | None = None,
    confirm_delete: Mapping[str, int] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    badge: StateBadge | None = None
    detail: str | None = None
    saved_secrets: set[str] = set()
    if existing is not None:
        workers = _workers(_host(request))
        badge, detail = _connection_status(existing, workers)
        saved_secrets = {
            field.name
            for field in form.fields
            if field.target == "secret" and field.name in existing.protocol_options
        }
    heading = existing.name if existing is not None else "Add connection"
    return render(
        request,
        "connection_edit.html",
        {
            "nav": "connections",
            "title": heading,
            "heading": heading,
            "form": form,
            "values": values,
            "errors": dict(errors or {}),
            "existing": existing,
            "badge": badge,
            "status_detail": detail,
            "saved_secrets": saved_secrets,
            "id_help": ID_HELP,
            "saved_note": SAVED_MESSAGE,
            "confirm_delete": confirm_delete,
        },
        status_code=status_code,
    )


def _not_found(request: Request) -> HTMLResponse:
    return render(
        request,
        "base.html",
        {
            "nav": "connections",
            "title": "Connection not found",
            "message": NOT_FOUND_MESSAGE,
        },
        status_code=status.HTTP_404_NOT_FOUND,
    )


# List rows


def _connection_rows(
    host: CollectorHost, config: CollectorConfig
) -> list[ConnectionRow]:
    workers = _workers(host)
    live = host.live_values.values()
    now = datetime.now(UTC)
    groups_by_connection: dict[str, list[TagGroupConfig]] = defaultdict(list)
    for group in config.tag_groups:
        groups_by_connection[group.connection_id].append(group)
    tags_by_group: dict[str, list[TagConfig]] = defaultdict(list)
    for tag in config.tags:
        tags_by_group[tag.tag_group_id].append(tag)

    rows = []
    for connection in config.connections:
        groups = groups_by_connection[connection.id]
        all_tags = [t for g in groups for t in tags_by_group[g.id]]
        badge, detail = _connection_status(connection, workers)
        if not connection.enabled:
            collected = f"Not collecting ({plural(len(all_tags), 'tag')})"
            last_poll, last_poll_failed = "None", False
        else:
            polled = [
                t for g in groups if g.enabled for t in tags_by_group[g.id] if t.enabled
            ]
            good = 0
            for tag in polled:
                value = live.get(tag.id)
                if (
                    value is not None
                    and value.connection_id == connection.id
                    and value.result.quality is Quality.GOOD
                ):
                    good += 1
            collected = f"{good} of {len(polled)}"
            last_poll, last_poll_failed = _last_poll(host, connection, groups, now)
        rows.append(
            ConnectionRow(
                connection=connection,
                protocol_label=form_for(connection.protocol).label,
                address=display_endpoint(connection.endpoint),
                badge=badge,
                detail=detail,
                collected=collected,
                last_poll=last_poll,
                last_poll_failed=last_poll_failed,
            )
        )
    return rows


def _last_poll(
    host: CollectorHost,
    connection: ConnectionConfig,
    groups: list[TagGroupConfig],
    now: datetime,
) -> tuple[str, bool]:
    newest: PollExecution | None = None
    for group in groups:
        execution = host.live_values.last_poll(group.id)
        if execution is None or execution.connection_id != connection.id:
            continue
        if newest is None or _poll_time(execution) > _poll_time(newest):
            newest = execution
    if newest is None:
        return "None", False
    age = format_age((now - _poll_time(newest)).total_seconds())
    if newest.status is PollStatus.FAILED:
        return f"Failed {age} ago", True
    return f"{age} ago", False


def _poll_time(execution: PollExecution) -> datetime:
    return execution.finished_at or execution.started_at


def _connection_status(
    connection: ConnectionConfig,
    workers: Mapping[str, RuntimeComponentStatus],
) -> tuple[StateBadge, str | None]:
    if not connection.enabled:
        return state_badge("disabled"), None
    worker = workers.get(f"{WORKER_PREFIX}{connection.id}")
    if worker is None:
        return state_badge("stopped", "Not collecting"), None
    badge = state_badge(worker.state.value)
    detail = None
    if badge.kind == "failed":
        reason = worker.error_code or "Failed"
        detail = f"{reason} since {worker.updated_at:%H:%M} UTC"
    return badge, detail


def _workers(host: CollectorHost) -> dict[str, RuntimeComponentStatus]:
    return {worker.component_id: worker for worker in host.api_state().workers}


# Forms


def _offered(request: Request, form: ProtocolForm) -> bool:
    registered = _host(request).registry.registered_protocols()
    return form.available and form.protocol in registered


async def _form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _values_from_connection(
    form: ProtocolForm, connection: ConnectionConfig
) -> dict[str, object]:
    values: dict[str, object] = {
        "name": connection.name,
        "id": connection.id,
        "timeout_ms": str(connection.timeout_ms),
        "enabled": connection.enabled,
    }
    for field in form.fields:
        if field.target == "endpoint":
            values[field.name] = display_endpoint(connection.endpoint)
        elif field.target == "option":
            option = connection.protocol_options.get(field.name, field.default)
            if field.kind == "bool":
                values[field.name] = option is True
            elif option is not None:
                values[field.name] = str(option)
    return values


def _values_from_data(form: ProtocolForm, data: Mapping[str, str]) -> dict[str, object]:
    """Return submitted values to show again; secrets are never echoed."""
    values: dict[str, object] = {
        "name": data.get("name", ""),
        "id": data.get("id", ""),
        "timeout_ms": data.get("timeout_ms", ""),
        "enabled": bool(data.get("enabled")),
    }
    for field in form.fields:
        if field.target == "secret":
            continue
        if field.kind == "bool":
            values[field.name] = bool(data.get(field.name))
        elif field.target == "endpoint":
            values[field.name] = display_endpoint(data.get(field.name, ""))
        else:
            values[field.name] = data.get(field.name, "")
    return values


def _probe_tag(config: CollectorConfig, connection_id: str) -> TagConfig | None:
    groups = {
        g.id
        for g in config.tag_groups
        if g.connection_id == connection_id and g.enabled
    }
    candidates = [t for t in config.tags if t.tag_group_id in groups and t.enabled]
    return min(candidates, key=lambda t: t.id, default=None)


def _host(request: Request) -> CollectorHost:
    return auth.web_options(request).host


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
