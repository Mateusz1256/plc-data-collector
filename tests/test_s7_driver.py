"""S7 driver tests against the python-snap7 server and a byte-level fake."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime

import pytest
from net_support import free_port
from s7_support import (
    AREA_DB,
    AREA_I,
    AREA_M,
    AREA_Q,
    SERVER_ORDER_CODE,
    FakeItem,
    FakeTransport,
    S7TestServer,
)

from plc_data_collector.app.connection_test import probe_connection
from plc_data_collector.domain import (
    ConnectionConfig,
    Quality,
    TagConfig,
    TagGroupConfig,
    TagRequest,
    TagResult,
    TransientCommunicationError,
    ValueType,
)
from plc_data_collector.protocols import (
    S7_PROTOCOL,
    build_driver_registry,
    create_s7_driver,
    default_driver_registry,
)
from plc_data_collector.protocols.driver import DescribesConnection
from plc_data_collector.protocols.s7 import S7Driver, Snap7Transport
from plc_data_collector.runtime import ConnectionWorker

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
LOGGER_NAME = "plc_data_collector.protocols.s7"

PUT_GET_MESSAGE = (
    "The PLC refused the read. On S7-1200/1500: enable 'Permit access with "
    "PUT/GET communication from remote partner' in the CPU's Protection & "
    "Security settings, then download the hardware configuration."
)


def db_message(db: int) -> str:
    return (
        f"The PLC has no DB{db} of this size. Check the DB number and offset; on "
        "S7-1200/1500 also switch off 'Optimized block access' in the DB's "
        "properties and download it."
    )


def connection(
    endpoint: str = "tcp://127.0.0.1:102",
    *,
    timeout_ms: int = 1000,
    options: Mapping[str, object] | None = None,
) -> ConnectionConfig:
    return ConnectionConfig(
        id="press",
        protocol="s7",
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


def fake_driver(
    transport: FakeTransport,
    *,
    timeout_ms: int = 1000,
    options: Mapping[str, object] | None = None,
) -> S7Driver:
    return S7Driver(
        connection(timeout_ms=timeout_ms, options=options),
        transport_factory=lambda: transport,
        clock=lambda: NOW,
    )


# DB1: one value of every data type at a known offset (big-endian).
DB1 = bytearray(100)
DB1[0] = 0b0000_0101
DB1[1] = 0xAB
struct.pack_into(">H", DB1, 2, 0x1234)
struct.pack_into(">I", DB1, 4, 0xDEADBEEF)
struct.pack_into(">Q", DB1, 8, 0x0102030405060708)
DB1[16] = ord("Z")
struct.pack_into(">b", DB1, 17, -2)
DB1[18] = 200
struct.pack_into(">h", DB1, 20, -1234)
struct.pack_into(">H", DB1, 22, 65000)
struct.pack_into(">i", DB1, 24, -100000)
struct.pack_into(">I", DB1, 28, 4_000_000_000)
struct.pack_into(">q", DB1, 32, -5_000_000_000)
struct.pack_into(">Q", DB1, 40, 10_000_000_000)
struct.pack_into(">f", DB1, 48, 1.5)
struct.pack_into(">d", DB1, 52, -2.25)
DB1[60:72] = bytes((10, 5)) + b"Hello" + bytes(5)
struct.pack_into(">i", DB1, 72, -5000)

MERKERS = bytearray(16)
MERKERS[0] = 0b0000_1000
struct.pack_into(">h", MERKERS, 2, 77)
INPUTS = bytes((0x0F, 0x00))
OUTPUTS = struct.pack(">H", 5)

EVERY_TYPE = [
    request("bool0", "DB1.0.0", "bool", ValueType.BOOLEAN),
    request("bool1", "DB1.0.1", "bool", ValueType.BOOLEAN),
    request("byte", "DB1.1", "byte"),
    request("word", "DB1.2", "word"),
    request("dword", "DB1.4", "dword"),
    request("lword", "DB1.8", "lword"),
    request("char", "DB1.16", "char", ValueType.TEXT),
    request("sint", "DB1.17", "sint"),
    request("usint", "DB1.18", "usint"),
    request("int", "DB1.20", "int"),
    request("uint", "DB1.22", "uint"),
    request("dint", "DB1.24", "dint"),
    request("udint", "DB1.28", "udint"),
    request("lint", "DB1.32", "lint"),
    request("ulint", "DB1.40", "ulint"),
    request("real", "DB1.48", "real", ValueType.NUMERIC),
    request("lreal", "DB1.52", "lreal", ValueType.NUMERIC),
    request("string", "DB1.60", "string", ValueType.TEXT, length=10),
    request("time", "DB1.72", "time"),
    request("m_bit", "M0.3", "bool", ValueType.BOOLEAN),
    request("m_int", "M2", "int"),
    request("i_bit", "I0.3", "bool", ValueType.BOOLEAN),
    request("q_word", "Q0", "uint"),
]
EVERY_TYPE_VALUES: dict[str, object] = {
    "bool0": True,
    "bool1": False,
    "byte": 0xAB,
    "word": 0x1234,
    "dword": 0xDEADBEEF,
    "lword": 0x0102030405060708,
    "char": "Z",
    "sint": -2,
    "usint": 200,
    "int": -1234,
    "uint": 65000,
    "dint": -100000,
    "udint": 4_000_000_000,
    "lint": -5_000_000_000,
    "ulint": 10_000_000_000,
    "real": 1.5,
    "lreal": -2.25,
    "string": "Hello",
    "time": -5000,
    "m_bit": True,
    "m_int": 77,
    "i_bit": True,
    "q_word": 5,
}


def fill(server: S7TestServer) -> None:
    server.set_area(AREA_DB, bytes(DB1), db=1)
    server.set_area(AREA_M, bytes(MERKERS))
    server.set_area(AREA_I, INPUTS)
    server.set_area(AREA_Q, OUTPUTS)


class CountingTransport(Snap7Transport):
    """The real transport, counting request PDUs."""

    def __init__(self) -> None:
        super().__init__()
        self.exchanges = 0

    async def exchange(self, pdu: bytes, timeout_s: float) -> bytes:
        self.exchanges += 1
        return await super().exchange(pdu, timeout_s)


def real_driver(
    server: S7TestServer,
    transport: Snap7Transport | None = None,
    *,
    timeout_ms: int = 1000,
) -> S7Driver:
    chosen = transport or Snap7Transport()
    return S7Driver(
        connection(server.endpoint, timeout_ms=timeout_ms),
        transport_factory=lambda: chosen,
    )


# -- against the python-snap7 server ---------------------------------------


async def test_reads_every_data_type(s7_server: S7TestServer) -> None:
    fill(s7_server)
    await s7_server.start()
    transport = CountingTransport()
    driver = real_driver(s7_server, transport)
    await driver.connect()
    try:
        results = by_id(await driver.read(EVERY_TYPE))
    finally:
        await driver.disconnect()
    for tag_id, value in EVERY_TYPE_VALUES.items():
        result = results[tag_id]
        assert result.quality is Quality.GOOD, (tag_id, result.error_message)
        assert result.value is not None
        assert result.value.raw == value, tag_id
    # DB1 (one block), M, I and Q blocks are packed into one request.
    assert transport.exchanges == 1


async def test_merged_block_of_many_tags_uses_one_request(
    s7_server: S7TestServer,
) -> None:
    s7_server.set_area(AREA_DB, bytes(range(200)), db=1)
    await s7_server.start()
    transport = CountingTransport()
    driver = real_driver(s7_server, transport)
    tags = [request(f"t{n}", f"DB1.{n * 10}", "byte") for n in range(20)]
    await driver.connect()
    try:
        results = await driver.read(tags)
        assert transport.exchanges == 1
        await driver.read(tags)
        assert transport.exchanges == 2
    finally:
        await driver.disconnect()
    assert [r.value.raw if r.value else None for r in results] == [
        n * 10 for n in range(20)
    ]


async def test_missing_db_isolates_its_tag(s7_server: S7TestServer) -> None:
    fill(s7_server)
    await s7_server.start()
    driver = real_driver(s7_server)
    await driver.connect()
    try:
        results = by_id(
            await driver.read(
                [
                    request("a", "DB1.1", "byte"),
                    request("missing", "DB2.0", "int"),
                    request("b", "DB1.2", "word"),
                    request("past_db_end", "DB1.98", "dword"),
                    request("past_m_end", "M20", "byte"),
                    request("m", "M2", "int"),
                ]
            )
        )
    finally:
        await driver.disconnect()
    assert results["a"].quality is Quality.GOOD
    assert results["b"].quality is Quality.GOOD
    assert results["m"].quality is Quality.GOOD
    assert results["missing"].error_code == "s7_db_not_found"
    assert results["missing"].error_message == db_message(2)
    assert results["past_db_end"].error_code == "s7_address_out_of_range"
    assert results["past_db_end"].error_message == db_message(1)
    assert results["past_m_end"].error_code == "s7_address_out_of_range"
    assert "M20" in (results["past_m_end"].error_message or "")


async def test_connection_details_from_server(s7_server: S7TestServer) -> None:
    await s7_server.start()
    driver = real_driver(s7_server)
    assert isinstance(driver, DescribesConnection)
    assert driver.connection_details() is None
    await driver.connect()
    try:
        assert driver.connection_details() == (
            f"PDU 480 bytes, CPU {SERVER_ORDER_CODE}"
        )
    finally:
        await driver.disconnect()


async def test_test_connection_message_includes_details(
    s7_server: S7TestServer,
) -> None:
    fill(s7_server)
    await s7_server.start()
    probe = TagConfig(
        id="speed",
        tag_group_id="g",
        name="Speed",
        address="DB1.20",
        value_type=ValueType.INTEGER,
        protocol_options={"data_type": "int"},
    )
    result = await probe_connection(
        connection(s7_server.endpoint), default_driver_registry(), probe_tag=probe
    )
    assert result.ok, result.message
    assert result.message == (
        f"Connected in {result.elapsed_ms} ms (PDU 480 bytes, CPU "
        f"{SERVER_ORDER_CODE}). Read DB1.20 without errors."
    )


async def test_read_timeout_from_server_is_transient(
    s7_server: S7TestServer,
) -> None:
    fill(s7_server)
    await s7_server.start()
    driver = real_driver(s7_server, timeout_ms=300)
    tags = [request("a", "DB1.1", "byte")]
    await driver.connect()
    try:
        s7_server.stall_reads()
        with pytest.raises(TransientCommunicationError) as error:
            await asyncio.wait_for(driver.read(tags), timeout=5)
        assert error.value.code == "s7_timeout"
        with pytest.raises(TransientCommunicationError) as again:
            await driver.read(tags)
        assert again.value.code == "s7_timeout"
        assert await driver.health_check() is False
    finally:
        s7_server.release_reads()
        await driver.disconnect()


async def test_lost_connection_and_reconnect(s7_server: S7TestServer) -> None:
    fill(s7_server)
    await s7_server.start()
    driver = create_s7_driver(connection(s7_server.endpoint, timeout_ms=500))
    tags = [request("a", "DB1.1", "byte")]
    await driver.connect()
    try:
        [first] = await driver.read(tags)
        assert first.quality is Quality.GOOD

        await s7_server.stop()
        # The server's client handler may answer one request already received.
        with pytest.raises(TransientCommunicationError) as error:
            for _ in range(3):
                await driver.read(tags)
        assert error.value.code == "s7_connection_lost"
        with pytest.raises(TransientCommunicationError) as again:
            await driver.read(tags)
        assert again.value.code == "s7_connection_lost"

        await s7_server.start()
        await driver.connect()
        [back] = await driver.read(tags)
        assert back.value is not None and back.value.raw == 0xAB
    finally:
        await driver.disconnect()


async def test_connect_to_closed_port_fails() -> None:
    driver = create_s7_driver(
        connection(f"tcp://127.0.0.1:{free_port()}", timeout_ms=500)
    )
    with pytest.raises(TransientCommunicationError) as error:
        await driver.connect()
    assert error.value.code == "s7_connect_failed"
    assert await driver.health_check() is False


@contextlib.asynccontextmanager
async def silent_server() -> AsyncIterator[str]:
    """A TCP server that accepts connections and never answers."""

    async def swallow(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while await reader.read(1024):
                pass
        finally:
            writer.close()

    server = await asyncio.start_server(swallow, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"tcp://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def test_connect_honours_connection_timeout() -> None:
    async with silent_server() as endpoint:
        driver = create_s7_driver(connection(endpoint, timeout_ms=200))
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(TransientCommunicationError) as error:
            await driver.connect()
        elapsed = loop.time() - started
    assert error.value.code == "s7_connect_failed"
    assert elapsed < 2  # snap7's own connect timeout is 5 s


# -- byte-level fake ---------------------------------------------------------


async def test_string_254_at_pdu_240_reads_across_items() -> None:
    text = bytes(range(0x41, 0x41 + 26)) * 9 + b"0123456789-+"
    assert len(text) == 246
    data = bytes((254, len(text))) + text + bytes(8)
    transport = FakeTransport(
        memory={(AREA_DB, 1): bytes(4) + data + bytes(4)}, pdu_length=240
    )
    driver = fake_driver(transport)
    await driver.connect()
    [result] = await driver.read(
        [request("s", "DB1.4", "string", ValueType.TEXT, length=254)]
    )
    assert result.quality is Quality.GOOD, result.error_message
    assert result.value is not None and result.value.raw == text.decode("latin-1")
    items = [item for request_items in transport.requests for item in request_items]
    assert sum(item.length for item in items) == 256
    assert all(item.length <= 222 for item in items)
    assert driver.connection_details() == "PDU 240 bytes, CPU 6ES7 214-1AG40-0XB0"


async def test_connection_details_without_cpu_description() -> None:
    driver = fake_driver(FakeTransport(cpu=None, pdu_length=240))
    await driver.connect()
    assert driver.connection_details() == "PDU 240 bytes"


async def test_cpu_description_is_asked_once_per_driver() -> None:
    transport = FakeTransport()
    driver = fake_driver(transport)
    await driver.connect()
    await driver.connect()
    assert transport.connects == 2
    assert transport.cpu_requests == 1


async def test_header_error_refuses_every_tag_without_reconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport(
        memory={(AREA_DB, 1): bytes(DB1), (AREA_M, 0): bytes(MERKERS)},
        header_error=0x8104,
    )
    driver = fake_driver(transport)
    tags = [
        request("a", "DB1.20", "int"),
        request("b", "DB1.48", "real", ValueType.NUMERIC),
        request("m", "M2", "int"),
    ]
    await driver.connect()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        for _ in range(3):
            results = await driver.read(tags)
            assert [r.error_code for r in results] == ["s7_access_denied"] * 3
            assert {r.error_message for r in results} == {PUT_GET_MESSAGE}
            assert await driver.health_check() is True
        refused = [r for r in caplog.records if r.getMessage() == "s7_reads_refused"]
        assert len(refused) == 1
        assert refused[0].levelno == logging.WARNING
        assert refused[0].__dict__["connection_id"] == "press"
        # No per-Tag re-read: one request per poll.
        assert len(transport.requests) == 3
        assert transport.connects == 1
        assert transport.closes == 0

        transport.header_error = 0
        results = await driver.read(tags)
        assert [r.quality for r in results] == [Quality.GOOD] * 3

        transport.header_error = 0x8104
        await driver.read(tags)
        refused = [r for r in caplog.records if r.getMessage() == "s7_reads_refused"]
        assert len(refused) == 2


async def test_other_header_errors_name_class_and_code() -> None:
    transport = FakeTransport(memory={(AREA_DB, 1): bytes(DB1)}, header_error=0x8500)
    driver = fake_driver(transport)
    tags = [request("a", "DB1.20", "int"), request("b", "DB1.22", "int")]
    await driver.connect()

    results = await driver.read(tags)

    assert [r.error_code for r in results] == ["s7_access_denied"] * 2
    assert {r.error_message for r in results} == {
        "The PLC refused the read (error class 0x85, code 0x00)."
    }
    assert len(transport.requests) == 1
    assert transport.connects == 1
    assert transport.closes == 0


async def test_requests_carry_at_most_20_items() -> None:
    transport = FakeTransport(memory={(AREA_DB, 1): bytes(1000)}, pdu_length=480)
    driver = fake_driver(transport, options={"max_gap": 0})
    tags = [request(f"t{n}", f"DB1.{n * 4}", "byte") for n in range(45)]
    await driver.connect()

    results = await driver.read(tags)

    assert [len(items) for items in transport.requests] == [20, 20, 5]
    assert all(r.quality is Quality.GOOD for r in results)


class OneRefusedRequest(FakeTransport):
    """Rejects (header error) only the requests that read DB2."""

    async def exchange(self, pdu: bytes, timeout_s: float) -> bytes:
        response = await super().exchange(pdu, timeout_s)
        if any(item.db == 2 for item in self.requests[-1]):
            return response[:6] + bytes(4) + b"\x81\x04"
        return response


async def test_header_error_affects_only_tags_of_that_request() -> None:
    # 19 separate DB1 items fill the first 240-byte request; the 20th and DB2
    # go in the next.
    transport = OneRefusedRequest(
        memory={(AREA_DB, 1): bytes(1000), (AREA_DB, 2): bytes(10)}, pdu_length=240
    )
    driver = fake_driver(transport, options={"max_gap": 0})
    tags = [request(f"t{n}", f"DB1.{n * 40}", "dword") for n in range(20)]
    tags.append(request("other", "DB2.0", "int"))
    await driver.connect()
    results = by_id(await driver.read(tags))
    assert len(transport.requests) == 2
    assert results["other"].error_code == "s7_access_denied"
    assert all(results[f"t{n}"].quality is Quality.GOOD for n in range(19))
    assert results["t19"].error_code == "s7_access_denied"


@pytest.mark.parametrize(
    ("address", "code", "error_code", "fragment"),
    [
        ("DB10.4", 0x0A, "s7_db_not_found", db_message(10)),
        ("DB10.4", 0x05, "s7_address_out_of_range", db_message(10)),
        ("M4", 0x0A, "s7_address_out_of_range", "M4"),
        ("I4", 0x05, "s7_address_out_of_range", "I4"),
        ("DB10.4", 0x03, "s7_access_denied", "DB10.4"),
        ("Q4", 0x06, "s7_type_not_supported", "Q4"),
        ("DB10.4", 0x42, "s7_item_error_42", "42"),
    ],
)
async def test_item_return_codes(
    address: str, code: int, error_code: str, fragment: str
) -> None:
    transport = FakeTransport(refuse=lambda item: code if item.start == 4 else None)
    driver = fake_driver(transport)
    await driver.connect()
    [result] = await driver.read([request("t", address, "int")])
    assert result.error_code == error_code
    assert fragment in (result.error_message or "")


THREE_TAGS = [
    request("a", "DB1.0", "int"),
    request("b", "DB1.2", "int"),
    request("c", "DB1.4", "int"),
]


def refuse_merged_and(*starts: int) -> FakeTransport:
    """Refuse (0x05) every multi-Tag item and single items at ``starts``."""

    def refuse(item: FakeItem) -> int | None:
        if item.length > 2 or item.start in starts:
            return 0x05
        return None

    return FakeTransport(memory={(AREA_DB, 1): bytes(range(6))}, refuse=refuse)


async def test_refused_merged_item_is_reread_per_tag_and_split_remembered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = refuse_merged_and(2)
    driver = fake_driver(transport)
    await driver.connect()
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        results = by_id(await driver.read(THREE_TAGS))
        assert results["a"].value is not None and results["a"].value.raw == 0x0001
        assert results["b"].error_code == "s7_address_out_of_range"
        assert results["c"].value is not None and results["c"].value.raw == 0x0405
        assert [[i.length for i in r] for r in transport.requests] == [[6], [2, 2, 2]]
        transport.requests.clear()
        await driver.read(THREE_TAGS)
        assert [[i.length for i in r] for r in transport.requests] == [[2, 2, 2]]
    logged = [
        r for r in caplog.records if r.getMessage() == "s7_block_split_remembered"
    ]
    assert len(logged) == 1
    assert logged[0].levelno == logging.INFO
    assert logged[0].__dict__["connection_id"] == "press"
    assert logged[0].__dict__["return_code"] == 0x05


async def test_refused_merged_item_without_bad_member_is_not_remembered() -> None:
    transport = refuse_merged_and()
    driver = fake_driver(transport)
    await driver.connect()
    results = await driver.read(THREE_TAGS)
    assert [r.quality for r in results] == [Quality.GOOD] * 3
    transport.requests.clear()
    await driver.read(THREE_TAGS)
    assert [i.length for i in transport.requests[0]] == [6]  # whole block first


async def test_invalid_tag_is_bad_and_others_read() -> None:
    transport = FakeTransport(memory={(AREA_DB, 1): bytes(DB1)})
    driver = fake_driver(transport)
    await driver.connect()
    results = by_id(
        await driver.read(
            [
                request("good", "DB1.20", "int"),
                request("bad_type", "DB1.20", "nope"),
                request("bad_address", "DB0.1", "int"),
            ]
        )
    )
    assert results["good"].value is not None and results["good"].value.raw == -1234
    assert results["bad_type"].error_code == "s7_invalid_tag"
    assert results["bad_address"].error_code == "s7_invalid_tag"
    assert "DB0.1" in (results["bad_address"].error_message or "")


async def test_decode_failure_affects_only_that_tag() -> None:
    memory = bytearray(DB1)
    memory[60:62] = bytes((10, 11))  # actual length above the maximum
    transport = FakeTransport(memory={(AREA_DB, 1): bytes(memory)})
    driver = fake_driver(transport)
    await driver.connect()
    results = by_id(
        await driver.read(
            [
                request("s", "DB1.60", "string", ValueType.TEXT, length=10),
                request("i", "DB1.20", "int"),
            ]
        )
    )
    assert results["s"].error_code == "s7_string_invalid"
    assert results["i"].quality is Quality.GOOD


async def test_results_keep_request_order_and_clock() -> None:
    transport = FakeTransport(memory={(AREA_DB, 1): bytes(DB1)})
    driver = fake_driver(transport)
    await driver.connect()
    tags = [
        request("c", "DB1.22", "uint"),
        request("x", "DB1.0", "nope"),
        request("a", "DB1.20", "int"),
    ]
    results = await driver.read(tags)
    assert [r.tag_id for r in results] == ["c", "x", "a"]
    assert all(r.received_at == NOW for r in results)
    assert results[0].source_timestamp == NOW


async def test_max_gap_option_controls_merging() -> None:
    tags = [request("a", "DB1.0", "int"), request("b", "DB1.10", "int")]
    merged = FakeTransport(memory={(AREA_DB, 1): bytes(20)})
    driver = fake_driver(merged)
    await driver.connect()
    await driver.read(tags)
    assert [i.length for i in merged.requests[0]] == [12]

    separate = FakeTransport(memory={(AREA_DB, 1): bytes(20)})
    driver = fake_driver(separate, options={"max_gap": 4})
    await driver.connect()
    await driver.read(tags)
    assert [i.length for i in separate.requests[0]] == [2, 2]


async def test_tag_timeout_is_used_for_its_request() -> None:
    transport = FakeTransport(memory={(AREA_DB, 1): bytes(10)})
    driver = fake_driver(transport, timeout_ms=1000)
    await driver.connect()
    tag = TagRequest(
        tag_id="a",
        address="DB1.0",
        value_type=ValueType.INTEGER,
        timeout_ms=250,
        protocol_options={"data_type": "int"},
    )
    await driver.read([tag, request("b", "DB1.2", "int")])
    assert transport.timeouts == [0.25]


@pytest.mark.parametrize(
    ("raised", "code", "message"),
    [
        (TimeoutError(), "s7_timeout", "S7 read timed out."),
        (ConnectionResetError(), "s7_connection_lost", "S7 connection lost."),
    ],
)
async def test_transport_errors_are_transient_and_repeated(
    raised: Exception, code: str, message: str
) -> None:
    transport = FakeTransport(memory={(AREA_DB, 1): bytes(10)}, fail=raised)
    driver = fake_driver(transport)
    tags = [request("a", "DB1.0", "int")]
    await driver.connect()
    with pytest.raises(TransientCommunicationError) as error:
        await driver.read(tags)
    assert (error.value.code, error.value.message) == (code, message)
    assert transport.closes == 1
    with pytest.raises(TransientCommunicationError) as again:
        await driver.read(tags)
    assert (again.value.code, again.value.message) == (code, message)
    assert len(transport.requests) == 1

    transport.fail = None
    await driver.connect()
    [result] = await driver.read(tags)
    assert result.quality is Quality.GOOD
    await driver.disconnect()
    with pytest.raises(TransientCommunicationError) as closed:
        await driver.read(tags)
    assert closed.value.code == "s7_not_connected"


async def test_malformed_response_is_connection_lost() -> None:
    class Garbage(FakeTransport):
        async def exchange(self, pdu: bytes, timeout_s: float) -> bytes:
            return b"\x32\x03"

    driver = fake_driver(Garbage())
    await driver.connect()
    with pytest.raises(TransientCommunicationError) as error:
        await driver.read([request("a", "DB1.0", "int")])
    assert error.value.code == "s7_connection_lost"


async def test_read_before_connect_is_not_connected() -> None:
    driver = fake_driver(FakeTransport())
    with pytest.raises(TransientCommunicationError) as error:
        await driver.read([request("a", "DB1.0", "int")])
    assert error.value.code == "s7_not_connected"


async def test_connect_failure_is_transient_and_closes() -> None:
    transport = FakeTransport(connect_error=ConnectionRefusedError())
    driver = fake_driver(transport)
    with pytest.raises(TransientCommunicationError) as error:
        await driver.connect()
    assert error.value.code == "s7_connect_failed"
    assert transport.closes == 1
    assert await driver.health_check() is False


async def test_cancellation_propagates() -> None:
    started = asyncio.Event()

    class Hanging(FakeTransport):
        async def exchange(self, pdu: bytes, timeout_s: float) -> bytes:
            started.set()
            await asyncio.sleep(30)
            return b""

    driver = fake_driver(Hanging(), timeout_ms=60000)
    await driver.connect()
    task = asyncio.create_task(driver.read([request("a", "DB1.0", "int")]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_capabilities_and_registry() -> None:
    transport = FakeTransport()
    driver = fake_driver(transport)
    assert driver.capabilities.protocol == "s7"
    assert await driver.health_check() is False
    await driver.connect()
    assert await driver.health_check() is True
    await driver.disconnect()
    assert transport.closes == 1
    assert await driver.health_check() is False
    created = default_driver_registry().create_driver(connection())
    assert isinstance(created, S7Driver)
    assert S7_PROTOCOL == "s7"


# -- worker -------------------------------------------------------------------


async def _no_sleep(_: float) -> None:
    return None


async def test_worker_keeps_polling_refused_reads_without_reconnecting() -> None:
    transport = FakeTransport(memory={(AREA_DB, 1): bytes(DB1)}, header_error=0x8104)
    registry = build_driver_registry(
        [("s7", lambda conn: S7Driver(conn, transport_factory=lambda: transport))]
    )
    worker = ConnectionWorker(
        connection(), registry, retry_sleep=_no_sleep, jitter=lambda: 0.5
    )
    group = TagGroupConfig(id="g", connection_id="press", interval_ms=1000)
    tags = (
        TagConfig(
            id="a",
            tag_group_id="g",
            name="A",
            address="DB1.20",
            value_type=ValueType.INTEGER,
            protocol_options={"data_type": "int"},
        ),
    )
    await worker.connect()
    try:
        for _ in range(3):
            poll = await worker.poll_group(group, tags)
            assert [r.error_code for r in poll.results] == ["s7_access_denied"]
        transport.header_error = 0
        poll = await worker.poll_group(group, tags)
    finally:
        await worker.disconnect()
    assert poll.results[0].value is not None and poll.results[0].value.raw == -1234
    assert transport.connects == 1


# -- CPU description failures --------------------------------------------------


async def test_failed_cpu_query_reopens_link_so_first_read_is_good() -> None:
    transport = FakeTransport(
        memory={(AREA_DB, 1): bytes(DB1)}, cpu_error=TimeoutError()
    )
    driver = fake_driver(transport)
    await driver.connect()
    assert transport.connects == 2
    assert transport.closes == 1
    assert driver.connection_details() == "PDU 480 bytes"
    [result] = await driver.read([request("a", "DB1.20", "int")])
    assert result.value is not None and result.value.raw == -1234
    await driver.connect()
    assert transport.cpu_requests == 1


async def test_failed_cpu_query_keeps_test_connection_ok() -> None:
    transport = FakeTransport(
        memory={(AREA_DB, 1): bytes(DB1)}, cpu_error=TimeoutError()
    )
    registry = build_driver_registry(
        [("s7", lambda conn: S7Driver(conn, transport_factory=lambda: transport))]
    )
    probe = TagConfig(
        id="speed",
        tag_group_id="g",
        name="Speed",
        address="DB1.20",
        value_type=ValueType.INTEGER,
        protocol_options={"data_type": "int"},
    )
    result = await probe_connection(connection(), registry, probe_tag=probe)
    assert result.ok, result.message
    assert result.message == (
        f"Connected in {result.elapsed_ms} ms (PDU 480 bytes). "
        "Read DB1.20 without errors."
    )


async def test_cpu_query_skipped_when_connect_used_the_timeout() -> None:
    transport = FakeTransport(connect_delay_s=0.25)
    driver = fake_driver(transport, timeout_ms=300)
    await driver.connect()
    assert transport.cpu_requests == 0
    assert driver.connection_details() == "PDU 480 bytes"
