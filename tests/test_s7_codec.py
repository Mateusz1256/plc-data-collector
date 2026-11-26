"""S7 Read Var codec tests.

Byte fixtures are copied from public sources, never produced by this project's
encoder. Each fixture names its source; the pcap frame numbers are the Wireshark
frame numbers of the capture file.
"""

from __future__ import annotations

import pytest
from modbus_support import free_port
from snap7.async_client import AsyncClient
from snap7.server import Server
from snap7.type import SrvArea

from plc_data_collector.protocols.s7_codec import (
    MAX_ITEM_PAYLOAD,
    RETURN_OK,
    ItemResult,
    ReadItem,
    S7HeaderError,
    build_read_request,
    parse_read_response,
    request_size,
    response_size,
)

AREA_I = 0x81
AREA_M = 0x83
AREA_DB = 0x84

# python-snap7 3.6.0, tests/test_client.py, TestClient.test_iso_exchange_buffer:
# "PDU read DB1 1.0 BYTE" and its "PDU response" (DB1.DBB0 = 0x11, pdu_ref 0x0100).
SNAP7_DB1_REQUEST = bytes.fromhex("320100000100000e00000401120a10020001000184000000")
SNAP7_DB1_RESPONSE = bytes.fromhex("3203000001000002000500000401ff04000811")

# Wireshark wiki SampleCaptures, s7comm_varservice_libnodavedemo.pcap
# (https://wiki.wireshark.org/SampleCaptures#s7comm---s7-communication):
# frame 11 reads 64 bytes from DB1.DBB0 (pdu_ref 0x0000).
LIBNODAVE_DB1_64_REQUEST = bytes.fromhex(
    "320100000000000e00000401120a10020040000184000000"
)
# Same capture, frames 13/14: 16 bytes from MB0 (pdu_ref 0x0001) and the response.
LIBNODAVE_M16_REQUEST = bytes.fromhex(
    "320100000001000e00000401120a10020010000083000000"
)
LIBNODAVE_M16_RESPONSE = bytes.fromhex(
    "3203000000010002001400000401ff040080a9100000000001010000000000000000"
)

# Wireshark wiki SampleCaptures, s7comm_varservice_libnodavedemo_bench.pcap,
# frames 4077/4078: five BYTE items from the I and M areas (pdu_ref 0x07d5).
LIBNODAVE_BENCH_REQUEST = bytes.fromhex(
    "3201000007d5003e00000405"
    "120a10020006000081000000"
    "120a10020006000083000000"
    "120a10020006000083000030"
    "120a10020036000083000020"
    "120a10020004000083000020"
)
LIBNODAVE_BENCH_RESPONSE = bytes.fromhex(
    "3203000007d50002006000000405"
    "ff040030000000000000"
    "ff0400300a1000020000"
    "ff040030010500000006"
    "ff0401b00000010500000006400ccccd"
    "000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000"
    "ff04002000000105"
)

# Apache PLC4X, protocols/s7/src/test/resources/protocols/s7/
# ParserSerializerTestsuite.xml, testcase "S7 Read Response" (S7 part after
# TPKT/COTP): four BIT items of one byte each, the first three followed by a
# fill byte (pdu_ref 0x000b).
PLC4X_PADDED_RESPONSE = bytes.fromhex(
    "32030000000b0002001700000404ff0300010100ff0300010100ff0300010100ff03000101"
)

# github.com/gymgit/s7-pcaps, wincc_s300_setup-alarm-read.pcapng, frame 20 (WinCC
# reading an S7-300, pdu_ref 0x0002): nine items mixing BYTE (0x04, length in
# bits), BIT (0x03, padded), REAL (0x07, length in bytes) and octet string (0x09,
# length in bytes) transport sizes.
WINCC_MIXED_RESPONSE = bytes.fromhex(
    "3203000000020002003c00000409"
    "ff04002000000000"
    "ff0300010100"
    "ff070004aabbccdd"
    "ff040020feadbeef"
    "ff040010babe"
    "ff0300010100"
    "ff0300010100"
    "ff0900022000"
    "ff0900020017"
)

# github.com/gymgit/s7-pcaps, snap7_s300_readVar.pcapng, frame 2: an S7-300
# answering a read of DB1 (which it does not have) with item return code 0x0A
# "object does not exist" and no data (pdu_ref 0x0000).
GYMGIT_DB_MISSING_RESPONSE = bytes.fromhex("32030000000000020004000004010a000000")

# NOT from a capture: no public capture with return code 0x05 was found. Built
# field by field from the Wireshark S7comm dissector (epan/dissectors/
# packet-s7comm.c, s7comm_decode_response_read_data): ACK_DATA header (12 bytes,
# error class/code 0), parameter = function 0x04 + item count 3; each data item
# = return code, transport size, length, data, plus a fill byte after an
# odd-length item that is not the last. Item 1: 0xFF, BYTE (0x04), 8 bits, 0x2a,
# fill byte. Item 2: 0x0A "object does not exist". Item 3: 0x05 "invalid
# address". Failed items carry transport size 0 and length 0, as the S7-300 in
# GYMGIT_DB_MISSING_RESPONSE sends (pdu_ref 0x0007).
FIELD_BUILT_ITEM_ERRORS_RESPONSE = bytes.fromhex(
    "3203000000070002000e00000403"  # header (data length 14), function 0x04, 3 items
    "ff0400082a00"  # item 1: OK, BYTE, 8 bits, 0x2a, fill byte
    "0a000000"  # item 2: 0x0A, no data
    "05000000"  # item 3: 0x05, no data
)

# python-snap7 3.6.0, tests/test_conformance.py,
# TestS7ResponseParsing.test_error_class_raises: an ACK_DATA header with error
# class 0x81, error code 0x04 ("function not available", what S7-1200/1500 send
# when PUT/GET is off), pdu_ref 0x0001, no parameters or data.
SNAP7_HEADER_ERROR_8104 = bytes.fromhex("320300000001000000008104")

# Apache PLC4X ParserSerializerTestsuite.xml, testcase "S7 Read Error Response":
# an ACK (type 0x02) with error class 0x85, code 0x00 (pdu_ref 0x000a).
PLC4X_HEADER_ERROR_8500 = bytes.fromhex("32020000000a000000008500")


def test_one_item_db_request_matches_snap7_fixture() -> None:
    request = build_read_request([ReadItem(AREA_DB, 1, 0, 1)], pdu_ref=0x0100)
    assert request == SNAP7_DB1_REQUEST


def test_one_item_requests_match_libnodave_capture() -> None:
    assert (
        build_read_request([ReadItem(AREA_DB, 1, 0, 64)], pdu_ref=0)
        == LIBNODAVE_DB1_64_REQUEST
    )
    assert (
        build_read_request([ReadItem(AREA_M, 0, 0, 16)], pdu_ref=1)
        == LIBNODAVE_M16_REQUEST
    )


def test_multi_item_request_matches_libnodave_capture() -> None:
    items = [
        ReadItem(AREA_I, 0, 0, 6),
        ReadItem(AREA_M, 0, 0, 6),
        ReadItem(AREA_M, 0, 6, 6),
        ReadItem(AREA_M, 0, 4, 54),
        ReadItem(AREA_M, 0, 4, 4),
    ]
    assert build_read_request(items, pdu_ref=0x07D5) == LIBNODAVE_BENCH_REQUEST


def test_two_item_db_and_m_request_concatenates_the_captured_items() -> None:
    # The DB item and the M item are each byte-identical to a captured one-item
    # request; a two-item request is the header, function 0x04, count 2, items.
    request = build_read_request(
        [ReadItem(AREA_DB, 1, 0, 64), ReadItem(AREA_M, 0, 0, 16)], pdu_ref=0
    )
    assert request[:12] == bytes.fromhex("320100000000001a00000402")
    assert request[12:24] == LIBNODAVE_DB1_64_REQUEST[12:]
    assert request[24:] == LIBNODAVE_M16_REQUEST[12:]


def test_one_item_response() -> None:
    results = parse_read_response(SNAP7_DB1_RESPONSE, pdu_ref=0x0100, item_count=1)
    assert results == [ItemResult(RETURN_OK, b"\x11")]
    results = parse_read_response(LIBNODAVE_M16_RESPONSE, pdu_ref=1, item_count=1)
    assert results == [ItemResult(RETURN_OK, LIBNODAVE_M16_RESPONSE[18:])]
    assert len(results[0].data) == 16


def test_multi_item_response_from_capture() -> None:
    results = parse_read_response(
        LIBNODAVE_BENCH_RESPONSE, pdu_ref=0x07D5, item_count=5
    )
    assert [r.return_code for r in results] == [RETURN_OK] * 5
    assert [len(r.data) for r in results] == [6, 6, 6, 54, 4]
    assert results[0].data == bytes(6)
    assert results[1].data == bytes.fromhex("0a1000020000")
    assert results[3].data[:12] == bytes.fromhex("0000010500000006400ccccd")
    assert results[4].data == bytes.fromhex("00000105")


def test_padded_items_are_skipped() -> None:
    results = parse_read_response(PLC4X_PADDED_RESPONSE, pdu_ref=0x0B, item_count=4)
    assert results == [ItemResult(RETURN_OK, b"\x01")] * 4


def test_transport_sizes_in_bits_and_bytes() -> None:
    results = parse_read_response(WINCC_MIXED_RESPONSE, pdu_ref=2, item_count=9)
    assert [r.data.hex() for r in results] == [
        "00000000",
        "01",
        "aabbccdd",
        "feadbeef",
        "babe",
        "01",
        "01",
        "2000",
        "0017",
    ]


def test_item_return_code_0a_from_capture() -> None:
    results = parse_read_response(GYMGIT_DB_MISSING_RESPONSE, pdu_ref=0, item_count=1)
    assert results == [ItemResult(0x0A, b"")]


def test_item_return_codes_0a_and_05_after_padded_item() -> None:
    results = parse_read_response(
        FIELD_BUILT_ITEM_ERRORS_RESPONSE, pdu_ref=7, item_count=3
    )
    assert results == [
        ItemResult(RETURN_OK, b"\x2a"),
        ItemResult(0x0A, b""),
        ItemResult(0x05, b""),
    ]


def test_header_error_81_04() -> None:
    with pytest.raises(S7HeaderError) as caught:
        parse_read_response(SNAP7_HEADER_ERROR_8104, pdu_ref=1, item_count=1)
    assert (caught.value.error_class, caught.value.error_code) == (0x81, 0x04)


def test_header_error_in_ack_pdu() -> None:
    with pytest.raises(S7HeaderError) as caught:
        parse_read_response(PLC4X_HEADER_ERROR_8500, pdu_ref=0x0A, item_count=4)
    assert (caught.value.error_class, caught.value.error_code) == (0x85, 0x00)


@pytest.mark.parametrize("cut", [1, 5, 17, len(LIBNODAVE_M16_RESPONSE) - 11])
def test_truncated_response_is_value_error(cut: int) -> None:
    with pytest.raises(ValueError):
        parse_read_response(LIBNODAVE_M16_RESPONSE[:-cut], pdu_ref=1, item_count=1)


def test_trailing_bytes_are_value_error() -> None:
    with pytest.raises(ValueError):
        parse_read_response(SNAP7_DB1_RESPONSE + b"\x00", pdu_ref=0x0100, item_count=1)


def test_wrong_pdu_ref_is_value_error() -> None:
    with pytest.raises(ValueError, match="pdu_ref"):
        parse_read_response(SNAP7_DB1_RESPONSE, pdu_ref=0x0101, item_count=1)
    with pytest.raises(ValueError, match="pdu_ref"):
        parse_read_response(SNAP7_HEADER_ERROR_8104, pdu_ref=2, item_count=1)


def test_wrong_item_count_is_value_error() -> None:
    with pytest.raises(ValueError, match="item"):
        parse_read_response(LIBNODAVE_BENCH_RESPONSE, pdu_ref=0x07D5, item_count=4)


def test_request_pdu_is_not_a_response() -> None:
    with pytest.raises(ValueError):
        parse_read_response(SNAP7_DB1_REQUEST, pdu_ref=0x0100, item_count=1)


@pytest.mark.parametrize(
    "items",
    [
        [],
        [ReadItem(AREA_DB, 1, 0, 0)],
        [ReadItem(AREA_DB, 65536, 0, 1)],
        [ReadItem(AREA_DB, 1, 65536, 1)],
        [ReadItem(AREA_DB, 1, -1, 1)],
        [ReadItem(256, 1, 0, 1)],
        [ReadItem(AREA_DB, 1, 0, 1)] * 256,
    ],
)
def test_invalid_request_is_value_error(items: list[ReadItem]) -> None:
    with pytest.raises(ValueError):
        build_read_request(items, pdu_ref=0)


def test_invalid_pdu_ref_is_value_error() -> None:
    with pytest.raises(ValueError):
        build_read_request([ReadItem(AREA_DB, 1, 0, 1)], pdu_ref=0x10000)


def test_sizes_match_fixtures() -> None:
    assert request_size(1) == len(SNAP7_DB1_REQUEST) == 24
    assert request_size(5) == len(LIBNODAVE_BENCH_REQUEST)
    assert response_size([1]) == len(SNAP7_DB1_RESPONSE)
    assert response_size([6, 6, 6, 54, 4]) == len(LIBNODAVE_BENCH_RESPONSE)
    assert response_size([1, 1, 1, 1]) == len(PLC4X_PADDED_RESPONSE)
    assert response_size([1, 0, 0]) == len(FIELD_BUILT_ITEM_ERRORS_RESPONSE)


def test_sizes_at_pdu_240() -> None:
    # ICS-pcap (github.com/automayt/ICS-pcap) S7/2-S7comm-VarService-CyclicData-1s
    # frame 18 is a 19-item read request of exactly 240 bytes (parameter length
    # 0x00e6), so 18 items leave room and 19 fill a 240-byte PDU.
    assert request_size(18) == 228
    assert request_size(19) == 240
    assert MAX_ITEM_PAYLOAD(240) == 222
    assert response_size([MAX_ITEM_PAYLOAD(240)]) == 240
    assert response_size([MAX_ITEM_PAYLOAD(240) + 1]) == 241
    assert response_size([4] * 18) == 14 + 18 * 8
    # Odd lengths are padded except after the last item.
    assert response_size([5] * 18) == 14 + 17 * 10 + 9


@pytest.mark.integration
async def test_codec_reads_from_snap7_server() -> None:
    db1 = bytearray(range(100))
    merkers = bytearray(range(0xA0, 0xB0))
    server = Server(log=False)
    server.register_area(SrvArea.DB, 1, db1)
    server.register_area(SrvArea.MK, 0, merkers)
    port = free_port()
    server.start(tcp_port=port)
    client = AsyncClient()
    try:
        await client.connect("127.0.0.1", 0, 1, port)
        connection = client.connection
        assert connection is not None
        items = [
            ReadItem(AREA_DB, 1, 3, 5),  # odd length: fill byte follows
            ReadItem(AREA_M, 0, 0, 16),
            ReadItem(AREA_DB, 2, 0, 4),  # DB2 is not registered
            ReadItem(AREA_DB, 1, 90, 10),
        ]
        await connection.send_data(build_read_request(items, pdu_ref=0x1234))
        pdu = await connection.receive_data()
        assert len(pdu) == response_size([5, 16, 0, 10])
        results = parse_read_response(pdu, pdu_ref=0x1234, item_count=len(items))
    finally:
        await client.disconnect()
        server.stop()
        server.destroy()
    assert results == [
        ItemResult(RETURN_OK, bytes(db1[3:8])),
        ItemResult(RETURN_OK, bytes(merkers)),
        ItemResult(0x0A, b""),
        ItemResult(RETURN_OK, bytes(db1[90:100])),
    ]
