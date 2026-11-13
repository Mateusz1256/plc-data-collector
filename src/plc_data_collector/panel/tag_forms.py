"""Tag Group and Tag forms: parsing, generated Tag IDs and the page section.

The forms validate in the Panel's own terms (field errors keyed by field
name); the domain models and ``ConfigStore`` stay the final authority.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.domain import (
    ConfigurationError,
    Quality,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.panel.host import CollectorHost
from plc_data_collector.panel.protocol_forms import (
    MAX_TIMEOUT_MS,
    TIMEOUT_ERROR,
    FormErrors,
    _parse_digits,
    validate_new_id,
)
from plc_data_collector.panel.rendering import StateBadge, format_interval, state_badge
from plc_data_collector.runtime.live_values import freshness

DEFAULT_GROUP_INTERVAL_MS = 1000
DEFAULT_GROUP_TIMEOUT_MS = 800
TIMEOUT_ORDER_ERROR = "Timeout must not be longer than the read interval."
GROUP_CHOICE_ERROR = "Choose a tag group of this connection."
CHOICE_ERROR = "Choose one of the listed options."
GROUP_DUPLICATE_ERROR = "A tag group with this ID already exists. Choose another ID."
UNIT_KEY = "unit"
VALUE_TYPE_CHOICES: tuple[str, ...] = tuple(value.value for value in ValueType)
# Room kept for the "_NN" suffix of a generated ID inside the 128 character cap.
_MAX_ID_LENGTH = 128
_SUFFIX_ROOM = 8


@dataclass(frozen=True)
class FormUi:
    """An open inline form: new or edit, with the values and errors to show."""

    mode: Literal["new", "edit"]
    target_id: str | None
    values: Mapping[str, object]
    errors: Mapping[str, str]


@dataclass(frozen=True)
class GroupRow:
    """One row of the Tag Groups table."""

    group: TagGroupConfig
    tag_count: int


@dataclass(frozen=True)
class TagRow:
    """One row of the Tags table; ``message`` is the error of a bad read."""

    tag: TagConfig
    group_label: str
    badge: StateBadge
    message: str | None

    @property
    def row_class(self) -> str:
        """Return the row highlight class; only bad reads are highlighted."""
        return "row-failed" if self.badge.kind == "failed" else ""


@dataclass(frozen=True)
class TagSection:
    """Everything the Tag Groups and Tags sections of a Connection page need."""

    connection_id: str
    group_rows: list[GroupRow]
    tag_rows: list[TagRow]
    group_choices: list[tuple[str, str]]
    group_ui: FormUi | None
    tag_ui: FormUi | None


def build_tag_section(
    host: CollectorHost,
    config: CollectorConfig,
    connection_id: str,
    *,
    group_ui: FormUi | None = None,
    tag_ui: FormUi | None = None,
    now: datetime | None = None,
) -> TagSection:
    """Collect the Tag Groups and Tags of one Connection for display.

    ``now`` (default: the current time) decides which good values are stale.
    """
    now = now or datetime.now(UTC)
    groups = [g for g in config.tag_groups if g.connection_id == connection_id]
    group_ids = {g.id for g in groups}
    tags_by_group: dict[str, list[TagConfig]] = defaultdict(list)
    for tag in config.tags:
        if tag.tag_group_id in group_ids:
            tags_by_group[tag.tag_group_id].append(tag)
    labels = dict(group_choices(config, connection_id))
    return TagSection(
        connection_id=connection_id,
        group_rows=[GroupRow(g, len(tags_by_group[g.id])) for g in groups],
        tag_rows=[
            _tag_row(host, tag, connection_id, labels[g.id], g, now)
            for g in groups
            for tag in tags_by_group[g.id]
        ],
        group_choices=list(labels.items()),
        group_ui=group_ui,
        tag_ui=tag_ui,
    )


def group_choices(config: CollectorConfig, connection_id: str) -> list[tuple[str, str]]:
    """Return ``(id, "id (interval)")`` for each Tag Group of a Connection."""
    return [
        (g.id, f"{g.id} ({format_interval(g.interval_ms)})")
        for g in config.tag_groups
        if g.connection_id == connection_id
    ]


def _tag_row(
    host: CollectorHost,
    tag: TagConfig,
    connection_id: str,
    group_label: str,
    group: TagGroupConfig,
    now: datetime,
) -> TagRow:
    if not tag.enabled or not group.enabled:
        return TagRow(tag, group_label, state_badge("disabled"), None)
    live = host.live_values.get(tag.id)
    if live is None or live.connection_id != connection_id:
        return TagRow(tag, group_label, state_badge("disabled", "Not read yet"), None)
    result = live.result
    badge = state_badge(result.quality.value)
    message = None
    if result.quality is not Quality.GOOD:
        message = result.error_message or result.error_code
    else:
        # Same rule as the Live page: older than two intervals is stale.
        age_s = max(0.0, (now - result.received_at).total_seconds())
        if freshness(age_s, max(group.interval_ms, 1) / 1000).stale:
            badge = state_badge("stale")
    return TagRow(tag, group_label, badge, message)


# Tag Group form


def group_defaults() -> dict[str, object]:
    """Return the values a new Tag Group form starts with."""
    return {
        "id": "",
        "interval_ms": str(DEFAULT_GROUP_INTERVAL_MS),
        "timeout_ms": str(DEFAULT_GROUP_TIMEOUT_MS),
        "enabled": True,
    }


def group_values(group: TagGroupConfig) -> dict[str, object]:
    """Return the form values of a saved Tag Group."""
    return {
        "id": group.id,
        "interval_ms": str(group.interval_ms),
        "timeout_ms": str(group.timeout_ms),
        "enabled": group.enabled,
    }


def group_values_from_data(data: Mapping[str, str]) -> dict[str, object]:
    """Return submitted Tag Group values to show again."""
    return {
        "id": data.get("id", ""),
        "interval_ms": data.get("interval_ms", ""),
        "timeout_ms": data.get("timeout_ms", ""),
        "enabled": bool(data.get("enabled")),
    }


def group_from_form(
    data: Mapping[str, str],
    *,
    connection_id: str,
    existing: TagGroupConfig | None,
) -> TagGroupConfig:
    """Build a Tag Group from submitted ``data``.

    A new group takes its ID from ``data``; an existing one keeps its ID,
    Connection and overlap policy whatever the form says. Raises
    ``FormErrors`` keyed by field name.
    """
    errors: dict[str, str] = {}
    if existing is None:
        group_id = data.get("id", "").strip()
        id_error = validate_new_id(group_id)
        if id_error is not None:
            errors["id"] = id_error
    else:
        group_id = existing.id

    interval = _parse_ms(data.get("interval_ms", ""))
    if interval is None:
        errors["interval_ms"] = TIMEOUT_ERROR
    raw_timeout = data.get("timeout_ms", "")
    if not raw_timeout.strip():
        timeout = min(DEFAULT_GROUP_TIMEOUT_MS, interval or DEFAULT_GROUP_TIMEOUT_MS)
    else:
        parsed = _parse_ms(raw_timeout)
        if parsed is None:
            errors["timeout_ms"] = TIMEOUT_ERROR
        timeout = parsed or DEFAULT_GROUP_TIMEOUT_MS
    if interval is not None and "timeout_ms" not in errors and timeout > interval:
        errors["timeout_ms"] = TIMEOUT_ORDER_ERROR
    if errors or interval is None:
        raise FormErrors(errors)

    try:
        if existing is None:
            return TagGroupConfig(
                id=group_id,
                connection_id=connection_id,
                interval_ms=interval,
                timeout_ms=timeout,
                enabled=bool(data.get("enabled")),
            )
        return TagGroupConfig(
            id=existing.id,
            connection_id=existing.connection_id,
            interval_ms=interval,
            timeout_ms=timeout,
            overlap_policy=existing.overlap_policy,
            enabled=bool(data.get("enabled")),
        )
    except ConfigurationError as error:
        raise FormErrors({"form": error.message}) from None


def _parse_ms(raw: str) -> int | None:
    value = _parse_digits(raw)
    if value is None or not 1 <= value <= MAX_TIMEOUT_MS:
        return None
    return value


# Tag form


def tag_defaults(group_choices: list[tuple[str, str]]) -> dict[str, object]:
    """Return the values a new Tag form starts with."""
    return {
        "name": "",
        "tag_group_id": group_choices[0][0] if group_choices else "",
        "address": "",
        "value_type": ValueType.NUMERIC.value,
        "unit": "",
        "enabled": True,
    }


def tag_values(tag: TagConfig) -> dict[str, object]:
    """Return the form values of a saved Tag."""
    unit = tag.metadata.get(UNIT_KEY)
    return {
        "name": tag.name,
        "tag_group_id": tag.tag_group_id,
        "address": tag.address,
        "value_type": tag.value_type.value,
        "unit": unit if isinstance(unit, str) else "",
        "enabled": tag.enabled,
    }


def tag_values_from_data(data: Mapping[str, str]) -> dict[str, object]:
    """Return submitted Tag values to show again."""
    return {
        "name": data.get("name", ""),
        "tag_group_id": data.get("tag_group_id", ""),
        "address": data.get("address", ""),
        "value_type": data.get("value_type", ""),
        "unit": data.get("unit", ""),
        "enabled": bool(data.get("enabled")),
    }


def tag_from_form(
    data: Mapping[str, str],
    *,
    tag_id: str | None,
    allowed_group_ids: Collection[str],
    existing: TagConfig | None,
) -> TagConfig:
    """Build a Tag from submitted ``data`` under the given ``tag_id``.

    ``allowed_group_ids`` are the Tag Groups of the Tag's own Connection; a
    group outside them is refused. Metadata other than the unit is kept from
    ``existing``. Raises ``FormErrors`` keyed by field name.
    """
    errors: dict[str, str] = {}
    name = data.get("name", "").strip()
    if not name:
        errors["name"] = "Enter a name."
    address = data.get("address", "").strip()
    if not address:
        errors["address"] = "Enter an address."
    group_id = data.get("tag_group_id", "").strip()
    if group_id not in allowed_group_ids:
        errors["tag_group_id"] = GROUP_CHOICE_ERROR
    raw_type = data.get("value_type", "").strip()
    if raw_type not in VALUE_TYPE_CHOICES:
        errors["value_type"] = CHOICE_ERROR
    if errors or tag_id is None:
        raise FormErrors(errors)

    metadata: dict[str, object] = dict(existing.metadata) if existing else {}
    unit = data.get("unit", "").strip()
    if unit:
        metadata[UNIT_KEY] = unit
    else:
        metadata.pop(UNIT_KEY, None)
    try:
        return TagConfig(
            id=tag_id,
            tag_group_id=group_id,
            name=name,
            address=address,
            value_type=ValueType(raw_type),
            enabled=bool(data.get("enabled")),
            metadata=metadata,
        )
    except ConfigurationError as error:
        raise FormErrors({"form": error.message}) from None


def generate_tag_id(connection_id: str, name: str, taken: Collection[str]) -> str:
    """Return a Tag ID such as ``line1_opcua_temperature``.

    The name is lowercased, runs of characters outside ``[a-z0-9]`` become one
    ``_``, and the Connection ID is the prefix. On a collision with any ID in
    ``taken`` (active or archived, so history never merges by accident) ``_2``,
    ``_3`` and so on is appended.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "tag"
    base = f"{connection_id}_{slug}"[: _MAX_ID_LENGTH - _SUFFIX_ROOM]
    candidate = base
    number = 2
    while candidate in taken:
        candidate = f"{base}_{number}"
        number += 1
    return candidate
