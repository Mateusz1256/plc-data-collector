"""Asynchronous Siemens S7 communication driver (ISO-on-TCP, PUT/GET reads)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from snap7.async_client import AsyncClient
from snap7.error import S7Error, S7TimeoutError

from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    PermanentProtocolError,
    TagRequest,
    TagResult,
    TransientCommunicationError,
)
from plc_data_collector.protocols.driver import DriverCapabilities
from plc_data_collector.protocols.endpoints import parse_endpoint
from plc_data_collector.protocols.s7_blocks import S7Block, pack_requests, plan_blocks
from plc_data_collector.protocols.s7_codec import (
    MAX_ITEM_PAYLOAD,
    RETURN_OK,
    S7HeaderError,
    build_read_request,
    parse_read_response,
)
from plc_data_collector.protocols.s7_tags import (
    S7_PROTOCOL,
    Area,
    S7Address,
    S7ConnectionOptions,
    S7TagPlan,
    decode,
    resolve_tag,
)

LOGGER = logging.getLogger(__name__)

# Caches live as long as the driver; a Tag edited many times must not grow them
# without bound, so they are dropped when they get this large.
_CACHE_LIMIT = 20_000
# Smallest negotiated PDU the planner accepts (real CPUs offer 240 or more).
_MIN_PDU_LENGTH = 64
# Closing sends a disconnect request; a dead peer must not hold it up.
_CLOSE_TIMEOUT_S = 1.0
# The CPU query is skipped when less of the connect timeout is left: a query
# cut short leaves its answer in the socket and costs a reconnect.
_MIN_CPU_QUERY_S = 0.2
_MAX_PDU_REF = 0xFFFF
_MAX_CPU_TEXT = 64
# Marks the items of a request the PLC rejected as a whole (S7 header error).
_REJECTED = -1

PUT_GET_MESSAGE = (
    "The PLC refused the read. On S7-1200/1500: enable 'Permit access with "
    "PUT/GET communication from remote partner' in the CPU's Protection & "
    "Security settings, then download the hardware configuration."
)

_ResolveKey = tuple[str, str, frozenset[tuple[str, object]]]


class S7Transport(Protocol):
    """ISO-on-TCP link to one CPU, exchanging bare S7 PDUs.

    ``connect`` raises on any failure; ``exchange`` raises ``TimeoutError``
    when no answer comes within ``timeout_s`` and ``OSError`` (usually
    ``ConnectionError``) when the link is broken; ``cpu_description`` raises
    when the CPU did not answer cleanly, after which the link may still hold
    a late answer and must be reopened before the next ``exchange``.
    """

    async def connect(
        self, host: str, port: int, rack: int, slot: int, timeout_s: float
    ) -> int:
        """Open the link and return the negotiated PDU length."""
        ...

    async def exchange(self, pdu: bytes, timeout_s: float) -> bytes:
        """Send one request PDU and return the response PDU."""
        ...

    async def close(self) -> None:
        """Close the link; never raises."""
        ...

    async def cpu_description(self, timeout_s: float) -> str | None:
        """Return the CPU's order number (or module name); raise on failure."""
        ...


class Snap7Transport:
    """``S7Transport`` over python-snap7's ``AsyncClient``."""

    def __init__(self) -> None:
        """Create a closed transport."""
        self._client: AsyncClient | None = None

    async def connect(
        self, host: str, port: int, rack: int, slot: int, timeout_s: float
    ) -> int:
        """Connect within ``timeout_s`` (python-snap7's own limit is 5 s)."""
        await self.close()
        client = AsyncClient()
        self._client = client
        try:
            async with asyncio.timeout(timeout_s):
                await client.connect(host, rack, slot, port)
        except S7TimeoutError as error:
            raise TimeoutError(str(error)) from error
        except S7Error as error:
            raise ConnectionError(str(error)) from error
        connection = client.connection
        if connection is None:
            raise ConnectionError("python-snap7 has no connection after connect.")
        connection.timeout = timeout_s
        return int(client.pdu_length)

    async def exchange(self, pdu: bytes, timeout_s: float) -> bytes:
        """Send a request PDU and return the response, under the client's lock."""
        client = self._client
        connection = None if client is None else client.connection
        if client is None or connection is None:
            raise ConnectionError("Not connected.")
        connection.timeout = timeout_s
        try:
            async with asyncio.timeout(timeout_s):
                response = await client.iso_exchange_buffer(bytearray(pdu))
        except S7TimeoutError as error:
            raise TimeoutError(str(error)) from error
        except S7Error as error:
            raise ConnectionError(str(error)) from error
        return bytes(response)

    async def close(self) -> None:
        """Disconnect; errors and a peer that does not answer are ignored."""
        client, self._client = self._client, None
        if client is None:
            return
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_CLOSE_TIMEOUT_S):
                await client.disconnect()

    async def cpu_description(self, timeout_s: float) -> str | None:
        """Read the order number (SZL 0x0011), else the module type name.

        Raises on a timeout or error: a cut-short SZL request leaves its answer
        in the socket, which python-snap7 does not drain.
        """
        client = self._client
        connection = None if client is None else client.connection
        if client is None or connection is None:
            raise ConnectionError("Not connected.")
        connection.timeout = timeout_s
        async with asyncio.timeout(timeout_s):
            text = _printable((await client.get_order_code()).OrderCode)
            if not text:
                text = _printable((await client.get_cpu_info()).ModuleTypeName)
        return text or None


def _printable(value: object) -> str:
    raw = value.decode("latin-1") if isinstance(value, bytes) else str(value)
    text = "".join(char for char in raw if char.isprintable())
    return " ".join(text.split())[:_MAX_CPU_TEXT]


@dataclass(frozen=True, slots=True)
class _Piece:
    """What came back for one item of a block."""

    start: int
    length: int
    return_code: int
    data: bytes


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _singles(block: S7Block) -> list[S7Block]:
    return [
        S7Block(block.area, block.db, plan.byte, plan.span, ((tag_id, plan),))
        for tag_id, plan in block.members
    ]


def _refused_code(pieces: Sequence[_Piece]) -> int | None:
    """First item return code that refused data (not a whole-request rejection)."""
    for piece in pieces:
        if piece.return_code not in (RETURN_OK, _REJECTED):
            return piece.return_code
    return None


class S7Driver:
    """S7 driver: one client per connection, Tags read in packed block items."""

    def __init__(
        self,
        connection: ConnectionConfig,
        *,
        transport_factory: Callable[[], S7Transport] = Snap7Transport,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create an S7 driver for one configured connection."""
        self._connection = connection
        self._options = S7ConnectionOptions.from_options(connection.protocol_options)
        self._host, self._port = parse_endpoint(
            connection.endpoint, protocol_label="S7"
        )
        self._transport_factory = transport_factory
        self._clock = clock
        self._transport: S7Transport | None = None
        self._pdu_length = 0
        # The CPU description is asked once per driver: a CPU that answers
        # the SZL request badly must not disturb every reconnect.
        self._cpu: str | None = None
        self._cpu_asked = False
        # The transient error that closed the transport; repeated by read()
        # until connect() succeeds, so a retry reports the real cause.
        self._closed_by: TransientCommunicationError | None = None
        # Whether a whole-request rejection was logged since the last read the
        # PLC accepted.
        self._refusing = False
        self._pdu_ref = 0
        self._plans: dict[_ResolveKey, S7TagPlan | str] = {}
        self._blocks: dict[tuple[int, tuple[_ResolveKey, ...]], list[S7Block]] = {}
        # Blocks with a Tag the PLC refuses on its own: read member by member.
        self._split: set[S7Block] = set()

    @property
    def capabilities(self) -> DriverCapabilities:
        """Return S7 driver capabilities."""
        return DriverCapabilities(
            protocol=S7_PROTOCOL,
            supports_batch_read=True,
            supports_health_check=True,
        )

    async def connect(self) -> None:
        """Open a new connection, closing any previous one first."""
        await self.disconnect()
        timeout_s = self._connection.timeout_ms / 1000
        loop = asyncio.get_running_loop()
        started = loop.time()
        transport = self._transport_factory()
        pdu_length = await self._open(transport, timeout_s)
        if not self._cpu_asked:
            self._cpu_asked = True
            remaining = timeout_s - (loop.time() - started)
            if remaining >= _MIN_CPU_QUERY_S:
                try:
                    self._cpu = await transport.cpu_description(remaining)
                except Exception:
                    # A late answer may still arrive on this link, so reads
                    # start on a new one; the CPU is not asked again.
                    LOGGER.debug(
                        "s7_cpu_description_failed",
                        extra={"connection_id": self._connection.id},
                    )
                    await self._close_quietly(transport)
                    transport = self._transport_factory()
                    pdu_length = await self._open(transport, timeout_s)
                except BaseException:
                    await self._close_quietly(transport)
                    raise
        self._transport = transport
        self._pdu_length = pdu_length
        self._closed_by = None

    async def _open(self, transport: S7Transport, timeout_s: float) -> int:
        """Connect the transport; return the negotiated PDU length."""
        try:
            async with asyncio.timeout(timeout_s):
                pdu_length = await transport.connect(
                    self._host,
                    self._port,
                    self._options.rack,
                    self._options.slot,
                    timeout_s,
                )
        except Exception as error:
            await self._close_quietly(transport)
            raise self._connect_failed() from error
        except BaseException:
            await self._close_quietly(transport)
            raise
        if pdu_length < _MIN_PDU_LENGTH:
            await self._close_quietly(transport)
            raise self._connect_failed()
        return pdu_length

    async def disconnect(self) -> None:
        """Close the connection."""
        self._closed_by = None
        transport, self._transport = self._transport, None
        if transport is not None:
            await self._close_quietly(transport)

    async def health_check(self) -> bool:
        """Return whether the connection is open.

        A PLC that refuses reads (PUT/GET off) is still connected, so the
        worker keeps polling instead of reconnecting.
        """
        return self._transport is not None

    def connection_details(self) -> str | None:
        """Return the negotiated PDU length and CPU, while connected."""
        if self._transport is None:
            return None
        details = f"PDU {self._pdu_length} bytes"
        if self._cpu:
            details += f", CPU {self._cpu}"
        return details

    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]:
        """Read every tag; one bad tag never makes the others bad.

        Raises ``TransientCommunicationError`` on timeouts and lost connections,
        discarding partial results so the worker can reconnect and retry.
        """
        transport = self._transport
        if transport is None:
            if self._closed_by is not None:
                raise self._repeat(self._closed_by)
            raise TransientCommunicationError(
                "S7 client is not connected.",
                code="s7_not_connected",
                details={"connection_id": self._connection.id},
            )

        results: dict[str, TagResult] = {}
        keys: list[_ResolveKey] = []
        resolved: list[tuple[str, S7TagPlan]] = []
        timeouts: dict[str, float] = {}
        for tag in tags:
            key = self._key(tag)
            plan = self._resolve(key, tag)
            if isinstance(plan, str):
                results[tag.tag_id] = self._failure(
                    tag.tag_id, "s7_invalid_tag", plan, self._now()
                )
                continue
            keys.append(key)
            resolved.append((tag.tag_id, plan))
            timeouts[tag.tag_id] = (
                tag.timeout_ms or self._connection.timeout_ms
            ) / 1000

        if resolved:
            blocks = self._plan(tuple(keys), resolved)
            try:
                results.update(await self._read_blocks(transport, blocks, timeouts))
            except TransientCommunicationError as error:
                # The link state is unknown after a timeout or reset.
                self._closed_by = error
                await self._close_quietly(transport)
                raise
        return [results[tag.tag_id] for tag in tags]

    # -- planning ---------------------------------------------------------

    def _key(self, tag: TagRequest) -> _ResolveKey:
        try:
            return (tag.tag_id, tag.address, frozenset(tag.protocol_options.items()))
        except TypeError:  # unhashable option value
            return (tag.tag_id, tag.address, frozenset())

    def _resolve(self, key: _ResolveKey, tag: TagRequest) -> S7TagPlan | str:
        cached = self._plans.get(key)
        if cached is not None:
            return cached
        try:
            plan: S7TagPlan | str = resolve_tag(tag.address, tag.protocol_options)
        except ConfigurationError as error:
            plan = error.message
        if len(self._plans) >= _CACHE_LIMIT:
            self._plans.clear()
        self._plans[key] = plan
        return plan

    def _plan(
        self,
        keys: tuple[_ResolveKey, ...],
        resolved: list[tuple[str, S7TagPlan]],
    ) -> list[S7Block]:
        cache_key = (self._pdu_length, keys)
        blocks = self._blocks.get(cache_key)
        if blocks is None:
            self._split.clear()
            blocks = plan_blocks(
                resolved,
                max_gap=self._options.max_gap,
                max_block=MAX_ITEM_PAYLOAD(self._pdu_length),
            )
            if len(self._blocks) >= _CACHE_LIMIT:
                self._blocks.clear()
            self._blocks[cache_key] = blocks
        return blocks

    # -- reading ----------------------------------------------------------

    async def _read_blocks(
        self,
        transport: S7Transport,
        blocks: list[S7Block],
        timeouts: dict[str, float],
    ) -> dict[str, TagResult]:
        planned: list[S7Block] = []
        for block in blocks:
            planned.extend(_singles(block) if block in self._split else [block])
        outcomes = await self._exchange(transport, planned, timeouts)

        results: dict[str, TagResult] = {}
        refused_blocks: list[tuple[S7Block, int]] = []
        for block in planned:
            pieces = outcomes[block]
            code = _refused_code(pieces)
            if code is not None and len(block.members) > 1:
                refused_blocks.append((block, code))
            else:
                results.update(self._decode_block(block, pieces))
        if not refused_blocks:
            return results

        # One refused item must not hide the Tags that can be read, so read
        # every member on its own. Later polls read the block the same way
        # only when a member is refused on its own; otherwise the PLC may have
        # refused the merged range (for example a gap past the end of a DB).
        member_blocks = [
            (block, code, _singles(block)) for block, code in refused_blocks
        ]
        for block, code, _ in member_blocks:
            LOGGER.debug(
                "s7_block_refused_reading_members", extra=self._block_log(block, code)
            )
        singles = [single for _, _, members in member_blocks for single in members]
        reread = await self._exchange(transport, singles, timeouts)
        for block, code, members in member_blocks:
            member_refused = False
            for single in members:
                pieces = reread[single]
                member_refused = member_refused or _refused_code(pieces) is not None
                results.update(self._decode_block(single, pieces))
            if member_refused:
                if len(self._split) >= _CACHE_LIMIT:
                    self._split.clear()
                self._split.add(block)
                LOGGER.info(
                    "s7_block_split_remembered", extra=self._block_log(block, code)
                )
        return results

    async def _exchange(
        self,
        transport: S7Transport,
        blocks: list[S7Block],
        timeouts: dict[str, float],
    ) -> dict[S7Block, list[_Piece]]:
        """Read the blocks in as few requests as the PDU allows."""
        # Equal blocks (the same Tag requested twice) are read once.
        outcomes: dict[S7Block, list[_Piece]] = {block: [] for block in blocks}
        for request in pack_requests(list(outcomes), pdu_length=self._pdu_length):
            items = [item for _, block_items in request for item in block_items]
            timeout_s = min(
                timeouts[tag_id] for block, _ in request for tag_id, _ in block.members
            )
            pdu_ref = self._next_pdu_ref()
            pdu = build_read_request(items, pdu_ref)
            try:
                async with asyncio.timeout(timeout_s):
                    response = await transport.exchange(pdu, timeout_s)
                answers = parse_read_response(
                    response, pdu_ref=pdu_ref, item_count=len(items)
                )
            except S7HeaderError as error:
                # The link is fine; the CPU's configuration refuses the read.
                self._log_refused(error)
                for block, block_items in request:
                    outcomes[block].extend(
                        _Piece(item.start, item.length, _REJECTED, b"")
                        for item in block_items
                    )
                continue
            except TimeoutError as error:
                raise TransientCommunicationError(
                    "S7 read timed out.",
                    code="s7_timeout",
                    details={"connection_id": self._connection.id},
                ) from error
            except (OSError, ValueError) as error:
                # Includes malformed responses: reconnecting resynchronises.
                raise self._connection_lost() from error
            self._refusing = False
            answer = iter(answers)
            for block, block_items in request:
                for item in block_items:
                    result = next(answer)
                    outcomes[block].append(
                        _Piece(item.start, item.length, result.return_code, result.data)
                    )
        return outcomes

    def _decode_block(
        self, block: S7Block, pieces: list[_Piece]
    ) -> dict[str, TagResult]:
        received_at = self._now()
        results: dict[str, TagResult] = {}
        for tag_id, plan in block.members:
            low, high = plan.byte, plan.byte + plan.span
            touching = sorted(
                (p for p in pieces if p.start < high and low < p.start + p.length),
                key=lambda piece: piece.start,
            )
            failure = self._piece_failure(plan, touching)
            if failure is not None:
                error_code, message = failure
                results[tag_id] = self._failure(
                    tag_id, error_code, message, received_at
                )
                continue
            data = b"".join(piece.data for piece in touching)
            offset = low - touching[0].start
            try:
                value = decode(plan, data[offset : offset + plan.span])
            except PermanentProtocolError as error:
                results[tag_id] = self._failure(
                    tag_id, error.code or "s7_invalid_tag", error.message, received_at
                )
                continue
            results[tag_id] = TagResult.success(
                tag_id=tag_id,
                value=value,
                source_timestamp=received_at,
                received_at=received_at,
            )
        return results

    def _piece_failure(
        self, plan: S7TagPlan, touching: list[_Piece]
    ) -> tuple[str, str] | None:
        if any(piece.return_code == _REJECTED for piece in touching):
            return "s7_access_denied", PUT_GET_MESSAGE
        code = _refused_code(touching)
        if code is not None:
            return self._item_error(code, plan)
        if any(len(piece.data) != piece.length for piece in touching):
            return (
                f"s7_item_error_{RETURN_OK:02X}",
                f"The PLC answered {self._address(plan)} with fewer bytes than asked.",
            )
        return None

    def _item_error(self, code: int, plan: S7TagPlan) -> tuple[str, str]:
        address = self._address(plan)
        if code in (0x0A, 0x05):
            if plan.area is Area.DB:
                return (
                    "s7_db_not_found" if code == 0x0A else "s7_address_out_of_range",
                    f"The PLC has no DB{plan.db} of this size. Check the DB number "
                    "and offset; on S7-1200/1500 also switch off 'Optimized block "
                    "access' in the DB's properties and download it.",
                )
            return (
                "s7_address_out_of_range",
                f"The PLC has no {address} of this size. Check the address.",
            )
        if code == 0x03:
            return (
                "s7_access_denied",
                f"The PLC denied access to {address}. On S7-1200/1500 check the "
                "CPU's protection level and 'Permit access with PUT/GET "
                "communication from remote partner'.",
            )
        if code == 0x06:
            return (
                "s7_type_not_supported",
                f"The PLC does not support reading {address} (return code 06).",
            )
        return (
            f"s7_item_error_{code:02X}",
            f"The PLC refused {address} with return code {code:02X}.",
        )

    # -- helpers ----------------------------------------------------------

    def _address(self, plan: S7TagPlan) -> str:
        return str(S7Address(plan.area, plan.db, plan.byte, plan.bit))

    def _failure(
        self, tag_id: str, code: str, message: str, received_at: datetime
    ) -> TagResult:
        return TagResult.failure(
            tag_id=tag_id,
            error_code=code,
            error_message=message,
            received_at=received_at,
        )

    def _log_refused(self, error: S7HeaderError) -> None:
        if self._refusing:
            return
        self._refusing = True
        LOGGER.warning(
            "s7_reads_refused",
            extra={
                "connection_id": self._connection.id,
                "error_class": error.error_class,
                "error_code": error.error_code,
            },
        )

    def _block_log(self, block: S7Block, code: int) -> dict[str, object]:
        return {
            "connection_id": self._connection.id,
            "area": block.area.value,
            "db": block.db,
            "start": block.start,
            "length": block.length,
            "return_code": code,
        }

    def _next_pdu_ref(self) -> int:
        self._pdu_ref = self._pdu_ref % _MAX_PDU_REF + 1
        return self._pdu_ref

    def _connection_lost(self) -> TransientCommunicationError:
        return TransientCommunicationError(
            "S7 connection lost.",
            code="s7_connection_lost",
            details={"connection_id": self._connection.id},
        )

    def _repeat(
        self, error: TransientCommunicationError
    ) -> TransientCommunicationError:
        return TransientCommunicationError(
            error.message, code=error.code, details=dict(error.details)
        )

    def _connect_failed(self) -> TransientCommunicationError:
        return TransientCommunicationError(
            "S7 connect failed.",
            code="s7_connect_failed",
            details={"connection_id": self._connection.id},
        )

    async def _close_quietly(self, transport: S7Transport) -> None:
        if transport is self._transport:
            self._transport = None
        try:
            await transport.close()
        except Exception:
            LOGGER.debug(
                "s7_transport_close_failed",
                extra={"connection_id": self._connection.id},
            )

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)


def create_s7_driver(connection: ConnectionConfig) -> S7Driver:
    """Create an S7 driver for registry registration."""
    return S7Driver(connection)
