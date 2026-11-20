from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from modbus_support import ModbusTestServer, free_port
from pymodbus.exceptions import ConnectionException, ModbusIOException

from plc_data_collector.domain import (
    CollectorError,
    ConnectionConfig,
    Quality,
    TagRequest,
    TagResult,
    TransientCommunicationError,
    ValueType,
)
from plc_data_collector.protocols import create_modbus_driver
from plc_data_collector.protocols.modbus import ModbusClient, ModbusDriver

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def connection(
    endpoint: str,
    *,
    timeout_ms: int = 1000,
    options: Mapping[str, object] | None = None,
) -> ConnectionConfig:
    return ConnectionConfig(
        id="plc",
        protocol="modbus",
        endpoint=endpoint,
        timeout_ms=timeout_ms,
        protocol_options=options or {},
    )


def request(
    tag_id: str,
    address: str,
    data_type: str,
    value_type: ValueType = ValueType.INTEGER,
    **options: object,
) -> TagRequest:
    return TagRequest(
        tag_id=tag_id,
        address=address,
        value_type=value_type,
        protocol_options={"data_type": data_type, **options},
    )


def by_id(results: list[TagResult]) -> dict[str, TagResult]:
    return {result.tag_id: result for result in results}


class FakeResponse:
    def __init__(
        self,
        *,
        registers: list[int] | None = None,
        bits: list[bool] | None = None,
        exception_code: int | None = None,
    ) -> None:
        self.registers = registers or []
        self.bits = bits or []
        self.exception_code = exception_code

    def isError(self) -> bool:
        return self.exception_code is not None


Handler = Callable[[str, int, int, int], FakeResponse]


class FakeClient:
    """Scripted ModbusClient: handler(call, address, count, unit) -> response."""

    def __init__(self, handler: Handler, *, connect_result: bool = True) -> None:
        self.handler = handler
        self.connect_result = connect_result
        self.connected = False
        self.closed = False
        self.calls: list[tuple[str, int, int, int]] = []

    async def connect(self) -> bool:
        self.connected = self.connect_result
        return self.connect_result

    def close(self) -> None:
        self.connected = False
        self.closed = True

    async def _call(
        self, name: str, address: int, count: int, device_id: int
    ) -> FakeResponse:
        self.calls.append((name, address, count, device_id))
        return self.handler(name, address, count, device_id)

    async def read_coils(
        self, address: int, *, count: int, device_id: int
    ) -> FakeResponse:
        return await self._call("coils", address, count, device_id)

    async def read_discrete_inputs(
        self, address: int, *, count: int, device_id: int
    ) -> FakeResponse:
        return await self._call("discrete", address, count, device_id)

    async def read_holding_registers(
        self, address: int, *, count: int, device_id: int
    ) -> FakeResponse:
        return await self._call("holding", address, count, device_id)

    async def read_input_registers(
        self, address: int, *, count: int, device_id: int
    ) -> FakeResponse:
        return await self._call("input", address, count, device_id)


def fake_driver(
    client: FakeClient,
    *,
    options: Mapping[str, object] | None = None,
    timeout_ms: int = 1000,
) -> ModbusDriver:
    return ModbusDriver(
        connection("tcp://127.0.0.1:1502", timeout_ms=timeout_ms, options=options),
        client_factory=lambda host, port, timeout: cast(ModbusClient, client),
        clock=lambda: NOW,
    )


def holding(values: list[int]) -> Handler:
    def handler(name: str, address: int, count: int, unit: int) -> FakeResponse:
        return FakeResponse(registers=values[address : address + count])

    return handler


async def test_reads_every_data_type(modbus_server: ModbusTestServer) -> None:
    server = modbus_server
    server.set_registers(1, "hr", 0, [0xFFFE])  # int16 -2
    server.set_registers(1, "hr", 1, [0x0001, 0x0002])  # uint32 0x00010002
    server.set_registers(1, "hr", 3, [0x3FC0, 0x0000])  # float32 1.5
    server.set_registers(1, "hr", 5, [0x4142, 0x4300])  # string "ABC"
    server.set_registers(1, "hr", 7, [0b0000_0100])  # bit 2 of register
    server.set_registers(1, "ir", 0, [42])
    server.set_bits(1, "co", 0, [True, False, True])
    server.set_bits(1, "di", 0, [False, True])
    await server.start()
    driver = create_modbus_driver(connection(server.endpoint))
    await driver.connect()
    try:
        results = by_id(
            await driver.read(
                [
                    request("i16", "hr:0", "int16"),
                    request("u32", "hr:1", "uint32"),
                    request("f32", "hr:3", "float32", ValueType.NUMERIC),
                    request("str", "hr:5", "string", ValueType.TEXT, length=2),
                    request("bit", "hr:7.2", "bit", ValueType.BOOLEAN),
                    request("ir", "ir:0", "uint16"),
                    request("co", "co:2", "bool", ValueType.BOOLEAN),
                    request("di", "di:1", "bool", ValueType.BOOLEAN),
                ]
            )
        )
    finally:
        await driver.disconnect()

    expected: dict[str, object] = {
        "i16": -2,
        "u32": 0x00010002,
        "f32": 1.5,
        "str": "ABC",
        "bit": True,
        "ir": 42,
        "co": True,
        "di": True,
    }
    for tag_id, value in expected.items():
        result = results[tag_id]
        assert result.quality is Quality.GOOD, (tag_id, result.error_message)
        assert result.value is not None
        assert result.value.raw == value, tag_id
        assert result.received_at is not None
    # hr:0..7 merge into one block; ir, co and di are one block each.
    assert len(server.requests) == 4


async def test_unit_override_reads_other_unit(modbus_server: ModbusTestServer) -> None:
    modbus_server.set_registers(1, "hr", 0, [11])
    modbus_server.set_registers(2, "hr", 0, [22])
    await modbus_server.start()
    driver = create_modbus_driver(connection(modbus_server.endpoint))
    await driver.connect()
    try:
        results = by_id(
            await driver.read(
                [
                    request("a", "hr:0", "uint16"),
                    request("b", "hr:0", "uint16", unit_id=2),
                ]
            )
        )
    finally:
        await driver.disconnect()
    assert results["a"].value is not None and results["a"].value.raw == 11
    assert results["b"].value is not None and results["b"].value.raw == 22


async def test_refused_register_isolates_tag(modbus_server: ModbusTestServer) -> None:
    modbus_server.set_registers(1, "hr", 0, [10])
    modbus_server.set_registers(1, "hr", 2, [30])
    await modbus_server.start()
    driver = create_modbus_driver(connection(modbus_server.endpoint))
    await driver.connect()
    try:
        results = by_id(
            await driver.read(
                [
                    request("a", "hr:0", "int16"),
                    request("b", "hr:1", "int16"),
                    request("c", "hr:2", "int16"),
                ]
            )
        )
    finally:
        await driver.disconnect()
    assert results["a"].quality is Quality.GOOD
    assert results["c"].quality is Quality.GOOD
    assert results["b"].quality is Quality.BAD
    assert results["b"].error_code == "modbus_exception_02"
    assert results["b"].error_message is not None
    assert "has no register 1" in results["b"].error_message


async def test_refused_message_uses_connection_numbering() -> None:
    client = FakeClient(lambda *_: FakeResponse(exception_code=2))
    driver = fake_driver(client, options={"address_numbering": "modicon"})
    await driver.connect()
    [result] = await driver.read([request("a", "hr:5", "int16")])
    assert result.error_message is not None
    assert "has no register 40006" in result.error_message


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        (1, "does not support Holding registers (FC03)"),
        (11, "no answer from unit 1"),
        (4, "Modbus exception 4"),
    ],
)
async def test_exception_messages(code: int, fragment: str) -> None:
    client = FakeClient(lambda *_: FakeResponse(exception_code=code))
    driver = fake_driver(client)
    await driver.connect()
    [result] = await driver.read([request("a", "hr:0", "int16")])
    assert result.error_code == f"modbus_exception_{code:02d}"
    assert result.error_message is not None
    assert fragment in result.error_message


async def test_gateway_exception_goes_per_tag() -> None:
    def handler(name: str, address: int, count: int, unit: int) -> FakeResponse:
        if unit == 3:
            return FakeResponse(exception_code=11)
        return FakeResponse(registers=[address + 100] * count)

    client = FakeClient(handler)
    driver = fake_driver(client)
    await driver.connect()
    results = by_id(
        await driver.read(
            [
                request("dead1", "hr:0", "uint16", unit_id=3),
                request("dead2", "hr:1", "uint16", unit_id=3),
                request("live", "hr:0", "uint16"),
            ]
        )
    )
    assert results["live"].quality is Quality.GOOD
    assert results["dead1"].error_code == "modbus_exception_11"
    assert results["dead2"].error_code == "modbus_exception_11"


async def test_short_response_marks_missing_tags() -> None:
    client = FakeClient(lambda name, address, count, unit: FakeResponse(registers=[7]))
    driver = fake_driver(client)
    await driver.connect()
    results = by_id(
        await driver.read(
            [request("a", "hr:0", "uint16"), request("b", "hr:1", "uint16")]
        )
    )
    assert results["a"].quality is Quality.GOOD
    assert results["b"].error_code == "modbus_short_response"
    assert len(client.calls) == 1


async def test_short_bit_response_marks_missing_tags() -> None:
    client = FakeClient(lambda *_: FakeResponse(bits=[True]))
    driver = fake_driver(client)
    await driver.connect()
    results = by_id(
        await driver.read(
            [
                request("a", "co:0", "bool", ValueType.BOOLEAN),
                request("b", "co:1", "bool", ValueType.BOOLEAN),
            ]
        )
    )
    assert results["a"].quality is Quality.GOOD
    assert results["b"].error_code == "modbus_short_response"


async def test_padded_bits_are_sliced_to_count() -> None:
    client = FakeClient(
        lambda name, address, count, unit: FakeResponse(bits=[True, False] + [True] * 6)
    )
    driver = fake_driver(client)
    await driver.connect()
    results = by_id(
        await driver.read(
            [
                request("a", "co:0", "bool", ValueType.BOOLEAN),
                request("b", "co:1", "bool", ValueType.BOOLEAN),
            ]
        )
    )
    assert results["a"].value is not None and results["a"].value.raw is True
    assert results["b"].value is not None and results["b"].value.raw is False


async def test_decode_failure_affects_only_that_tag() -> None:
    client = FakeClient(
        lambda name, address, count, unit: FakeResponse(
            registers=[0x7FC0, 0, 5][:count]
        )
    )
    driver = fake_driver(client)
    await driver.connect()
    results = by_id(
        await driver.read(
            [
                request("nan", "hr:0", "float32", ValueType.NUMERIC),
                request("ok", "hr:2", "uint16"),
            ]
        )
    )
    assert results["nan"].error_code == "modbus_value_not_finite"
    assert results["ok"].quality is Quality.GOOD


async def test_timeout_mid_block_is_transient() -> None:
    async def slow() -> None:
        await asyncio.sleep(5)

    class SlowClient(FakeClient):
        async def read_holding_registers(
            self, address: int, *, count: int, device_id: int
        ) -> FakeResponse:
            await slow()
            return FakeResponse()

    client = SlowClient(holding([1, 2]))
    driver = fake_driver(client, timeout_ms=100)
    await driver.connect()
    with pytest.raises(TransientCommunicationError) as error:
        await driver.read(
            [request("a", "hr:0", "uint16"), request("b", "co:0", "bool")]
        )
    assert error.value.code == "modbus_timeout"
    assert await driver.health_check() is False


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (ConnectionException("lost"), "modbus_connection_lost"),
        (ModbusIOException("No response received after 0 retries"), "modbus_timeout"),
        (ModbusIOException("socket closed"), "modbus_connection_lost"),
        (OSError("reset"), "modbus_connection_lost"),
        (ValueError("malformed"), "modbus_connection_lost"),
    ],
)
async def test_transport_errors_are_transient(raised: Exception, code: str) -> None:
    def handler(*_: object) -> FakeResponse:
        raise raised

    client = FakeClient(cast(Handler, handler))
    driver = fake_driver(client)
    await driver.connect()
    with pytest.raises(TransientCommunicationError) as error:
        await driver.read([request("a", "hr:0", "uint16")])
    assert error.value.code == code
    assert client.closed


async def test_transient_error_in_fallback_discards_partial_results() -> None:
    def handler(name: str, address: int, count: int, unit: int) -> FakeResponse:
        if count > 1:
            return FakeResponse(exception_code=2)
        if address == 1:
            raise ConnectionException("lost")
        return FakeResponse(registers=[1])

    driver = fake_driver(FakeClient(handler))
    await driver.connect()
    with pytest.raises(TransientCommunicationError):
        await driver.read(
            [request("a", "hr:0", "uint16"), request("b", "hr:1", "uint16")]
        )


async def test_cancellation_propagates() -> None:
    started = asyncio.Event()

    class HangingClient(FakeClient):
        async def read_holding_registers(
            self, address: int, *, count: int, device_id: int
        ) -> FakeResponse:
            started.set()
            await asyncio.sleep(30)
            return FakeResponse()

    driver = fake_driver(HangingClient(holding([1])), timeout_ms=60000)
    await driver.connect()
    task = asyncio.create_task(driver.read([request("a", "hr:0", "uint16")]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_connect_failure_is_transient() -> None:
    driver = create_modbus_driver(
        connection(f"tcp://127.0.0.1:{free_port()}", timeout_ms=500)
    )
    with pytest.raises(TransientCommunicationError) as error:
        await driver.connect()
    assert error.value.code == "modbus_connect_failed"
    assert await driver.health_check() is False


async def test_connect_returning_false_is_transient() -> None:
    client = FakeClient(holding([]), connect_result=False)
    driver = fake_driver(client)
    with pytest.raises(TransientCommunicationError) as error:
        await driver.connect()
    assert error.value.code == "modbus_connect_failed"
    assert client.closed


async def test_connect_timeout_is_transient() -> None:
    class StuckClient(FakeClient):
        async def connect(self) -> bool:
            await asyncio.sleep(30)
            return True

    driver = fake_driver(StuckClient(holding([])), timeout_ms=100)
    with pytest.raises(TransientCommunicationError) as error:
        await driver.connect()
    assert error.value.code == "modbus_connect_failed"


async def test_read_without_connection_is_transient() -> None:
    driver = fake_driver(FakeClient(holding([])))
    with pytest.raises(TransientCommunicationError):
        await driver.read([request("a", "hr:0", "uint16")])


async def test_reconnect_after_server_restart(modbus_server: ModbusTestServer) -> None:
    modbus_server.set_registers(1, "hr", 0, [5])
    await modbus_server.start()
    driver = create_modbus_driver(connection(modbus_server.endpoint, timeout_ms=500))
    tags = [request("a", "hr:0", "uint16")]
    await driver.connect()
    try:
        [first] = await driver.read(tags)
        assert first.quality is Quality.GOOD

        await modbus_server.stop()
        with pytest.raises(TransientCommunicationError):
            await driver.read(tags)
        assert await driver.health_check() is False

        await modbus_server.start()
        await driver.disconnect()
        await driver.connect()
        [again] = await driver.read(tags)
        assert again.quality is Quality.GOOD
        assert again.value is not None and again.value.raw == 5
        assert await driver.health_check() is True
    finally:
        await driver.disconnect()


async def test_invalid_tag_does_not_block_others(
    modbus_server: ModbusTestServer,
) -> None:
    modbus_server.set_registers(1, "hr", 0, [9])
    await modbus_server.start()
    driver = create_modbus_driver(connection(modbus_server.endpoint))
    await driver.connect()
    try:
        results = by_id(
            await driver.read(
                [
                    request("good", "hr:0", "uint16"),
                    request("bad", "hr:0", "nope"),
                    TagRequest(
                        tag_id="bad_addr",
                        address="zz:1",
                        value_type=ValueType.INTEGER,
                        protocol_options={"data_type": "uint16"},
                    ),
                ]
            )
        )
    finally:
        await driver.disconnect()
    assert results["good"].quality is Quality.GOOD
    assert results["bad"].error_code == "modbus_invalid_tag"
    assert results["bad_addr"].error_code == "modbus_invalid_tag"


async def test_results_keep_request_order_and_clock() -> None:
    client = FakeClient(holding([1, 2, 3]))
    driver = fake_driver(client)
    await driver.connect()
    tags = [
        request("c", "hr:2", "uint16"),
        request("x", "hr:0", "nope"),
        request("a", "hr:0", "uint16"),
    ]
    results = await driver.read(tags)
    assert [r.tag_id for r in results] == ["c", "x", "a"]
    assert all(r.received_at == NOW for r in results)
    assert results[0].source_timestamp == NOW


async def test_repeated_reads_reuse_plans() -> None:
    client = FakeClient(holding([1, 2, 3]))
    driver = fake_driver(client)
    await driver.connect()
    tags = [request("a", "hr:0", "uint16"), request("b", "hr:1", "uint16")]
    first = await driver.read(tags)
    second = await driver.read(tags)
    assert [r.value for r in first] == [r.value for r in second]
    assert len(client.calls) == 2  # one merged block per read


async def test_max_gap_option_merges_blocks() -> None:
    client = FakeClient(holding(list(range(10))))
    driver = fake_driver(client, options={"max_gap": 4})
    await driver.connect()
    await driver.read([request("a", "hr:0", "uint16"), request("b", "hr:5", "uint16")])
    assert client.calls == [("holding", 0, 6, 1)]


async def test_capabilities_and_health() -> None:
    client = FakeClient(holding([]))
    driver = fake_driver(client)
    assert driver.capabilities.protocol == "modbus"
    assert driver.capabilities.supports_batch_read
    assert driver.capabilities.supports_health_check
    assert await driver.health_check() is False
    await driver.connect()
    assert await driver.health_check() is True
    await driver.disconnect()
    assert client.closed
    assert await driver.health_check() is False


async def test_per_request_timeout_is_used() -> None:
    class SlowClient(FakeClient):
        async def read_holding_registers(
            self, address: int, *, count: int, device_id: int
        ) -> FakeResponse:
            await asyncio.sleep(1)
            return FakeResponse(registers=[1])

    driver = fake_driver(SlowClient(holding([1])), timeout_ms=60000)
    await driver.connect()
    tag = TagRequest(
        tag_id="a",
        address="hr:0",
        value_type=ValueType.INTEGER,
        timeout_ms=50,
        protocol_options={"data_type": "uint16"},
    )
    with pytest.raises(CollectorError) as error:
        await driver.read([tag])
    assert error.value.code == "modbus_timeout"
