"""Asynchronous Modbus TCP communication driver."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, cast

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    PermanentProtocolError,
    TagRequest,
    TagResult,
    TransientCommunicationError,
)
from plc_data_collector.protocols.driver import DriverCapabilities
from plc_data_collector.protocols.modbus_blocks import ReadBlock, plan_blocks
from plc_data_collector.protocols.modbus_tags import (
    MODBUS_PROTOCOL,
    Area,
    ModbusConnectionOptions,
    ModbusTagPlan,
    decode_bits,
    decode_registers,
    display_address,
    parse_endpoint,
    resolve_tag,
)

LOGGER = logging.getLogger(__name__)

# Caches live as long as the driver; a Tag edited many times must not grow them
# without bound, so they are dropped when they get this large.
_CACHE_LIMIT = 20_000

# The pymodbus client caps every request at its own timeout, so it gets the
# largest Connection timeout and the driver's asyncio.timeout decides instead.
_CLIENT_TIMEOUT_S = 600.0

# Exceptions that concern the whole request, not one register: function not
# supported, gateway path unavailable, gateway target did not answer.
_WHOLE_BLOCK_EXCEPTIONS = frozenset({0x01, 0x0A, 0x0B})

_ResolveKey = tuple[str, str, frozenset[tuple[str, object]]]


class ModbusResponse(Protocol):
    """Subset of a pymodbus response used by the driver."""

    registers: list[int]
    bits: list[bool]
    exception_code: int

    def isError(self) -> bool:
        """Return whether the device answered with a Modbus exception."""
        ...


class ModbusClient(Protocol):
    """Subset of the pymodbus async TCP client used by the driver."""

    @property
    def connected(self) -> bool:
        """Return whether the transport is connected."""
        ...

    async def connect(self) -> bool:
        """Open the connection; return whether it succeeded."""
        ...

    def close(self) -> None:
        """Close the connection."""
        ...

    async def read_coils(
        self, address: int, *, count: int, device_id: int
    ) -> ModbusResponse:
        """Read coils (FC01)."""
        ...

    async def read_discrete_inputs(
        self, address: int, *, count: int, device_id: int
    ) -> ModbusResponse:
        """Read discrete inputs (FC02)."""
        ...

    async def read_holding_registers(
        self, address: int, *, count: int, device_id: int
    ) -> ModbusResponse:
        """Read holding registers (FC03)."""
        ...

    async def read_input_registers(
        self, address: int, *, count: int, device_id: int
    ) -> ModbusResponse:
        """Read input registers (FC04)."""
        ...


ClientFactory = Callable[[str, int, float], ModbusClient]

_READERS = {
    Area.COILS: "read_coils",
    Area.DISCRETE_INPUTS: "read_discrete_inputs",
    Area.HOLDING_REGISTERS: "read_holding_registers",
    Area.INPUT_REGISTERS: "read_input_registers",
}


def _create_client(host: str, port: int, timeout_s: float) -> ModbusClient:
    # No internal retries or reconnects: the connection worker owns reconnecting.
    return cast(
        ModbusClient,
        AsyncModbusTcpClient(
            host, port=port, timeout=timeout_s, retries=0, reconnect_delay=0
        ),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ModbusDriver:
    """Modbus TCP driver: one client per connection, tags read in merged blocks."""

    def __init__(
        self,
        connection: ConnectionConfig,
        *,
        client_factory: ClientFactory = _create_client,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create a Modbus driver for one configured connection."""
        self._connection = connection
        self._options = ModbusConnectionOptions.from_options(
            connection.protocol_options
        )
        self._host, self._port = parse_endpoint(connection.endpoint)
        self._client_factory = client_factory
        self._clock = clock
        self._client: ModbusClient | None = None
        # The transient error that closed the client; repeated by read() until
        # connect() succeeds, so a retry reports the real cause.
        self._closed_by: TransientCommunicationError | None = None
        self._plans: dict[_ResolveKey, ModbusTagPlan | str] = {}
        self._blocks: dict[tuple[_ResolveKey, ...], list[ReadBlock]] = {}
        # Blocks whose request was refused for one register: read member by member.
        self._split: set[ReadBlock] = set()

    @property
    def capabilities(self) -> DriverCapabilities:
        """Return Modbus driver capabilities."""
        return DriverCapabilities(
            protocol=MODBUS_PROTOCOL,
            supports_batch_read=True,
            supports_health_check=True,
        )

    async def connect(self) -> None:
        """Open a new connection, closing any previous one first."""
        await self.disconnect()
        timeout_s = self._connection.timeout_ms / 1000
        client = self._client_factory(self._host, self._port, _CLIENT_TIMEOUT_S)
        try:
            async with asyncio.timeout(timeout_s):
                connected = await client.connect()
        except (TimeoutError, OSError, ModbusException) as error:
            self._close_quietly(client)
            raise self._connect_failed() from error
        except BaseException:
            self._close_quietly(client)
            raise
        if not connected:
            self._close_quietly(client)
            raise self._connect_failed()
        self._client = client
        self._closed_by = None

    async def disconnect(self) -> None:
        """Close the connection."""
        self._closed_by = None
        client, self._client = self._client, None
        if client is not None:
            self._close_quietly(client)

    async def health_check(self) -> bool:
        """Return whether the transport is still connected."""
        client = self._client
        return client is not None and bool(client.connected)

    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]:
        """Read every tag; one bad tag never makes the others bad.

        Raises ``TransientCommunicationError`` on timeouts and lost connections,
        discarding partial results so the worker can reconnect and retry.
        """
        client = self._client
        if client is None:
            if self._closed_by is not None:
                raise self._repeat(self._closed_by)
            raise TransientCommunicationError(
                "Modbus client is not connected.",
                code="modbus_not_connected",
                details={"connection_id": self._connection.id},
            )
        if not client.connected:
            # The transport closed under the client, for example a server reset.
            self._closed_by = self._connection_lost()
            self._close_quietly(client)
            raise self._repeat(self._closed_by)

        results: dict[str, TagResult] = {}
        keys: list[_ResolveKey] = []
        resolved: list[tuple[str, ModbusTagPlan]] = []
        timeouts: dict[str, float] = {}
        for tag in tags:
            key = self._key(tag)
            plan = self._resolve(key, tag)
            if isinstance(plan, str):
                results[tag.tag_id] = TagResult.failure(
                    tag_id=tag.tag_id,
                    error_code="modbus_invalid_tag",
                    error_message=plan,
                    received_at=self._now(),
                )
                continue
            keys.append(key)
            resolved.append((tag.tag_id, plan))
            timeouts[tag.tag_id] = (
                tag.timeout_ms or self._connection.timeout_ms
            ) / 1000

        for block in self._plan(tuple(keys), resolved):
            block_timeout = min(timeouts[tag_id] for tag_id, _ in block.members)
            try:
                results.update(await self._read_block(client, block, block_timeout))
            except TransientCommunicationError as error:
                # The transport state is unknown after a timeout or reset.
                self._closed_by = error
                self._close_quietly(client)
                raise
        return [results[tag.tag_id] for tag in tags]

    # -- planning ---------------------------------------------------------

    def _key(self, tag: TagRequest) -> _ResolveKey:
        try:
            return (tag.tag_id, tag.address, frozenset(tag.protocol_options.items()))
        except TypeError:  # unhashable option value
            return (tag.tag_id, tag.address, frozenset())

    def _resolve(self, key: _ResolveKey, tag: TagRequest) -> ModbusTagPlan | str:
        cached = self._plans.get(key)
        if cached is not None:
            return cached
        try:
            plan: ModbusTagPlan | str = resolve_tag(
                tag.address, tag.protocol_options, self._options
            )
        except ConfigurationError as error:
            plan = error.message
        if len(self._plans) >= _CACHE_LIMIT:
            self._plans.clear()
        self._plans[key] = plan
        return plan

    def _plan(
        self,
        keys: tuple[_ResolveKey, ...],
        resolved: list[tuple[str, ModbusTagPlan]],
    ) -> list[ReadBlock]:
        blocks = self._blocks.get(keys)
        if blocks is None:
            self._split.clear()
            blocks = plan_blocks(
                resolved,
                max_registers=self._options.max_registers_per_read,
                max_bits=self._options.max_bits_per_read,
                max_gap=self._options.max_gap,
            )
            if len(self._blocks) >= _CACHE_LIMIT:
                self._blocks.clear()
            self._blocks[keys] = blocks
        return blocks

    # -- reading ----------------------------------------------------------

    async def _read_block(
        self, client: ModbusClient, block: ReadBlock, timeout_s: float
    ) -> dict[str, TagResult]:
        if block in self._split:
            return await self._read_members(client, block, timeout_s)
        response = await self._request(
            client, block.unit_id, block.area, block.start, block.count, timeout_s
        )
        received_at = self._now()
        if not response.isError():
            return self._decode_block(block, response, received_at)

        code = _exception_code(response)
        if len(block.members) == 1 or code in _WHOLE_BLOCK_EXCEPTIONS:
            return self._refused(block, block.members, code, received_at)
        # One refused register must not hide the Tags that can be read, so read
        # every member on its own, now and on later polls.
        LOGGER.debug(
            "modbus_block_refused_reading_members",
            extra={
                "connection_id": self._connection.id,
                "unit_id": block.unit_id,
                "area": block.area.value,
                "start": block.start,
                "count": block.count,
                "exception_code": code,
            },
        )
        if len(self._split) >= _CACHE_LIMIT:
            self._split.clear()
        self._split.add(block)
        return await self._read_members(client, block, timeout_s)

    async def _read_members(
        self, client: ModbusClient, block: ReadBlock, timeout_s: float
    ) -> dict[str, TagResult]:
        results: dict[str, TagResult] = {}
        for member in block.members:
            plan = member[1]
            single = ReadBlock(
                block.unit_id, block.area, plan.address, plan.span, (member,)
            )
            response = await self._request(
                client,
                single.unit_id,
                single.area,
                single.start,
                single.count,
                timeout_s,
            )
            received_at = self._now()
            if response.isError():
                results.update(
                    self._refused(
                        single, single.members, _exception_code(response), received_at
                    )
                )
            else:
                results.update(self._decode_block(single, response, received_at))
        return results

    async def _request(
        self,
        client: ModbusClient,
        unit_id: int,
        area: Area,
        start: int,
        count: int,
        timeout_s: float,
    ) -> ModbusResponse:
        reader = getattr(client, _READERS[area])
        try:
            async with asyncio.timeout(timeout_s):
                response: ModbusResponse = await reader(
                    start, count=count, device_id=unit_id
                )
        except TimeoutError as error:
            raise TransientCommunicationError(
                "Modbus read timed out.",
                code="modbus_timeout",
                details={"connection_id": self._connection.id},
            ) from error
        except ModbusIOException as error:
            code = (
                "modbus_timeout"
                if "no response" in str(error).lower()
                else "modbus_connection_lost"
            )
            raise TransientCommunicationError(
                "Modbus read failed.",
                code=code,
                details={"connection_id": self._connection.id},
            ) from error
        except (ConnectionException, ModbusException, OSError, ValueError) as error:
            # Includes malformed responses: reconnecting resynchronises framing.
            raise self._connection_lost() from error
        return response

    def _decode_block(
        self, block: ReadBlock, response: ModbusResponse, received_at: datetime
    ) -> dict[str, TagResult]:
        values: list[int] | list[bool]
        if block.area.is_bit_area:
            values = list(response.bits)[: block.count]
        else:
            values = list(response.registers)[: block.count]
        results: dict[str, TagResult] = {}
        for tag_id, plan in block.members:
            offset = plan.address - block.start
            width = 1 if block.area.is_bit_area else plan.span
            if offset + width > len(values):
                results[tag_id] = TagResult.failure(
                    tag_id=tag_id,
                    error_code="modbus_short_response",
                    error_message="The device answered with fewer values than asked.",
                    received_at=received_at,
                )
                continue
            try:
                if block.area.is_bit_area:
                    value = decode_bits(
                        plan, cast(list[bool], values[offset : offset + 1])
                    )
                else:
                    value = decode_registers(
                        plan, cast(list[int], values[offset : offset + width])
                    )
            except PermanentProtocolError as error:
                results[tag_id] = TagResult.failure(
                    tag_id=tag_id,
                    error_code=error.code or "modbus_decode_failed",
                    error_message=error.message,
                    received_at=received_at,
                )
                continue
            results[tag_id] = TagResult.success(
                tag_id=tag_id,
                value=value,
                source_timestamp=received_at,
                received_at=received_at,
            )
        return results

    def _refused(
        self,
        block: ReadBlock,
        members: tuple[tuple[str, ModbusTagPlan], ...],
        code: int | None,
        received_at: datetime,
    ) -> dict[str, TagResult]:
        suffix = "unknown" if code is None else f"{code:02X}"
        results: dict[str, TagResult] = {}
        for tag_id, plan in members:
            results[tag_id] = TagResult.failure(
                tag_id=tag_id,
                error_code=f"modbus_exception_{suffix}",
                error_message=self._exception_message(code, block, plan),
                received_at=received_at,
            )
        return results

    def _exception_message(
        self, code: int | None, block: ReadBlock, plan: ModbusTagPlan
    ) -> str:
        if code == 2:
            shown = display_address(
                plan.area, plan.address, self._options.address_numbering
            )
            return f"The device has no register {shown}. Check the address."
        if code == 1:
            return f"The device does not support {block.area.label}."
        if code == 0x0B:
            return f"The gateway got no answer from unit {block.unit_id}."
        if code == 0x0A:
            return f"The gateway has no path to unit {block.unit_id}."
        if code is None:
            return "The device answered with an unknown Modbus exception."
        return f"The device answered with Modbus exception {code:02X}."

    # -- helpers ----------------------------------------------------------

    def _connection_lost(self) -> TransientCommunicationError:
        return TransientCommunicationError(
            "Modbus connection lost.",
            code="modbus_connection_lost",
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
            "Modbus connect failed.",
            code="modbus_connect_failed",
            details={"connection_id": self._connection.id},
        )

    def _close_quietly(self, client: ModbusClient) -> None:
        if client is self._client:
            self._client = None
        try:
            client.close()
        except Exception:
            LOGGER.debug(
                "modbus_client_close_failed",
                extra={"connection_id": self._connection.id},
            )

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)


def _exception_code(response: ModbusResponse) -> int | None:
    code = getattr(response, "exception_code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def create_modbus_driver(connection: ConnectionConfig) -> ModbusDriver:
    """Create a Modbus driver for registry registration."""
    return ModbusDriver(connection)
