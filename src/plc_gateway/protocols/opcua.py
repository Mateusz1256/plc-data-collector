"""Asynchronous OPC UA communication driver."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from asyncua import ua
from asyncua.client.client import Client
from asyncua.ua.uaerrors import UaError

from plc_gateway.domain import (
    ConfigurationError,
    ConnectionConfig,
    PermanentProtocolError,
    Quality,
    TagRequest,
    TagResult,
    TagValue,
    TransientCommunicationError,
    ValueType,
)
from plc_gateway.protocols.driver import DriverCapabilities

OPCUA_PROTOCOL = "opcua"


class OpcUaNode(Protocol):
    """Subset of asyncua node behavior used by the driver."""

    async def read_data_value(
        self,
        raise_on_bad_status: bool = True,
    ) -> ua.DataValue:
        """Read a node data value."""
        ...


class OpcUaClient(Protocol):
    """Subset of asyncua client behavior used by the driver."""

    def get_node(self, nodeid: object) -> OpcUaNode:
        """Return a node handle for a node id."""
        ...

    def set_security_string(self, string: str) -> None:
        """Configure asyncua security settings from a security string."""
        ...

    def set_user(self, username: str) -> None:
        """Configure username authentication."""
        ...

    def set_password(self, pwd: str) -> None:
        """Configure password authentication."""
        ...

    async def connect(
        self,
        *,
        auto_reconnect: bool | None = None,
        reconnect_max_delay: float | None = None,
        reconnect_request_timeout: float | None = None,
    ) -> None:
        """Open the OPC UA session."""
        ...

    async def disconnect(self) -> None:
        """Close the OPC UA session."""
        ...

    async def check_connection(self) -> None:
        """Raise when the connection is not healthy."""
        ...


ClientFactory = Callable[[str, float], OpcUaClient]


@dataclass(frozen=True, slots=True)
class OpcUaDriverConfig:
    """Validated OPC UA driver options."""

    security_string: str | None = None
    username: str | None = None
    password: str | None = None
    auto_reconnect: bool = False
    reconnect_max_delay_s: float = 30.0
    reconnect_request_timeout_s: float = 60.0

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> OpcUaDriverConfig:
        """Build OPC UA driver config from connection protocol options."""
        return cls(
            security_string=_optional_str(options, "security_string"),
            username=_optional_str(options, "username"),
            password=_optional_str(options, "password"),
            auto_reconnect=_read_bool(options, "auto_reconnect", default=False),
            reconnect_max_delay_s=_read_positive_number(
                options,
                "reconnect_max_delay_s",
                default=30.0,
            ),
            reconnect_request_timeout_s=_read_positive_number(
                options,
                "reconnect_request_timeout_s",
                default=60.0,
            ),
        )


class OpcUaDriver:
    """OPC UA driver backed by one asyncua client instance per connection."""

    def __init__(
        self,
        connection: ConnectionConfig,
        *,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create an OPC UA driver for one configured connection."""
        self._connection = connection
        self._config = OpcUaDriverConfig.from_options(connection.protocol_options)
        self._client_factory = (
            _create_asyncua_client if client_factory is None else client_factory
        )
        self._clock = _utc_now if clock is None else clock
        self._client: OpcUaClient | None = None
        self._connected = False

    @property
    def capabilities(self) -> DriverCapabilities:
        """Return OPC UA driver capabilities."""
        return DriverCapabilities(protocol=OPCUA_PROTOCOL, supports_batch_read=True)

    async def connect(self) -> None:
        """Open a new OPC UA client session."""
        client = self._client_factory(self._connection.endpoint, self._timeout_s())
        self._configure_client(client)
        try:
            await asyncio.wait_for(
                client.connect(
                    auto_reconnect=self._config.auto_reconnect,
                    reconnect_max_delay=self._config.reconnect_max_delay_s,
                    reconnect_request_timeout=(
                        self._config.reconnect_request_timeout_s
                    ),
                ),
                timeout=self._timeout_s(),
            )
        except TimeoutError as error:
            raise TransientCommunicationError(
                "OPC UA connect timed out.",
                code="opcua_connect_timeout",
                details={"connection_id": self._connection.id},
            ) from error
        except UaError as error:
            raise TransientCommunicationError(
                "OPC UA connect failed.",
                code="opcua_connect_failed",
                details={"connection_id": self._connection.id},
            ) from error
        except OSError as error:
            raise TransientCommunicationError(
                "OPC UA socket connect failed.",
                code="opcua_socket_connect_failed",
                details={"connection_id": self._connection.id},
            ) from error

        self._client = client
        self._connected = True

    async def disconnect(self) -> None:
        """Close the OPC UA client session."""
        client = self._client
        self._client = None
        self._connected = False
        if client is None:
            return
        try:
            await asyncio.wait_for(client.disconnect(), timeout=self._timeout_s())
        except TimeoutError as error:
            raise TransientCommunicationError(
                "OPC UA disconnect timed out.",
                code="opcua_disconnect_timeout",
                details={"connection_id": self._connection.id},
            ) from error
        except UaError as error:
            raise TransientCommunicationError(
                "OPC UA disconnect failed.",
                code="opcua_disconnect_failed",
                details={"connection_id": self._connection.id},
            ) from error

    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]:
        """Read a batch of OPC UA nodes and return one result per request."""
        client = self._require_client()
        tasks = [self._read_tag(client, tag) for tag in tags]
        return list(await asyncio.gather(*tasks))

    async def health_check(self) -> bool:
        """Return whether the OPC UA session appears healthy."""
        if self._client is None or not self._connected:
            return False
        try:
            await asyncio.wait_for(
                self._client.check_connection(),
                timeout=self._timeout_s(),
            )
        except TimeoutError:
            self._connected = False
            return False
        except UaError:
            self._connected = False
            return False
        return True

    async def _read_tag(self, client: OpcUaClient, tag: TagRequest) -> TagResult:
        timeout_s = _timeout_s(tag.timeout_ms, self._connection.timeout_ms)
        try:
            node = client.get_node(tag.address)
            data_value = await asyncio.wait_for(
                node.read_data_value(raise_on_bad_status=False),
                timeout=timeout_s,
            )
        except TimeoutError as error:
            return _failure_result(
                tag,
                "opcua_read_timeout",
                "OPC UA read timed out.",
                self._now(),
                error,
            )
        except UaError as error:
            return _failure_result(
                tag,
                "opcua_read_failed",
                "OPC UA read failed.",
                self._now(),
                error,
            )
        except OSError as error:
            return _failure_result(
                tag,
                "opcua_socket_read_failed",
                "OPC UA socket read failed.",
                self._now(),
                error,
            )

        return _tag_result_from_data_value(tag, data_value, self._now())

    def _configure_client(self, client: OpcUaClient) -> None:
        if self._config.security_string is not None:
            client.set_security_string(self._config.security_string)
        if self._config.username is not None:
            client.set_user(self._config.username)
        if self._config.password is not None:
            client.set_password(self._config.password)

    def _require_client(self) -> OpcUaClient:
        if self._client is None or not self._connected:
            raise TransientCommunicationError(
                "OPC UA client is not connected.",
                code="opcua_not_connected",
                details={"connection_id": self._connection.id},
            )
        return self._client

    def _timeout_s(self) -> float:
        return _timeout_s(self._connection.timeout_ms, self._connection.timeout_ms)

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)


def create_opcua_driver(connection: ConnectionConfig) -> OpcUaDriver:
    """Create an OPC UA driver for registry registration."""
    return OpcUaDriver(connection)


def _create_asyncua_client(endpoint: str, timeout_s: float) -> OpcUaClient:
    return cast(OpcUaClient, Client(url=endpoint, timeout=timeout_s))


def _tag_result_from_data_value(
    tag: TagRequest,
    data_value: ua.DataValue,
    received_at: datetime,
) -> TagResult:
    status_code = data_value.StatusCode or ua.StatusCode()
    quality = _quality_from_status(status_code)
    if status_code.is_bad():
        return TagResult.failure(
            tag_id=tag.tag_id,
            error_code="opcua_bad_status",
            error_message=f"OPC UA status {status_code.name}.",
            received_at=received_at,
            source_timestamp=_source_timestamp(data_value),
        )

    variant = data_value.Value
    if variant is None:
        return TagResult.failure(
            tag_id=tag.tag_id,
            error_code="opcua_empty_value",
            error_message="OPC UA data value has no variant payload.",
            received_at=received_at,
            source_timestamp=_source_timestamp(data_value),
        )

    try:
        tag_value = _tag_value_from_variant(tag.value_type, variant.Value)
    except ConfigurationError as error:
        return TagResult.failure(
            tag_id=tag.tag_id,
            error_code=error.code or "opcua_value_type_mismatch",
            error_message=error.message,
            received_at=received_at,
            source_timestamp=_source_timestamp(data_value),
        )

    return TagResult.success(
        tag_id=tag.tag_id,
        value=tag_value,
        source_timestamp=_source_timestamp(data_value) or received_at,
        received_at=received_at,
        quality=quality,
    )


def _failure_result(
    tag: TagRequest,
    error_code: str,
    message: str,
    received_at: datetime,
    error: Exception,
) -> TagResult:
    return TagResult.failure(
        tag_id=tag.tag_id,
        error_code=error_code,
        error_message=f"{message} {error.__class__.__name__}",
        received_at=received_at,
    )


def _tag_value_from_variant(value_type: ValueType, raw_value: object) -> TagValue:
    if value_type is ValueType.NUMERIC:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            raise ConfigurationError(
                "OPC UA numeric value must be int or float.",
                code="opcua_value_type_mismatch",
            )
        return TagValue.numeric(raw_value)
    if value_type is ValueType.INTEGER:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ConfigurationError(
                "OPC UA integer value must be int.",
                code="opcua_value_type_mismatch",
            )
        return TagValue.integer(raw_value)
    if value_type is ValueType.BOOLEAN:
        if not isinstance(raw_value, bool):
            raise ConfigurationError(
                "OPC UA boolean value must be bool.",
                code="opcua_value_type_mismatch",
            )
        return TagValue.boolean(raw_value)
    if value_type is ValueType.TEXT:
        if not isinstance(raw_value, str):
            raise ConfigurationError(
                "OPC UA text value must be str.",
                code="opcua_value_type_mismatch",
            )
        return TagValue.text(raw_value)
    if value_type is ValueType.BINARY:
        if isinstance(raw_value, bytes):
            return TagValue.binary(raw_value)
        if isinstance(raw_value, bytearray):
            return TagValue.binary(bytes(raw_value))
        raise ConfigurationError(
            "OPC UA binary value must be bytes.",
            code="opcua_value_type_mismatch",
        )
    raise PermanentProtocolError(
        f"Unsupported OPC UA value type '{value_type}'.",
        code="opcua_unsupported_value_type",
    )


def _quality_from_status(status_code: ua.StatusCode) -> Quality:
    if status_code.is_good():
        return Quality.GOOD
    if status_code.is_uncertain():
        return Quality.UNCERTAIN
    return Quality.BAD


def _source_timestamp(data_value: ua.DataValue) -> datetime | None:
    if data_value.SourceTimestamp is None:
        return None
    return data_value.SourceTimestamp.astimezone(UTC)


def _timeout_s(timeout_ms: int | None, default_timeout_ms: int) -> float:
    effective_timeout_ms = default_timeout_ms if timeout_ms is None else timeout_ms
    if effective_timeout_ms <= 0:
        raise ConfigurationError(
            "OPC UA timeout must be positive.",
            code="opcua_invalid_timeout",
        )
    return effective_timeout_ms / 1000


def _optional_str(options: Mapping[str, object], key: str) -> str | None:
    value = options.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError(
            f"OPC UA protocol option '{key}' must be a string.",
            code="invalid_opcua_options",
            details={"field": key},
        )
    stripped = value.strip()
    return stripped or None


def _read_bool(options: Mapping[str, object], key: str, *, default: bool) -> bool:
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(
            f"OPC UA protocol option '{key}' must be a boolean.",
            code="invalid_opcua_options",
            details={"field": key},
        )
    return value


def _read_positive_number(
    options: Mapping[str, object],
    key: str,
    *,
    default: float,
) -> float:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigurationError(
            f"OPC UA protocol option '{key}' must be a positive number.",
            code="invalid_opcua_options",
            details={"field": key},
        )
    if value <= 0:
        raise ConfigurationError(
            f"OPC UA protocol option '{key}' must be positive.",
            code="invalid_opcua_options",
            details={"field": key},
        )
    return float(value)


def _utc_now() -> datetime:
    return datetime.now(UTC)
