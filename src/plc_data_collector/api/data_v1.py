"""Read-only data API (``/api/v1``): Tags, their current values and history.

Every endpoint needs an API key in the ``X-API-Key`` header. Endpoints read
the live value cache on the event loop and run database work in a thread.
"""

from __future__ import annotations

import asyncio
import base64
import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from plc_data_collector.domain import Quality, TagConfig, TagResult, TagValue, ValueType
from plc_data_collector.persistence import ApiKeyInfo, TagReadingRow

if TYPE_CHECKING:
    from plc_data_collector.app.stores import Stores
    from plc_data_collector.panel.host import CollectorHost
    from plc_data_collector.runtime.live_values import LiveValue

API_KEY_HEADER = "X-API-Key"
MISSING_KEY_MESSAGE = "Send an API key in the X-API-Key header."
INVALID_KEY_MESSAGE = "API key is not valid or was revoked."
NOT_READY_MESSAGE = "Starting: waiting for the database."
TAG_NOT_FOUND_MESSAGE = "Tag not found."
NAIVE_DATETIME_MESSAGE = "Include a timezone, for example 2026-11-12T10:00:00Z."
FROM_AFTER_TO_MESSAGE = "Set from to a time before to."
UNIT_KEY = "unit"
DEFAULT_HISTORY_SPAN = timedelta(hours=1)
DEFAULT_HISTORY_LIMIT = 1000
MAX_HISTORY_LIMIT = 10_000

_api_key_header = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="An API key created in the Panel under Settings.",
)

JsonValue = float | int | bool | str | None


def utc_now() -> datetime:
    """Return the current UTC time; tests replace it."""
    return datetime.now(UTC)


class CurrentValueOut(BaseModel):
    """The latest polled value of a Tag."""

    value: JsonValue
    quality: Quality
    received_at: datetime
    source_timestamp: datetime | None
    error_code: str | None


class TagOut(BaseModel):
    """An active Tag with its current value; null before the first poll."""

    id: str
    name: str
    connection_id: str
    tag_group_id: str
    address: str
    value_type: ValueType
    unit: str | None
    enabled: bool
    current: CurrentValueOut | None


class TagListOut(BaseModel):
    """Every active Tag."""

    tags: list[TagOut]


class ReadingOut(BaseModel):
    """One stored reading."""

    received_at: datetime
    source_timestamp: datetime | None
    quality: Quality
    value: JsonValue
    error_code: str | None


class HistoryOut(BaseModel):
    """Stored readings of a Tag in ``[from, to)``, oldest first."""

    tag_id: str
    archived: bool
    from_: datetime = Field(serialization_alias="from")
    to: datetime
    readings: list[ReadingOut]
    truncated: bool = Field(
        description="True when the range holds more readings than the limit."
    )


def encode_value(value: TagValue | None) -> JsonValue:
    """Return a Tag value as JSON: binary as base64, non-finite numbers as null."""
    if value is None:
        return None
    raw = value.raw
    if isinstance(raw, bytes):
        return base64.b64encode(raw).decode("ascii")
    if isinstance(raw, float) and not math.isfinite(raw):
        return None
    return raw


def _host(request: Request) -> CollectorHost:
    host: CollectorHost = request.app.state.panel_web.host
    return host


def _stores(request: Request) -> Stores:
    stores = _host(request).stores
    if stores is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_READY_MESSAGE)
    return stores


def require_api_key(
    request: Request,
    key: Annotated[str | None, Security(_api_key_header)],
) -> ApiKeyInfo:
    """FastAPI dependency: return the caller's active API key or answer 401.

    The response never says which part of a key failed, and the key itself
    is never logged or echoed.
    """
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, MISSING_KEY_MESSAGE)
    info = _stores(request).api_keys.authenticate(key)
    if info is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_KEY_MESSAGE)
    return info


def create_data_v1_router() -> APIRouter:
    """Create the data API router; it carries its own ``/api/v1`` prefix."""
    router = APIRouter(
        prefix="/api/v1",
        tags=["Data"],
        dependencies=[Depends(require_api_key)],
        responses={
            401: {"description": "Missing, unknown or revoked API key."},
            503: {"description": "The database is not ready yet."},
        },
    )

    @router.get("/tags", response_model=TagListOut)
    async def list_tags(request: Request) -> TagListOut:
        """Return every active Tag with its current value."""
        stores = _stores(request)
        active = await asyncio.to_thread(stores.config.active_tags)
        live = _host(request).live_values.values()
        return TagListOut(
            tags=[
                _tag_out(tag, connection_id, live.get(tag.id))
                for tag, connection_id in active
            ]
        )

    @router.get(
        "/tags/{tag_id}",
        response_model=TagOut,
        responses={404: {"description": "No active Tag has this ID."}},
    )
    async def get_tag(request: Request, tag_id: str) -> TagOut:
        """Return one active Tag with its current value."""
        stores = _stores(request)
        found = await asyncio.to_thread(stores.config.active_tags, tag_id)
        if not found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, TAG_NOT_FOUND_MESSAGE)
        tag, connection_id = found[0]
        return _tag_out(tag, connection_id, _host(request).live_values.get(tag_id))

    @router.get(
        "/tags/{tag_id}/history",
        response_model=HistoryOut,
        responses={404: {"description": "No Tag, active or archived, has this ID."}},
    )
    def tag_history(
        request: Request,
        tag_id: str,
        from_: Annotated[
            datetime | None,
            Query(
                alias="from",
                description="Start, inclusive, ISO 8601 with a timezone. "
                "Defaults to one hour before to.",
            ),
        ] = None,
        to: Annotated[
            datetime | None,
            Query(
                description="End, exclusive, ISO 8601 with a timezone. Defaults to now."
            ),
        ] = None,
        limit: Annotated[
            int, Query(ge=1, le=MAX_HISTORY_LIMIT)
        ] = DEFAULT_HISTORY_LIMIT,
    ) -> HistoryOut:
        """Return stored readings of a Tag, archived Tags included."""
        # Plain def: FastAPI runs it in the threadpool, so the stores may block.
        for value in (from_, to):
            if value is not None and value.utcoffset() is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, NAIVE_DATETIME_MESSAGE
                )
        end = (to or utc_now()).astimezone(UTC)
        start = (from_ or end - DEFAULT_HISTORY_SPAN).astimezone(UTC)
        if start >= end:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, FROM_AFTER_TO_MESSAGE
            )
        stores = _stores(request)
        archived = stores.config.is_archived_tag(tag_id)
        if archived is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, TAG_NOT_FOUND_MESSAGE)
        rows, truncated = stores.readings.history(tag_id, start, end, limit)
        return HistoryOut(
            tag_id=tag_id,
            archived=archived,
            from_=start,
            to=end,
            readings=[_reading_out(row) for row in rows],
            truncated=truncated,
        )

    return router


def _tag_out(tag: TagConfig, connection_id: str, live: LiveValue | None) -> TagOut:
    unit = tag.metadata.get(UNIT_KEY)
    return TagOut(
        id=tag.id,
        name=tag.name,
        connection_id=connection_id,
        tag_group_id=tag.tag_group_id,
        address=tag.address,
        value_type=tag.value_type,
        unit=unit if isinstance(unit, str) and unit else None,
        enabled=tag.enabled,
        current=None if live is None else _current_out(live.result),
    )


def _current_out(result: TagResult) -> CurrentValueOut:
    return CurrentValueOut(
        value=encode_value(result.value),
        quality=result.quality,
        received_at=result.received_at,
        source_timestamp=result.source_timestamp,
        error_code=result.error_code,
    )


def _reading_out(row: TagReadingRow) -> ReadingOut:
    return ReadingOut(
        received_at=row.received_at,
        source_timestamp=row.source_timestamp,
        quality=row.quality,
        value=encode_value(row.value),
        error_code=row.error_code,
    )
