"""Panel routes for Tag Groups and Tags, shown on the Connection page.

Edit forms are fetched with htmx and swapped into the page; a plain request
for the same URL gets the whole Connection page with that form open, so
nothing depends on scripts. Saves are ordinary form posts that redirect back
to the Connection page, or show it again with the field errors.

Every write goes through ``asyncio.to_thread`` (the store is blocking I/O)
or, for handlers that touch only the stores, a plain ``def``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from urllib.parse import urlencode

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import ConfigurationError, ConnectionConfig, TagConfig
from plc_data_collector.panel import audit
from plc_data_collector.panel.app import AdminRequired, PanelStores, SameSiteRequired
from plc_data_collector.panel.protocol_forms import FormErrors
from plc_data_collector.panel.rendering import flash, render
from plc_data_collector.panel.routes_connections import (
    SAVED_MESSAGE,
    render_connection_page,
)
from plc_data_collector.panel.tag_adapters import TagFormAdapter, tag_adapter_for
from plc_data_collector.panel.tag_forms import (
    GROUP_DUPLICATE_ERROR,
    FormUi,
    generate_tag_id,
    group_choices,
    group_defaults,
    group_from_form,
    group_values,
    group_values_from_data,
)
from plc_data_collector.panel.tag_paste import (
    PasteResult,
    PasteRow,
    example_paste,
    parse_paste,
)

GROUP_DELETED_MESSAGE = (
    "Tag group deleted. Saved changes take effect after you apply them from the banner."
)
TAG_DELETED_MESSAGE = (
    "Tag deleted. Saved changes take effect after you apply them from the banner."
)
CONNECTION_NOT_FOUND = "This connection doesn't exist or was deleted."
GROUP_NOT_FOUND = "This tag group doesn't exist or was deleted."
TAG_NOT_FOUND = "This tag doesn't exist or was deleted."
PASTE_IDS_TAKEN = "Some IDs were taken meanwhile. Preview again."
# Attempts at a free generated Tag ID when concurrent creates take the same one.
ID_ATTEMPTS = 5


def create_tags_router() -> APIRouter:
    """Create the Tag Group and Tag routes; include them with ``/panel``."""
    router = APIRouter()

    # Tag Groups

    @router.get(
        "/connections/{connection_id}/groups/new",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    async def new_group(
        request: Request, connection_id: str, stores: PanelStores
    ) -> HTMLResponse:
        if await _connection(stores, connection_id) is None:
            return _not_found(request, CONNECTION_NOT_FOUND)
        ui = FormUi("new", None, group_defaults(), {})
        return await _group_form(request, stores, connection_id, ui)

    @router.get(
        "/groups/{group_id}/edit",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    async def edit_group(
        request: Request, group_id: str, stores: PanelStores
    ) -> HTMLResponse:
        group = await asyncio.to_thread(stores.config.get_tag_group, group_id)
        if group is None:
            return _not_found(request, GROUP_NOT_FOUND)
        ui = FormUi("edit", group.id, group_values(group), {})
        return await _group_form(request, stores, group.connection_id, ui)

    @router.post(
        "/connections/{connection_id}/groups",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def create_group(
        request: Request, connection_id: str, stores: PanelStores
    ) -> Response:
        if await _connection(stores, connection_id) is None:
            return _not_found(request, CONNECTION_NOT_FOUND)
        data = await _form_data(request)
        try:
            group = group_from_form(data, connection_id=connection_id, existing=None)
            # Refuses an active ID under the store's lock; restores an archived one.
            await asyncio.to_thread(stores.config.create_tag_group, group)
        except FormErrors as error:
            errors = error.errors
        except ConfigurationError as error:
            errors = _group_store_errors(error)
        else:
            await asyncio.to_thread(
                audit.configuration_changed, stores, "tag_group", group.id, "created"
            )
            flash(request, SAVED_MESSAGE)
            return _back_to(connection_id)
        ui = FormUi("new", None, group_values_from_data(data), errors)
        return await render_connection_page(
            request,
            stores,
            connection_id,
            group_ui=ui,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @router.post(
        "/groups/{group_id}",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def save_group(
        request: Request, group_id: str, stores: PanelStores
    ) -> Response:
        existing = await asyncio.to_thread(stores.config.get_tag_group, group_id)
        if existing is None:
            return _not_found(request, GROUP_NOT_FOUND)
        data = await _form_data(request)
        try:
            group = group_from_form(
                data, connection_id=existing.connection_id, existing=existing
            )
            await asyncio.to_thread(stores.config.upsert_tag_group, group)
        except FormErrors as error:
            errors = error.errors
        except ConfigurationError as error:
            errors = _group_store_errors(error)
        else:
            await asyncio.to_thread(
                audit.configuration_changed, stores, "tag_group", group.id, "updated"
            )
            flash(request, SAVED_MESSAGE)
            return _back_to(existing.connection_id)
        ui = FormUi("edit", existing.id, group_values_from_data(data), errors)
        return await render_connection_page(
            request,
            stores,
            existing.connection_id,
            group_ui=ui,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @router.post(
        "/groups/{group_id}/delete",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    def delete_group(request: Request, group_id: str, stores: PanelStores) -> Response:
        group = stores.config.get_tag_group(group_id)
        if group is None:
            return _not_found(request, GROUP_NOT_FOUND)
        stores.config.archive_tag_group(group_id)
        audit.configuration_changed(stores, "tag_group", group_id, "deleted")
        flash(request, GROUP_DELETED_MESSAGE)
        return _back_to(group.connection_id)

    # Tags

    @router.get(
        "/connections/{connection_id}/tags/new",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    async def new_tag(
        request: Request, connection_id: str, stores: PanelStores
    ) -> HTMLResponse:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request, CONNECTION_NOT_FOUND)
        config = await _load_config(stores)
        adapter = tag_adapter_for(connection.protocol)
        values = _redrawn_values(request, adapter)
        if values is None:
            values = adapter.defaults(connection, group_choices(config, connection_id))
        ui = FormUi("new", None, values, {})
        return await _tag_form(request, stores, connection, adapter, ui, config)

    @router.get(
        "/connections/{connection_id}/tags/paste",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    async def paste_form(
        request: Request, connection_id: str, stores: PanelStores
    ) -> HTMLResponse:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request, CONNECTION_NOT_FOUND)
        return await _paste_page(request, stores, connection, text="", group_id="")

    @router.post(
        "/connections/{connection_id}/tags/paste",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def paste_tags(
        request: Request, connection_id: str, stores: PanelStores
    ) -> Response:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request, CONNECTION_NOT_FOUND)
        data = await _form_data(request)
        text = data.get("text", "")
        group_id = data.get("tag_group_id", "")
        config = await _load_config(stores)
        choices = group_choices(config, connection_id)
        result = await _parse_paste(stores, connection, choices, text, group_id)
        if data.get("action") == "save" and result.ok:
            tags = [row.tag for row in result.rows if row.tag is not None]
            try:
                await asyncio.to_thread(stores.config.create_tags, tags)
            except ConfigurationError as error:
                message = (
                    PASTE_IDS_TAKEN if error.code == "duplicate_id" else error.message
                )
                return await _paste_page(
                    request,
                    stores,
                    connection,
                    text=text,
                    group_id=group_id,
                    status_code=status.HTTP_409_CONFLICT,
                    error=message,
                )
            for tag in tags:
                await asyncio.to_thread(
                    audit.configuration_changed, stores, "tag", tag.id, "created"
                )
            flash(request, f"Added {len(tags)} {'tag' if len(tags) == 1 else 'tags'}.")
            return _back_to(connection_id)
        refused_save = data.get("action") == "save" and not result.ok
        return await _paste_page(
            request,
            stores,
            connection,
            text=text,
            group_id=group_id,
            result=result,
            status_code=(
                status.HTTP_400_BAD_REQUEST
                if refused_save or result.error
                else status.HTTP_200_OK
            ),
        )

    @router.get(
        "/tags/{tag_id}/edit",
        include_in_schema=False,
        dependencies=[AdminRequired],
    )
    async def edit_tag(
        request: Request, tag_id: str, stores: PanelStores
    ) -> HTMLResponse:
        found = await _tag_and_connection(stores, tag_id)
        if found is None:
            return _not_found(request, TAG_NOT_FOUND)
        tag, connection = found
        adapter = tag_adapter_for(connection.protocol)
        values = _redrawn_values(request, adapter)
        if values is None:
            values = adapter.values(tag, connection)
        ui = FormUi("edit", tag.id, values, {})
        return await _tag_form(
            request, stores, connection, adapter, ui, await _load_config(stores)
        )

    @router.post(
        "/connections/{connection_id}/tags",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def create_tag(
        request: Request, connection_id: str, stores: PanelStores
    ) -> Response:
        connection = await _connection(stores, connection_id)
        if connection is None:
            return _not_found(request, CONNECTION_NOT_FOUND)
        adapter = tag_adapter_for(connection.protocol)
        data = await _form_data(request)
        config = await _load_config(stores)
        allowed = {gid for gid, _ in group_choices(config, connection_id)}
        errors: dict[str, str] = {}
        try:
            # Validate with a placeholder ID first; the real one is generated
            # below from the IDs taken at that moment.
            template = adapter.build(
                data,
                connection=connection,
                tag_id="pending",
                allowed_group_ids=allowed,
                existing=None,
                enabled="enabled" in data,
            )
            await _create_with_generated_id(stores, connection_id, template)
        except FormErrors as error:
            errors = error.errors
        except ConfigurationError as error:
            errors = {"form": error.message}
        else:
            flash(request, SAVED_MESSAGE)
            following = adapter.next_values(template, connection)
            if following is None:
                return _back_to(connection_id)
            # Fast entry: redirect to a new form for the next Tag, so a reload
            # or Back never posts the same Tag again.
            return RedirectResponse(
                f"/panel/connections/{connection_id}/tags/new?{_form_query(following)}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        ui = FormUi("new", None, adapter.values_from_data(data), errors)
        return await render_connection_page(
            request,
            stores,
            connection_id,
            tag_ui=ui,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @router.post(
        "/tags/{tag_id}",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def save_tag(request: Request, tag_id: str, stores: PanelStores) -> Response:
        found = await _tag_and_connection(stores, tag_id)
        if found is None:
            return _not_found(request, TAG_NOT_FOUND)
        existing, connection = found
        adapter = tag_adapter_for(connection.protocol)
        data = await _form_data(request)
        config = await _load_config(stores)
        allowed = {gid for gid, _ in group_choices(config, connection.id)}
        try:
            tag = adapter.build(
                data,
                connection=connection,
                tag_id=existing.id,
                allowed_group_ids=allowed,
                existing=existing,
                enabled="enabled" in data,
            )
            await asyncio.to_thread(stores.config.upsert_tag, tag)
        except FormErrors as error:
            errors = error.errors
        except ConfigurationError as error:
            errors = {"form": error.message}
        else:
            await asyncio.to_thread(
                audit.configuration_changed, stores, "tag", tag.id, "updated"
            )
            flash(request, SAVED_MESSAGE)
            return _back_to(connection.id)
        ui = FormUi("edit", existing.id, adapter.values_from_data(data), errors)
        return await render_connection_page(
            request,
            stores,
            connection.id,
            tag_ui=ui,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @router.post(
        "/tags/{tag_id}/delete",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    def delete_tag(request: Request, tag_id: str, stores: PanelStores) -> Response:
        tag = stores.config.get_tag(tag_id)
        group = stores.config.get_tag_group(tag.tag_group_id) if tag else None
        if tag is None or group is None:
            return _not_found(request, TAG_NOT_FOUND)
        stores.config.archive_tag(tag_id)
        audit.configuration_changed(stores, "tag", tag_id, "deleted")
        flash(request, TAG_DELETED_MESSAGE)
        return _back_to(group.connection_id)

    return router


# Store helpers


async def _connection(stores: Stores, connection_id: str) -> ConnectionConfig | None:
    return await asyncio.to_thread(stores.config.get_connection, connection_id)


async def _load_config(stores: Stores) -> CollectorConfig:
    config, _ = await asyncio.to_thread(stores.config.load_active)
    return config


async def _tag_and_connection(
    stores: Stores, tag_id: str
) -> tuple[TagConfig, ConnectionConfig] | None:
    """Return a Tag and the Connection its Tag Group belongs to."""
    tag = await asyncio.to_thread(stores.config.get_tag, tag_id)
    if tag is None:
        return None
    group = await asyncio.to_thread(stores.config.get_tag_group, tag.tag_group_id)
    if group is None:
        return None
    connection = await _connection(stores, group.connection_id)
    return None if connection is None else (tag, connection)


async def _create_with_generated_id(
    stores: Stores, connection_id: str, template: TagConfig
) -> None:
    """Create the Tag under a generated ID, retrying when another create wins it."""
    for attempt in range(ID_ATTEMPTS):
        taken = await asyncio.to_thread(stores.config.all_tag_ids)
        tag_id = generate_tag_id(connection_id, template.name, taken)
        tag = TagConfig(
            id=tag_id,
            tag_group_id=template.tag_group_id,
            name=template.name,
            address=template.address,
            value_type=template.value_type,
            enabled=template.enabled,
            metadata=template.metadata,
            protocol_options=template.protocol_options,
        )
        try:
            await asyncio.to_thread(stores.config.create_tag, tag)
        except ConfigurationError as error:
            if error.code != "duplicate_id" or attempt == ID_ATTEMPTS - 1:
                raise
        else:
            await asyncio.to_thread(
                audit.configuration_changed, stores, "tag", tag.id, "created"
            )
            return


async def _parse_paste(
    stores: Stores,
    connection: ConnectionConfig,
    choices: list[tuple[str, str]],
    text: str,
    group_id: str,
) -> PasteResult:
    allowed = {gid for gid, _ in choices}
    default_group = (
        group_id if group_id in allowed else (choices[0][0] if choices else "")
    )
    taken = await asyncio.to_thread(stores.config.all_tag_ids)
    return parse_paste(
        text,
        adapter=tag_adapter_for(connection.protocol),
        connection=connection,
        default_group_id=default_group,
        allowed_group_ids=allowed,
        taken_ids=taken,
    )


def _paste_row_view(
    row: PasteRow, adapter: TagFormAdapter, connection: ConnectionConfig
) -> dict[str, object]:
    """Return what the preview table shows for one pasted line."""
    values = row.values
    if row.tag is not None:
        read_from, address, value_type = adapter.table_cells(row.tag, connection)
    else:
        read_from = values.get("read_from", "")
        address = values.get("address", "")
        value_type = values.get("data_type") or values.get("value_type", "")
    return {
        "line": row.line,
        "name": row.tag.name if row.tag else values.get("name", ""),
        "read_from": read_from,
        "address": address,
        "value_type": value_type,
        "group": row.tag.tag_group_id if row.tag else values.get("tag_group", ""),
        "tag_id": row.tag.id if row.tag else "",
        "errors": list(row.errors.items()),
    }


async def _paste_page(
    request: Request,
    stores: Stores,
    connection: ConnectionConfig,
    *,
    text: str,
    group_id: str,
    result: PasteResult | None = None,
    status_code: int = 200,
    error: str | None = None,
) -> HTMLResponse:
    config = await _load_config(stores)
    adapter = tag_adapter_for(connection.protocol)
    choices = group_choices(config, connection.id)
    rows = (
        [_paste_row_view(row, adapter, connection) for row in result.rows]
        if result
        else []
    )
    context: dict[str, object] = {
        "connection_id": connection.id,
        "connection_name": connection.name,
        "title": "Paste tags",
        "nav": "connections",
        "text": text,
        "selected_group": group_id,
        "group_choices": choices,
        "columns": adapter.columns,
        "required": adapter.required_columns,
        "example": example_paste(connection),
        "read_from_column": adapter.read_from_column,
        "rows": rows,
        "can_save": result is not None and result.ok,
        "error": error or (result.error if result else None),
    }
    template = "_tag_paste.html" if _is_htmx(request) else "tag_paste.html"
    return render(request, template, context, status_code=status_code)


def _group_store_errors(error: ConfigurationError) -> dict[str, str]:
    if error.code == "duplicate_id":
        return {"id": GROUP_DUPLICATE_ERROR}
    return {"form": error.message}


# Responses


async def _group_form(
    request: Request, stores: Stores, connection_id: str, ui: FormUi
) -> HTMLResponse:
    if _is_htmx(request):
        tag_count = 0
        if ui.target_id is not None:
            active = await asyncio.to_thread(stores.config.active_tags)
            tag_count = sum(1 for tag, _ in active if tag.tag_group_id == ui.target_id)
        return render(
            request,
            "_group_form.html",
            {"connection_id": connection_id, "ui": ui, "tag_count": tag_count},
        )
    return await render_connection_page(request, stores, connection_id, group_ui=ui)


async def _tag_form(
    request: Request,
    stores: Stores,
    connection: ConnectionConfig,
    adapter: TagFormAdapter,
    ui: FormUi,
    config: CollectorConfig,
) -> HTMLResponse:
    if _is_htmx(request):
        return render(
            request,
            "_tag_form.html",
            {
                "connection_id": connection.id,
                "ui": ui,
                "group_choices": group_choices(config, connection.id),
                "form_template": adapter.template,
                "extra": adapter.template_context(ui.values, connection),
            },
        )
    return await render_connection_page(request, stores, connection.id, tag_ui=ui)


def _redrawn_values(
    request: Request, adapter: TagFormAdapter
) -> dict[str, object] | None:
    """Return the values of a form fetched again with its current fields.

    The Modbus form asks for itself again when Read from or Data type
    changes, sending its fields as query parameters, so the data types and
    hints follow the choice. None when the request carries no form fields.
    """
    query = request.query_params
    if not any(name in query for name in adapter.columns):
        return None
    return adapter.values_from_data(
        {key: value for key, value in query.items() if isinstance(value, str)}
    )


def _form_query(values: Mapping[str, object]) -> str:
    """Encode form values for a URL: a ticked box as "on", empty fields left out."""
    pairs: list[tuple[str, str]] = []
    for key, value in values.items():
        if isinstance(value, bool):
            if value:
                pairs.append((key, "on"))
        elif value not in (None, ""):
            pairs.append((key, str(value)))
    return urlencode(pairs)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


async def _form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _back_to(connection_id: str) -> RedirectResponse:
    return RedirectResponse(
        f"/panel/connections/{connection_id}", status_code=status.HTTP_303_SEE_OTHER
    )


def _not_found(request: Request, message: str) -> HTMLResponse:
    return render(
        request,
        "base.html",
        {"nav": "connections", "title": "Not found", "message": message},
        status_code=status.HTTP_404_NOT_FOUND,
    )
