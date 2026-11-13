"""Configuration diff, JSON export and import parsing."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from plc_data_collector.app.config import CollectorConfig, parse_collector_config
from plc_data_collector.app.secrets import merge_secrets, split_secrets
from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
)

MAX_IMPORT_BYTES = 5 * 1024 * 1024

ChangeKind = Literal["connection", "tag_group", "tag"]


@dataclass(frozen=True)
class ConfigChange:
    """One added, changed or removed item; ``label`` is its display name."""

    kind: ChangeKind
    id: str
    label: str


@dataclass(frozen=True)
class ConfigDiff:
    """Differences between two configurations."""

    added: tuple[ConfigChange, ...]
    changed: tuple[ConfigChange, ...]
    removed: tuple[ConfigChange, ...]

    @property
    def total(self) -> int:
        """Return the number of changed items."""
        return len(self.added) + len(self.changed) + len(self.removed)

    @property
    def is_empty(self) -> bool:
        """Return whether the two configurations are identical."""
        return self.total == 0


def diff_configs(current: CollectorConfig, new: CollectorConfig) -> ConfigDiff:
    """Compare every field (secrets included), ordered by kind then id."""
    added: list[ConfigChange] = []
    changed: list[ConfigChange] = []
    removed: list[ConfigChange] = []
    sections: tuple[tuple[ChangeKind, Sequence[_Item], Sequence[_Item]], ...] = (
        ("connection", current.connections, new.connections),
        ("tag_group", current.tag_groups, new.tag_groups),
        ("tag", current.tags, new.tags),
    )
    for kind, old_items, new_items in sections:
        old_by_id = {item.id: item for item in old_items}
        new_by_id = {item.id: item for item in new_items}
        for item_id in sorted(new_by_id.keys() | old_by_id.keys()):
            old, fresh = old_by_id.get(item_id), new_by_id.get(item_id)
            if old is None and fresh is not None:
                added.append(ConfigChange(kind, item_id, _label(fresh)))
            elif fresh is None and old is not None:
                removed.append(ConfigChange(kind, item_id, _label(old)))
            elif old != fresh and fresh is not None:
                changed.append(ConfigChange(kind, item_id, _label(fresh)))
    return ConfigDiff(tuple(added), tuple(changed), tuple(removed))


def export_config(config: CollectorConfig) -> dict[str, object]:
    """Return the configuration-file shape with secrets omitted."""
    return {
        "format_version": 1,
        "connections": [
            {
                "id": connection.id,
                "name": connection.name,
                "protocol": connection.protocol,
                "endpoint": connection.endpoint,
                "enabled": connection.enabled,
                "timeout_ms": connection.timeout_ms,
                "protocol_options": _plain(
                    split_secrets(connection.protocol_options)[0]
                ),
            }
            for connection in config.connections
        ],
        "tag_groups": [
            {
                "id": group.id,
                "connection_id": group.connection_id,
                "interval_ms": group.interval_ms,
                "timeout_ms": group.timeout_ms,
                "overlap_policy": group.overlap_policy.value,
                "enabled": group.enabled,
            }
            for group in config.tag_groups
        ],
        "tags": [
            {
                "id": tag.id,
                "tag_group_id": tag.tag_group_id,
                "name": tag.name,
                "address": tag.address,
                "value_type": tag.value_type.value,
                "enabled": tag.enabled,
                "metadata": _plain(tag.metadata),
            }
            for tag in config.tags
        ],
    }


def parse_import(data: bytes, *, current: CollectorConfig) -> CollectorConfig:
    """Parse an uploaded configuration file.

    Secrets the file omits are kept for Connection IDs that exist in ``current``.
    Environment overrides are not applied.
    """
    return keep_stored_values(parse_import_file(data), current=current)


def parse_import_file(data: bytes) -> CollectorConfig:
    """Validate an uploaded configuration file, without merging stored values.

    Raises ``ConfigurationError`` for a file that is too large, not JSON or
    not a valid configuration.
    """
    if len(data) > MAX_IMPORT_BYTES:
        raise ConfigurationError(
            f"The file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MiB.",
            code="import_too_large",
            details={"max_bytes": MAX_IMPORT_BYTES},
        )
    try:
        loaded = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ConfigurationError(
            f"Invalid JSON: the file is not UTF-8 text (byte {error.start}).",
            code="invalid_configuration_json",
            details={"byte": error.start},
        ) from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"Invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}.",
            code="invalid_configuration_json",
            details={"line": error.lineno, "column": error.colno},
        ) from error
    except (RecursionError, ValueError) as error:
        # Deeply nested input or an integer literal over Python's digit limit.
        raise ConfigurationError(
            "The file is not valid JSON, is nested too deeply or has a number "
            "that is too long.",
            code="invalid_configuration_json",
        ) from error
    if not isinstance(loaded, dict):
        raise ConfigurationError(
            "Configuration root at $ must be a JSON object.",
            code="invalid_configuration_shape",
            details={"location": "$"},
        )

    return parse_collector_config(loaded)


def keep_stored_values(
    config: CollectorConfig, *, current: CollectorConfig
) -> CollectorConfig:
    """Return ``config`` with what files leave out taken from ``current``.

    For Connection IDs that exist in ``current``, secrets the file omits are
    kept; the file's own values win.
    """
    existing = {connection.id: connection for connection in current.connections}
    connections = []
    for connection in config.connections:
        known = existing.get(connection.id)
        if known is not None:
            secrets = split_secrets(known.protocol_options)[1]
            # Existing secrets first, then the file's options on top: file wins.
            options = merge_secrets(secrets, connection.protocol_options)
            connection = replace(connection, protocol_options=options)
        connections.append(connection)
    return replace(config, connections=tuple(connections))


def export_filename(now: datetime) -> str:
    """Return the download file name for an export made at ``now``."""
    return f"plc-data-collector-config-{now:%Y%m%d-%H%M%S}.json"


type _Item = ConnectionConfig | TagGroupConfig | TagConfig


def _label(item: _Item) -> str:
    if isinstance(item, TagGroupConfig):
        return item.id
    return item.name


def _plain(value: object) -> dict[str, object]:
    result = _plain_value(value)
    assert isinstance(result, dict)
    return result


def _plain_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain_value(item) for item in value]
    return value
