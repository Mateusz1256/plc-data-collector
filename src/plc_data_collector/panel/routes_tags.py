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
from plc_data_collector.panel.tag_forms import (
    GROUP_DUPLICATE_ERROR,
    VALUE_TYPE_CHOICES,
    FormUi,
    generate_tag_id,
    group_choices,
    group_defaults,
    group_from_form,
    group_values,
    group_values_from_data,
    tag_defaults,
    tag_from_form,
    tag_values,
    tag_values_from_data,
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
        if await _connection(stores, connection_id) is None:
            return _not_found(request, CONNECTION_NOT_FOUND)
        config = await _load_config(stores)
        choices = group_choices(config, connection_id)
        ui = FormUi("new", None, tag_defaults(choices), {})
        return await _tag_form(request, stores, connection_id, ui, config)

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
        tag, connection_id = found
        ui = FormUi("edit", tag.id, tag_values(tag), {})
        return await _tag_form(
            request, stores, connection_id, ui, await _load_config(stores)
        )

    @router.post(
        "/connections/{connection_id}/tags",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def create_tag(
        request: Request, connection_id: str, stores: PanelStores
    ) -> Response:
        if await _connection(stores, connection_id) is None:
            return _not_found(request, CONNECTION_NOT_FOUND)
        data = await _form_data(request)
        config = await _load_config(stores)
        allowed = {gid for gid, _ in group_choices(config, connection_id)}
        errors: dict[str, str] = {}
        try:
            # Validate with a placeholder ID first; the real one is generated
            # below from the IDs taken at that moment.
            template = tag_from_form(
                data, tag_id="pending", allowed_group_ids=allowed, existing=None
            )
            await _create_with_generated_id(stores, connection_id, template)
        except FormErrors as error:
            errors = error.errors
        except ConfigurationError as error:
            errors = {"form": error.message}
        else:
            flash(request, SAVED_MESSAGE)
            return _back_to(connection_id)
        ui = FormUi("new", None, tag_values_from_data(data), errors)
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
        existing, connection_id = found
        data = await _form_data(request)
        config = await _load_config(stores)
        allowed = {gid for gid, _ in group_choices(config, connection_id)}
        try:
            tag = tag_from_form(
                data, tag_id=existing.id, allowed_group_ids=allowed, existing=existing
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
            return _back_to(connection_id)
        ui = FormUi("edit", existing.id, tag_values_from_data(data), errors)
        return await render_connection_page(
            request,
            stores,
            connection_id,
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
) -> tuple[TagConfig, str] | None:
    """Return a Tag and the ID of the Connection its Tag Group belongs to."""
    tag = await asyncio.to_thread(stores.config.get_tag, tag_id)
    if tag is None:
        return None
    group = await asyncio.to_thread(stores.config.get_tag_group, tag.tag_group_id)
    return None if group is None else (tag, group.connection_id)


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


def _group_store_errors(error: ConfigurationError) -> dict[str, str]:
    if error.code == "duplicate_id":
        return {"id": GROUP_DUPLICATE_ERROR}
    return {"form": error.message}


# Responses


async def _group_form(
    request: Request, stores: Stores, connection_id: str, ui: FormUi
) -> HTMLResponse:
    if _is_htmx(request):
        return render(
            request,
            "_group_form.html",
            {"connection_id": connection_id, "ui": ui, "tag_count": 0},
        )
    return await render_connection_page(request, stores, connection_id, group_ui=ui)


async def _tag_form(
    request: Request,
    stores: Stores,
    connection_id: str,
    ui: FormUi,
    config: CollectorConfig,
) -> HTMLResponse:
    if _is_htmx(request):
        return render(
            request,
            "_tag_form.html",
            {
                "connection_id": connection_id,
                "ui": ui,
                "group_choices": group_choices(config, connection_id),
                "value_types": VALUE_TYPE_CHOICES,
            },
        )
    return await render_connection_page(request, stores, connection_id, tag_ui=ui)


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
