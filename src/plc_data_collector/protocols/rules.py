"""Per-protocol validation rules for Connections and Tags."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

from plc_data_collector.domain import ConfigurationError, ConnectionConfig, TagConfig
from plc_data_collector.protocols.mbus_tags import (
    MBUS_PROTOCOL,
    MBusRules,
    endpoint_key,
)
from plc_data_collector.protocols.modbus_tags import MODBUS_PROTOCOL, ModbusRules
from plc_data_collector.protocols.s7_tags import S7_PROTOCOL, S7Rules

if TYPE_CHECKING:
    from plc_data_collector.app.config import CollectorConfig


class ProtocolRules(Protocol):
    """Validation a protocol applies to its Connections and Tags."""

    def validate_connection(self, connection: ConnectionConfig) -> None:
        """Raise ``ConfigurationError`` when the Connection is invalid."""
        ...

    def validate_tag(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> dict[str, object]:
        """Return the Tag's protocol_options in stored (normalised) form.

        Raise ``ConfigurationError`` when the Tag is invalid on its Connection.
        """
        ...


class _PermissiveRules:
    """Rules for protocols without extra validation (OPC UA, mock)."""

    def validate_connection(self, connection: ConnectionConfig) -> None:
        return None

    def validate_tag(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> dict[str, object]:
        return dict(tag.protocol_options)


class _OpcUaRules(_PermissiveRules):
    """OPC UA: any address; Tags have no protocol options."""

    def validate_tag(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> dict[str, object]:
        if tag.protocol_options:
            names = ", ".join(repr(name) for name in sorted(tag.protocol_options))
            raise ConfigurationError(
                f"OPC UA Tags take no protocol_options; remove {names}.",
                code="opcua_tag_options_not_supported",
            )
        return {}


_REGISTRY: dict[str, ProtocolRules] = {}
_PERMISSIVE = _PermissiveRules()


def register_rules(protocol: str, rules: ProtocolRules) -> None:
    """Register the rules for a protocol name, replacing any earlier ones."""
    _REGISTRY[protocol] = rules


def rules_for(protocol: str) -> ProtocolRules:
    """Return the rules for a protocol; unknown protocols accept everything."""
    return _REGISTRY.get(protocol, _PERMISSIVE)


register_rules(MODBUS_PROTOCOL, ModbusRules())
register_rules("opcua", _OpcUaRules())
register_rules(S7_PROTOCOL, S7Rules())
register_rules(MBUS_PROTOCOL, MBusRules())


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
    refuse_shared_mbus_endpoints(config.connections)
    group_connection = {group.id: group.connection_id for group in config.tag_groups}
    for tag in config.tags:
        owner = by_id.get(group_connection.get(tag.tag_group_id, ""))
        if owner is None:
            continue
        try:
            rules_for(owner.protocol).validate_tag(tag, owner)
        except ConfigurationError as error:
            raise prefixed(error, f"Tag '{tag.id}'", {"tag_id": tag.id}) from error


def refuse_shared_mbus_endpoints(connections: Iterable[ConnectionConfig]) -> None:
    """Two M-Bus Connections cannot share a gateway or a serial port.

    The error names the earlier Connection and the later one; ``details``
    carry the later one as ``connection_id``.
    """
    seen: dict[tuple[object, ...], str] = {}
    for connection in connections:
        if connection.protocol != MBUS_PROTOCOL:
            continue
        key = endpoint_key(connection.endpoint)
        other = seen.setdefault(key, connection.id)
        if other != connection.id:
            raise ConfigurationError(
                f"Connections '{other}' and '{connection.id}' use the same M-Bus "
                f"endpoint {connection.endpoint!r}; one bus needs one Connection.",
                code="invalid_mbus_connection",
                details={
                    "connection_id": connection.id,
                    "other_connection_id": other,
                },
            )
