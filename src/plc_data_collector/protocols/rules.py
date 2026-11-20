"""Per-protocol validation rules for Connections and Tags."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from plc_data_collector.domain import ConfigurationError, ConnectionConfig, TagConfig
from plc_data_collector.protocols.modbus_tags import MODBUS_PROTOCOL, ModbusRules

if TYPE_CHECKING:
    from plc_data_collector.app.config import CollectorConfig


class ProtocolRules(Protocol):
    """Validation a protocol applies to its Connections and Tags."""

    def validate_connection(self, connection: ConnectionConfig) -> None:
        """Raise ``ConfigurationError`` when the Connection is invalid."""
        ...

    def validate_tag(self, tag: TagConfig, connection: ConnectionConfig) -> None:
        """Raise ``ConfigurationError`` when the Tag is invalid on its Connection."""
        ...


class _PermissiveRules:
    """Rules for protocols without extra validation (OPC UA, mock)."""

    def validate_connection(self, connection: ConnectionConfig) -> None:
        return None

    def validate_tag(self, tag: TagConfig, connection: ConnectionConfig) -> None:
        return None


def rules_for(protocol: str) -> ProtocolRules:
    """Return the rules for a protocol; unknown protocols accept everything."""
    if protocol == MODBUS_PROTOCOL:
        return ModbusRules()
    return _PermissiveRules()


def prefixed(
    error: ConfigurationError, prefix: str, details: dict[str, object]
) -> ConfigurationError:
    return ConfigurationError(
        f"{prefix}: {error.message}",
        code=error.code,
        details={**error.details, **details},
    )


def validate_config(config: CollectorConfig) -> None:
    """Validate every Connection, then every Tag with its Connection."""
    by_id = {connection.id: connection for connection in config.connections}
    for connection in config.connections:
        try:
            rules_for(connection.protocol).validate_connection(connection)
        except ConfigurationError as error:
            raise prefixed(
                error,
                f"Connection '{connection.id}'",
                {"connection_id": connection.id},
            ) from error
    group_connection = {group.id: group.connection_id for group in config.tag_groups}
    for tag in config.tags:
        owner = by_id.get(group_connection.get(tag.tag_group_id, ""))
        if owner is None:
            continue
        try:
            rules_for(owner.protocol).validate_tag(tag, owner)
        except ConfigurationError as error:
            raise prefixed(error, f"Tag '{tag.id}'", {"tag_id": tag.id}) from error
