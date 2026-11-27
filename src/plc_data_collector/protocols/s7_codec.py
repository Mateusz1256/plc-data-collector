"""S7 "Read Var" (function 0x04) request and response codec (no I/O).

Builds and parses the S7 PDU only: S7 header plus parameters (and data, for the
response). TPKT and COTP framing is added and removed by python-snap7's ISO
connection (``AsyncClient.connection.send_data`` / ``receive_data``).

Layout, from the public S7comm descriptions (Wireshark ``packet-s7comm.c``):

- Job header (10 bytes): protocol id 0x32, PDU type 0x01, reserved (2),
  pdu_ref (2), parameter length (2), data length (2).
- Ack-data header (12 bytes): as above with PDU type 0x03 (0x02 for a bare
  ACK), followed by error class (1) and error code (1).
- Request parameters: function 0x04, item count, then per item 12 bytes:
  0x12 (variable specification), 0x0A (length of the rest), 0x10 (any-type
  addressing), transport size, count (2), DB number (2), area (1), bit address
  (3 = byte * 8 + bit).
- Response parameters: function 0x04, item count. Data: per item a 4-byte
  header (return code, transport size, length) and the data; the length is in
  bits for transport sizes 0x03/0x04/0x05 and in bytes otherwise; an odd-length
  item is followed by a fill byte unless it is the last.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass

RETURN_OK = 0xFF

_PROTOCOL_ID = 0x32
_PDU_JOB = 0x01
_PDU_ACK = 0x02
_PDU_ACK_DATA = 0x03
_FUNCTION_READ_VAR = 0x04
_JOB_HEADER = struct.Struct(">BBHHHH")
_ACK_HEADER = struct.Struct(">BBHHHHBB")
_ITEM_SPEC = struct.Struct(">BBBBHHBBH")  # area + 3-byte address as B + H
_ITEM_HEADER = struct.Struct(">BBH")
_SPEC_VARIABLE = 0x12
_SPEC_LENGTH = 0x0A
_SYNTAX_ANY = 0x10
_TRANSPORT_BYTE = 0x02
# Response transport sizes whose length field counts bits (BIT, BYTE, INT).
_LENGTH_IN_BITS = frozenset({0x03, 0x04, 0x05})
# Return codes whose data the PLC sends (Wireshark: reserved 0x00 and success).
_CODES_WITH_DATA = frozenset({0x00, RETURN_OK})
_RESPONSE_PARAMS = 2
_REQUEST_ITEM_SIZE = 12
_RESPONSE_ITEM_HEADER = 4
_MAX_ITEMS = 255
_MAX_U16 = 0xFFFF


@dataclass(frozen=True)
class ReadItem:
    """One Read Var item, byte addressing, transport size BYTE."""

    area_code: int
    db: int
    start: int
    length: int


@dataclass(frozen=True)
class ItemResult:
    """Return code and payload of one response item."""

    return_code: int
    data: bytes


class S7HeaderError(Exception):
    """The PLC rejected the whole request (non-zero header error class/code)."""

    def __init__(self, error_class: int, error_code: int) -> None:
        super().__init__(
            f"S7 header error class 0x{error_class:02X}, code 0x{error_code:02X}"
        )
        self.error_class = error_class
        self.error_code = error_code


def MAX_ITEM_PAYLOAD(pdu_length: int) -> int:
    """Largest data one single-item response can carry for a negotiated PDU."""
    return pdu_length - _ACK_HEADER.size - _RESPONSE_PARAMS - _RESPONSE_ITEM_HEADER


def request_size(item_count: int) -> int:
    """Size in bytes of a Read Var request PDU with ``item_count`` items."""
    return _JOB_HEADER.size + 2 + _REQUEST_ITEM_SIZE * item_count


def response_size(lengths: Sequence[int]) -> int:
    """Size in bytes of the response PDU for items of the given data lengths."""
    size = _ACK_HEADER.size + _RESPONSE_PARAMS
    last = len(lengths) - 1
    for index, length in enumerate(lengths):
        size += _RESPONSE_ITEM_HEADER + length
        if length % 2 and index < last:
            size += 1
    return size


def build_read_request(items: Sequence[ReadItem], pdu_ref: int) -> bytes:
    """Build the S7 Read Var request PDU (without TPKT/COTP)."""
    if not 1 <= len(items) <= _MAX_ITEMS:
        raise ValueError(f"A read request holds 1-{_MAX_ITEMS} items.")
    _check_range("pdu_ref", pdu_ref, 0, _MAX_U16)
    parameters = bytearray((_FUNCTION_READ_VAR, len(items)))
    for item in items:
        _check_range("area_code", item.area_code, 0, 0xFF)
        _check_range("db", item.db, 0, _MAX_U16)
        _check_range("start", item.start, 0, _MAX_U16)
        _check_range("length", item.length, 1, _MAX_U16)
        bit_address = item.start * 8
        parameters += _ITEM_SPEC.pack(
            _SPEC_VARIABLE,
            _SPEC_LENGTH,
            _SYNTAX_ANY,
            _TRANSPORT_BYTE,
            item.length,
            item.db,
            item.area_code,
            bit_address >> 16,
            bit_address & _MAX_U16,
        )
    header = _JOB_HEADER.pack(_PROTOCOL_ID, _PDU_JOB, 0, pdu_ref, len(parameters), 0)
    return header + bytes(parameters)


def parse_read_response(
    pdu: bytes, *, pdu_ref: int, item_count: int
) -> list[ItemResult]:
    """Parse a Read Var response PDU (without TPKT/COTP) into per-item results.

    Raises ``S7HeaderError`` when the PLC rejected the whole request and
    ``ValueError`` on malformed data, a wrong ``pdu_ref`` or item count.
    """
    if len(pdu) < _ACK_HEADER.size:
        raise ValueError(f"S7 response too short ({len(pdu)} bytes).")
    (
        protocol_id,
        pdu_type,
        _reserved,
        response_ref,
        param_length,
        data_length,
        error_class,
        error_code,
    ) = _ACK_HEADER.unpack_from(pdu)
    if protocol_id != _PROTOCOL_ID:
        raise ValueError(f"Not an S7 PDU (protocol id 0x{protocol_id:02X}).")
    if pdu_type not in (_PDU_ACK, _PDU_ACK_DATA):
        raise ValueError(f"Not an S7 response (PDU type 0x{pdu_type:02X}).")
    if response_ref != pdu_ref:
        raise ValueError(f"S7 response pdu_ref {response_ref} != request {pdu_ref}.")
    if error_class or error_code:
        raise S7HeaderError(error_class, error_code)
    if pdu_type != _PDU_ACK_DATA:
        raise ValueError("S7 read response is not an ACK_DATA PDU.")
    if _ACK_HEADER.size + param_length + data_length != len(pdu):
        raise ValueError(
            f"S7 response length {len(pdu)} does not match its header "
            f"(parameters {param_length}, data {data_length})."
        )
    if param_length != _RESPONSE_PARAMS:
        raise ValueError(f"Unexpected read response parameter length {param_length}.")
    function, response_items = pdu[_ACK_HEADER.size], pdu[_ACK_HEADER.size + 1]
    if function != _FUNCTION_READ_VAR:
        raise ValueError(f"Not a Read Var response (function 0x{function:02X}).")
    if response_items != item_count:
        raise ValueError(
            f"S7 response has {response_items} items, expected {item_count}."
        )
    return _parse_items(pdu, _ACK_HEADER.size + _RESPONSE_PARAMS, item_count)


def _parse_items(pdu: bytes, offset: int, item_count: int) -> list[ItemResult]:
    results: list[ItemResult] = []
    for index in range(item_count):
        if offset + _RESPONSE_ITEM_HEADER > len(pdu):
            raise ValueError(f"S7 response truncated at item {index}.")
        return_code, transport_size, length = _ITEM_HEADER.unpack_from(pdu, offset)
        offset += _RESPONSE_ITEM_HEADER
        if return_code not in _CODES_WITH_DATA:
            results.append(ItemResult(return_code, b""))
            continue
        size = (length + 7) // 8 if transport_size in _LENGTH_IN_BITS else length
        if offset + size > len(pdu):
            raise ValueError(f"S7 response truncated in item {index} data.")
        results.append(ItemResult(return_code, bytes(pdu[offset : offset + size])))
        offset += size
        if size % 2 and index < item_count - 1:
            offset += 1
    if offset != len(pdu):
        raise ValueError(f"S7 response has {len(pdu) - offset} unexpected bytes.")
    return results


def _check_range(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} {value} is outside {low}-{high}.")
