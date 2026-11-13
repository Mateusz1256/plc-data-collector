"""Panel Live tags page, the pending-changes banner and Apply changes.

``pending_changes`` reads the store, so it always runs in a thread: the
``attach_pending_changes`` dependency, mounted on every Panel router, runs it
with ``asyncio.to_thread`` for each full page a signed-in browser loads, and
``render`` passes the result to the banner in ``base.html``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.config_transfer import (
    ConfigChange,
    ConfigDiff,
    diff_configs,
)
from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import (
    CollectorError,
    ConnectionConfig,
    PollExecution,
    PollStatus,
    Quality,
    TagConfig,
    TagGroupConfig,
    TagValue,
    ValueType,
)
from plc_data_collector.panel import auth
from plc_data_collector.panel.app import AdminRequired, PanelStores, SameSiteRequired
from plc_data_collector.panel.host import CollectorHost
from plc_data_collector.panel.protocol_forms import display_endpoint, form_for
from plc_data_collector.panel.rendering import (
    StateBadge,
    flash,
    format_age,
    format_interval,
    plural,
    render,
    state_badge,
)
from plc_data_collector.panel.routes_connections import (
    NOT_FOUND_MESSAGE,
    connection_status,
    worker_statuses,
)
from plc_data_collector.panel.tag_forms import UNIT_KEY
from plc_data_collector.runtime.live_values import LiveValue, freshness

APPLIED_MESSAGE = "Changes applied. Collection restarted."
APPLY_IN_PROGRESS_MESSAGE = "Changes are already being applied. Wait a few seconds."
APPLY_NOT_RUNNING_MESSAGE = (
    "Collection is starting or stopping, so changes cannot be applied now. "
    "Try again in a few seconds."
)
ENABLED_MESSAGE = (
    "Connection enabled. Saved changes take effect after you apply them "
    "from the banner."
)
NOTHING_PENDING_MESSAGE = "Nothing to apply. The saved configuration is running."
NO_VALUE = "—"
THIN_SPACE = "\u2009"
EMPTY_CONFIG = CollectorConfig(connections=(), tag_groups=(), tags=())
# A Tag Group interval below this cannot come from the store; it is clamped
# so the freshness bar never divides by zero.
MIN_INTERVAL_MS = 1
_PENDING_STATE = "pending_changes"
_KIND_LABELS = {"connection": "Connection", "tag_group": "Tag group", "tag": "Tag"}


def utc_now() -> datetime:
    """Return the current time; a module-level seam so tests can fix it."""
    return datetime.now(UTC)


# Banner


@dataclass(frozen=True)
class PendingChanges:
    """Saved configuration that is not running yet, or a stopped collection."""

    diff: ConfigDiff
    stopped_error: str | None

    @property
    def headline(self) -> str:
        """Return the banner's bold first sentence."""
        if self.stopped_error is not None:
            return f"Collection is stopped: {self.stopped_error}."
        total = self.diff.total
        if total == 0:
            return "Saved changes are not running yet."
        if total == 1:
            return "1 change is saved but not running yet."
        return f"{total} changes are saved but not running yet."

    @property
    def detail(self) -> str:
        """Return the sentence after the headline."""
        if self.stopped_error is not None:
            return "Fix the problem and apply changes again."
        return (
            "Applying restarts collection for a few seconds. "
            "Readings waiting to be stored are kept."
        )


def pending_changes(host: CollectorHost) -> PendingChanges | None:
    """Return what the banner shows, or None to hide it.

    Blocking: reads the store, so call it in a thread. The banner shows when
    the saved revision differs from the running one, or when collection is
    stopped by an error. It stays hidden while an apply is in progress and
    while collection is starting (stopped without an error code).
    """
    stores = host.stores
    if stores is None or host.applying:
        return None
    running_revision = host.running_revision
    running_config = host.running_config
    stopped_error: str | None = None
    if running_revision is None:
        stopped_error = host.last_error_code
        if stopped_error is None:
            return None
    elif stores.config.revision() == running_revision:
        return None
    saved, revision = stores.config.load_active()
    if stopped_error is None and revision == running_revision:
        return None
    return PendingChanges(
        diff=diff_configs(running_config or EMPTY_CONFIG, saved),
        stopped_error=stopped_error,
    )


async def load_pending_changes(request: Request) -> PendingChanges | None:
    """Return ``pending_changes`` for this request, computed once in a thread."""
    if hasattr(request.state, _PENDING_STATE):
        cached: PendingChanges | None = getattr(request.state, _PENDING_STATE)
        return cached
    host = auth.web_options(request).host
    pending = await asyncio.to_thread(pending_changes, host)
    setattr(request.state, _PENDING_STATE, pending)
    return pending


async def attach_pending_changes(request: Request) -> None:
    """FastAPI dependency for every Panel router: prepare the banner.

    Only full pages a signed-in browser loads get it; htmx fragments, such as
    the Live body that refreshes every second, and form posts skip the store.
    """
    if request.method not in ("GET", "HEAD"):
        return
    if request.headers.get("HX-Request") == "true":
        return
    if not auth.is_signed_in(request):
        return
    await load_pending_changes(request)


# Live view


@dataclass(frozen=True)
class TagRow:
    """One Tag on the Live page."""

    tag: TagConfig
    value: str
    unit: str
    dim_value: bool
    badge: StateBadge
    detail: str | None
    updated: str
    bar_width: int | None
    stale: bool


@dataclass(frozen=True)
class GroupView:
    """One Tag Group sub-header with its Tags."""

    group: TagGroupConfig
    interval: str
    last_poll: str
    rows: tuple[TagRow, ...]


@dataclass(frozen=True)
class ConnectionView:
    """One Connection section of the Live page."""

    connection: ConnectionConfig
    protocol_label: str
    address: str
    badge: StateBadge
    detail: str | None
    tag_count: int
    groups: tuple[GroupView, ...]


@dataclass(frozen=True)
class LiveView:
    """Everything the Live body template shows."""

    intro: str
    connections: tuple[ConnectionView, ...]


def format_value(value: TagValue | None) -> str:
    """Format a live value: 2 decimals, thin-space thousands, ``—`` for none."""
    if value is None:
        return NO_VALUE
    raw = value.raw
    if value.value_type is ValueType.BOOLEAN:
        return "True" if raw else "False"
    if value.value_type is ValueType.NUMERIC and isinstance(raw, float):
        return f"{raw:,.2f}".replace(",", THIN_SPACE)
    if value.value_type is ValueType.INTEGER and isinstance(raw, int):
        return f"{raw:,}".replace(",", THIN_SPACE)
    if isinstance(raw, bytes):
        return plural(len(raw), "byte")
    return str(raw)


def freshness_bar(age_s: float, interval_ms: int) -> tuple[int, bool]:
    """Return the freshness bar width in percent and whether the value is stale.

    A negative age (clock skew) counts as just updated, and an interval
    below 1 ms is clamped, so the bar is always 0-100 % and never raises.
    """
    result = freshness(max(0.0, age_s), max(interval_ms, MIN_INTERVAL_MS) / 1000)
    width = min(100, max(0, round(result.fraction * 100)))
    return width, result.stale


def updated_text(age_s: float) -> str:
    """Return ``Updated under a second ago`` or ``Updated 12 s ago``."""
    age = max(0.0, age_s)
    if age < 1:
        return "Updated under a second ago"
    if age < 60:
        return f"Updated {round(age)} s ago"
    return f"Updated {format_age(age)} ago"


def build_live_view(
    host: CollectorHost, config: CollectorConfig, now: datetime
) -> LiveView:
    """Build the Live page from the saved configuration and the live cache."""
    workers = worker_statuses(host)
    values = host.live_values.values()
    groups_by_connection: dict[str, list[TagGroupConfig]] = defaultdict(list)
    for group in config.tag_groups:
        groups_by_connection[group.connection_id].append(group)
    tags_by_group: dict[str, list[TagConfig]] = defaultdict(list)
    for tag in config.tags:
        tags_by_group[tag.tag_group_id].append(tag)

    collecting = 0
    views = []
    for connection in config.connections:
        groups = groups_by_connection[connection.id]
        tag_count = sum(len(tags_by_group[g.id]) for g in groups)
        badge, detail = connection_status(connection, workers)
        group_views: list[GroupView] = []
        if connection.enabled:
            for group in groups:
                tags = tags_by_group[group.id]
                if group.enabled:
                    collecting += sum(1 for t in tags if t.enabled)
                group_views.append(
                    _group_view(host, connection, group, tags, values, now)
                )
        views.append(
            ConnectionView(
                connection=connection,
                protocol_label=form_for(connection.protocol).label,
                address=display_endpoint(connection.endpoint),
                badge=badge,
                detail=detail,
                tag_count=tag_count,
                groups=tuple(group_views),
            )
        )
    intro = (
        f"Collecting {collecting} of {plural(len(config.tags), 'tag')} from "
        f"{plural(len(config.connections), 'connection')}. "
        "Values refresh every second."
    )
    return LiveView(intro=intro, connections=tuple(views))


def _group_view(
    host: CollectorHost,
    connection: ConnectionConfig,
    group: TagGroupConfig,
    tags: list[TagConfig],
    values: Mapping[str, LiveValue],
    now: datetime,
) -> GroupView:
    rows = []
    good = 0
    for tag in tags:
        collected = group.enabled and tag.enabled
        live = values.get(tag.id)
        if (
            live is not None
            and live.connection_id == connection.id
            and live.tag_group_id == group.id
            and collected
        ):
            row = _value_row(tag, live, group, now)
            good += live.result.quality is Quality.GOOD
        else:
            row = _empty_row(tag, collected=collected)
        rows.append(row)
    total = sum(1 for t in tags if t.enabled) if group.enabled else 0
    return GroupView(
        group=group,
        interval=format_interval(group.interval_ms),
        last_poll=_last_poll_text(
            host.live_values.last_poll(group.id),
            connection,
            group,
            good=good,
            total=total,
            now=now,
        ),
        rows=tuple(rows),
    )


def _value_row(
    tag: TagConfig, live: LiveValue, group: TagGroupConfig, now: datetime
) -> TagRow:
    result = live.result
    age_s = (now - result.received_at).total_seconds()
    width, stale = freshness_bar(age_s, group.interval_ms)
    good = result.quality is Quality.GOOD
    if good:
        badge = state_badge("stale") if stale else state_badge("good")
    else:
        badge = state_badge(result.quality.value)
    return TagRow(
        tag=tag,
        value=format_value(result.value),
        unit=_unit(tag),
        dim_value=not good or result.value is None,
        badge=badge,
        detail=None if good else result.error_code,
        updated=updated_text(age_s),
        bar_width=width,
        stale=stale,
    )


def _empty_row(tag: TagConfig, *, collected: bool) -> TagRow:
    badge = (
        state_badge("stopped", "No value yet") if collected else state_badge("disabled")
    )
    return TagRow(
        tag=tag,
        value=NO_VALUE,
        unit=_unit(tag),
        dim_value=True,
        badge=badge,
        detail=None,
        updated="",
        bar_width=None,
        stale=False,
    )


def _unit(tag: TagConfig) -> str:
    unit = tag.metadata.get(UNIT_KEY)
    return unit if isinstance(unit, str) else ""


def _last_poll_text(
    execution: PollExecution | None,
    connection: ConnectionConfig,
    group: TagGroupConfig,
    *,
    good: int,
    total: int,
    now: datetime,
) -> str:
    if not group.enabled:
        return "Not collecting"
    if execution is None or execution.connection_id != connection.id:
        return "No poll yet"
    finished = execution.finished_at or execution.started_at
    age = format_age((now - finished).total_seconds())
    if execution.status is PollStatus.FAILED:
        return f"Last poll failed {age} ago"
    return f"Last poll {age} ago, {good} of {plural(total, 'tag')} good"


def diff_sections(
    diff: ConfigDiff,
) -> list[tuple[str, list[tuple[str, ConfigChange]]]]:
    """Group a diff into Added / Changed / Removed sections for the templates."""
    sections: list[tuple[str, list[tuple[str, ConfigChange]]]] = []
    for heading, changes in (
        ("Added", diff.added),
        ("Changed", diff.changed),
        ("Removed", diff.removed),
    ):
        if changes:
            sections.append((heading, [(_KIND_LABELS[c.kind], c) for c in changes]))
    return sections


# Routes


def create_live_router() -> APIRouter:
    """Create the Live, Review changes, Apply and Enable routes under ``/panel``."""
    router = APIRouter()

    @router.get("/live", include_in_schema=False, dependencies=[AdminRequired])
    async def live(request: Request, stores: PanelStores) -> HTMLResponse:
        view = await _live_view(request, stores)
        return render(
            request, "live.html", {"nav": "live", "title": "Live tags", "view": view}
        )

    @router.get("/live/body", include_in_schema=False, dependencies=[AdminRequired])
    async def live_body(request: Request, stores: PanelStores) -> HTMLResponse:
        view = await _live_view(request, stores)
        return render(request, "_live_body.html", {"view": view})

    @router.get("/changes", include_in_schema=False, dependencies=[AdminRequired])
    async def review_changes(request: Request, stores: PanelStores) -> HTMLResponse:
        pending = await load_pending_changes(request)
        sections = diff_sections(pending.diff) if pending is not None else []
        return render(
            request,
            "changes.html",
            {
                "nav": "live",
                "title": "Review changes",
                "sections": sections,
                "nothing_message": NOTHING_PENDING_MESSAGE,
            },
        )

    @router.post(
        "/apply",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    async def apply_changes(request: Request) -> RedirectResponse:
        host = auth.web_options(request).host
        try:
            await host.apply()
        except CollectorError as error:
            if error.code == "apply_in_progress":
                flash(request, APPLY_IN_PROGRESS_MESSAGE)
            elif error.code == "runtime_not_running":
                flash(request, APPLY_NOT_RUNNING_MESSAGE)
            # Any other failure leaves collection stopped; the banner says so.
        except Exception:
            # The supervisor logs the failure with its error code, and the
            # banner shows collection stopped with that code.
            pass
        else:
            flash(request, APPLIED_MESSAGE)
        return RedirectResponse("/panel/live", status_code=status.HTTP_303_SEE_OTHER)

    @router.post(
        "/connections/{connection_id}/enable",
        include_in_schema=False,
        dependencies=[AdminRequired, SameSiteRequired],
    )
    def enable_connection(
        request: Request, connection_id: str, stores: PanelStores
    ) -> Response:
        existing = stores.config.get_connection(connection_id)
        if existing is None:
            return render(
                request,
                "base.html",
                {
                    "nav": "live",
                    "title": "Connection not found",
                    "message": NOT_FOUND_MESSAGE,
                },
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if not existing.enabled:
            stores.config.upsert_connection(dataclasses.replace(existing, enabled=True))
        flash(request, ENABLED_MESSAGE)
        return RedirectResponse(
            f"/panel/live#{connection_id}", status_code=status.HTTP_303_SEE_OTHER
        )

    return router


async def _live_view(request: Request, stores: Stores) -> LiveView:
    config, _ = await asyncio.to_thread(stores.config.load_active)
    # The live cache and worker states belong to the event loop; read them here.
    return build_live_view(auth.web_options(request).host, config, utc_now())
