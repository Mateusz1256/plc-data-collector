from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from asyncua import ua
from asyncua.server.server import Server
from asyncua.ua import status_codes

from plc_gateway.domain import (
    ConfigurationError,
    ConnectionConfig,
    Quality,
    TagRequest,
    TransientCommunicationError,
    ValueType,
)
from plc_gateway.protocols import OPCUA_PROTOCOL, OpcUaDriver, create_opcua_driver


class FakeOpcUaNode:
    """Fake OPC UA node returning configured data values or errors."""

    def __init__(
        self,
        value: object,
        *,
        status_code: ua.StatusCode | None = None,
        source_timestamp: datetime | None = None,
        error: Exception | None = None,
    ) -> None:
        """Create a fake OPC UA node."""
        self._value = value
        self._status_code = status_code or ua.StatusCode()
        self._source_timestamp = source_timestamp
        self._error = error

    async def read_data_value(
        self,
        raise_on_bad_status: bool = True,
    ) -> ua.DataValue:
        """Return a fake data value."""
        _ = raise_on_bad_status
        if self._error is not None:
            raise self._error
        return ua.DataValue(
            ua.Variant(self._value),
            StatusCode=self._status_code,
            SourceTimestamp=cast(ua.DateTime | None, self._source_timestamp),
        )


class FakeOpcUaClient:
    """Fake asyncua client for unit-level driver tests."""

    def __init__(self, nodes: dict[str, FakeOpcUaNode]) -> None:
        """Create a fake OPC UA client."""
        self.nodes = nodes
        self.connected = False
        self.disconnect_calls = 0
        self.security_string: str | None = None
        self.username: str | None = None
        self.password: str | None = None

    def get_node(self, nodeid: object) -> FakeOpcUaNode:
        """Return a configured fake node."""
        return self.nodes[str(nodeid)]

    def set_security_string(self, string: str) -> None:
        """Record configured security string."""
        self.security_string = string

    def set_user(self, username: str) -> None:
        """Record configured username."""
        self.username = username

    def set_password(self, pwd: str) -> None:
        """Record configured password."""
        self.password = pwd

    async def connect(
        self,
        *,
        auto_reconnect: bool | None = None,
        reconnect_max_delay: float | None = None,
        reconnect_request_timeout: float | None = None,
    ) -> None:
        """Mark the fake client connected."""
        _ = auto_reconnect, reconnect_max_delay, reconnect_request_timeout
        self.connected = True

    async def disconnect(self) -> None:
        """Mark the fake client disconnected."""
        self.disconnect_calls += 1
        self.connected = False

    async def check_connection(self) -> None:
        """Raise when fake client is disconnected."""
        if not self.connected:
            raise OSError("disconnected")


class ClientFactory:
    """Factory recording created fake clients."""

    def __init__(self, nodes: dict[str, FakeOpcUaNode]) -> None:
        """Create a fake client factory."""
        self.nodes = nodes
        self.clients: list[FakeOpcUaClient] = []

    def __call__(self, endpoint: str, timeout_s: float) -> FakeOpcUaClient:
        """Create one fake client."""
        _ = endpoint, timeout_s
        client = FakeOpcUaClient(self.nodes)
        self.clients.append(client)
        return client


def make_connection(
    protocol_options: dict[str, object] | None = None,
) -> ConnectionConfig:
    return ConnectionConfig(
        id="opcua_connection",
        protocol=OPCUA_PROTOCOL,
        endpoint="opc.tcp://127.0.0.1:4840",
        timeout_ms=1000,
        protocol_options=protocol_options or {},
    )


def make_request(
    tag_id: str,
    address: str,
    value_type: ValueType,
) -> TagRequest:
    return TagRequest(tag_id=tag_id, address=address, value_type=value_type)


def _status_code(name: str) -> ua.StatusCode:
    value = cast(ua.UInt32, getattr(status_codes.StatusCodes, name))
    return ua.StatusCode(value)


@pytest.mark.asyncio
async def test_opcua_driver_reads_batch_and_maps_quality() -> None:
    source_timestamp = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    factory = ClientFactory(
        {
            "ns=2;s=temperature": FakeOpcUaNode(
                20.5,
                source_timestamp=source_timestamp,
            ),
            "ns=2;s=counter": FakeOpcUaNode(7),
            "ns=2;s=uncertain": FakeOpcUaNode(
                True,
                status_code=_status_code("Uncertain"),
            ),
            "ns=2;s=missing": FakeOpcUaNode(
                None,
                status_code=_status_code("BadNodeIdUnknown"),
            ),
        }
    )
    driver = OpcUaDriver(make_connection(), client_factory=factory)
    await driver.connect()

    results = await driver.read(
        (
            make_request("temperature", "ns=2;s=temperature", ValueType.NUMERIC),
            make_request("counter", "ns=2;s=counter", ValueType.INTEGER),
            make_request("uncertain", "ns=2;s=uncertain", ValueType.BOOLEAN),
            make_request("missing", "ns=2;s=missing", ValueType.NUMERIC),
        )
    )

    assert [result.tag_id for result in results] == [
        "temperature",
        "counter",
        "uncertain",
        "missing",
    ]
    assert results[0].value is not None
    assert results[0].value.numeric_value == 20.5
    assert results[0].source_timestamp == source_timestamp
    assert results[1].value is not None
    assert results[1].value.integer_value == 7
    assert results[2].quality is Quality.UNCERTAIN
    assert results[3].is_success is False
    assert results[3].error_code == "opcua_bad_status"


@pytest.mark.asyncio
async def test_opcua_driver_maps_single_node_errors_without_failing_batch() -> None:
    factory = ClientFactory(
        {
            "ns=2;s=ok": FakeOpcUaNode("running"),
            "ns=2;s=broken": FakeOpcUaNode(None, error=OSError("socket reset")),
        }
    )
    driver = OpcUaDriver(make_connection(), client_factory=factory)
    await driver.connect()

    results = await driver.read(
        (
            make_request("ok", "ns=2;s=ok", ValueType.TEXT),
            make_request("broken", "ns=2;s=broken", ValueType.NUMERIC),
        )
    )

    assert results[0].is_success is True
    assert results[1].is_success is False
    assert results[1].error_code == "opcua_socket_read_failed"


@pytest.mark.asyncio
async def test_opcua_driver_can_reconnect_with_new_client_instance() -> None:
    factory = ClientFactory({"ns=2;s=ok": FakeOpcUaNode(1)})
    driver = OpcUaDriver(make_connection(), client_factory=factory)

    await driver.connect()
    await driver.disconnect()
    await driver.connect()

    assert len(factory.clients) == 2
    assert factory.clients[0].disconnect_calls == 1
    assert factory.clients[1].connected is True
    assert await driver.health_check() is True


@pytest.mark.asyncio
async def test_opcua_driver_rejects_invalid_options() -> None:
    with pytest.raises(ConfigurationError, match="auto_reconnect"):
        OpcUaDriver(
            make_connection(protocol_options={"auto_reconnect": "yes"}),
            client_factory=ClientFactory({}),
        )


@pytest.mark.asyncio
async def test_opcua_driver_requires_connection_before_read() -> None:
    driver = OpcUaDriver(make_connection(), client_factory=ClientFactory({}))

    with pytest.raises(TransientCommunicationError, match="not connected"):
        await driver.read((make_request("tag", "ns=2;s=tag", ValueType.NUMERIC),))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_opcua_driver_reads_from_local_asyncua_server() -> None:
    endpoint = "opc.tcp://127.0.0.1:48580/plc-gateway-test/"
    server = Server()
    await server.init()
    server.set_endpoint(endpoint)
    namespace_index = await server.register_namespace("urn:plc-gateway:test")
    machine = await server.nodes.objects.add_object(namespace_index, "Machine")
    temperature = await machine.add_variable(
        namespace_index,
        "Temperature",
        21.25,
    )
    counter = await machine.add_variable(namespace_index, "Counter", 4)
    await server.start()
    try:
        driver = create_opcua_driver(
            ConnectionConfig(
                id="local_opcua",
                protocol=OPCUA_PROTOCOL,
                endpoint=endpoint,
                timeout_ms=3000,
            )
        )
        await driver.connect()
        results = await driver.read(
            (
                make_request(
                    "temperature",
                    temperature.nodeid.to_string(),
                    ValueType.NUMERIC,
                ),
                make_request("counter", counter.nodeid.to_string(), ValueType.INTEGER),
                make_request("missing", "ns=2;s=Missing", ValueType.NUMERIC),
            )
        )
        await driver.disconnect()
    finally:
        await server.stop()

    assert results[0].is_success is True
    assert results[0].value is not None
    assert results[0].value.numeric_value == 21.25
    assert results[1].is_success is True
    assert results[1].value is not None
    assert results[1].value.integer_value == 4
    assert results[2].is_success is False
