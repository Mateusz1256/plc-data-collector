"""The wired M-Bus: transports and the one shared bus per Connection.

``MBusBus`` is the only way to talk to the meters of a Connection: polling,
Test connection, Read meter and Scan bus each take a **turn**
(``async with bus.turn() as turn``), one at a time. A turn is one complete
exchange that leaves the bus clean.

A reply is never attributed to the wrong request:

* every request starts with the input flushed, after waiting out what is left
  of the previous request's reply window if that one did not end cleanly
  (garbled, cut short by a cancellation or an error);
* a garbled reply, or a well-formed one that is not the answer expected (an
  ``E5`` where a telegram is due, a telegram from another primary address, a
  follow-up telegram from another meter), is treated as garbled: the rest of
  its reply window is waited out and discarded before the one retry;
* frames are assembled by length, so a reply split across reads (a TCP
  gateway's segments) is one frame, and bytes left over at the end of the
  window are a garbled reply, never the start of the next answer.

Link layer (EN 13757-2): SND_NKE to a meter before its first read after the
bus opens and after a failed read, then REQ_UD2 with the frame count bit
(FCB) alternating; a retry repeats the FCB so the meter repeats its telegram.
A meter read by secondary address is deselected (SND_NKE to 253), selected,
read through 253 and deselected again. Identity checks against configured
IDs are the driver's: ``MeterReading.header`` carries the meter's ID.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import serial  # type: ignore[import-untyped]

from plc_data_collector.domain import CollectorError, TransientCommunicationError
from plc_data_collector.protocols.mbus_codec import (
    ADDRESS_SECONDARY,
    Ack,
    Frame,
    FrameReader,
    GarbledFrame,
    LongFrame,
    MBusRecord,
    MeterHeader,
    Telegram,
    parse_rsp_ud,
    req_ud2,
    select_secondary,
    snd_nke,
)
from plc_data_collector.protocols.mbus_tags import MeterAddress, SecondaryAddress

LOGGER = logging.getLogger(__name__)

ProbeResult = Literal["none", "ack", "collision"]
TransportErrorCode = Literal[
    "mbus_timeout", "mbus_connection_lost", "mbus_connect_failed", "mbus_not_connected"
]

_TRANSPORT_MESSAGES: dict[str, str] = {
    "mbus_timeout": "The M-Bus transport did not respond in time.",
    "mbus_connection_lost": "The M-Bus connection was lost.",
    "mbus_connect_failed": "Could not open the M-Bus connection.",
    "mbus_not_connected": "The M-Bus connection is not open.",
}
_ATTEMPTS = 2  # one retry
# pyserial's read timeout: how often the reading thread checks its deadline.
_SERIAL_POLL_S = 0.05
_SERIAL_WRITE_TIMEOUT_S = 5.0
_SERIAL_CHUNK = 512


class BusTransportError(TransientCommunicationError):
    """The transport failed; the bus closes it and the worker reconnects."""

    def __init__(self, code: TransportErrorCode, message: str | None = None) -> None:
        """Create the error for ``code`` with a default message."""
        super().__init__(message or _TRANSPORT_MESSAGES[code], code=code)


class MeterError(CollectorError):
    """One meter did not answer cleanly; the bus stays open."""


class MeterNoReply(MeterError):
    """No reply, after one retry."""

    def __init__(self, message: str) -> None:
        """Create the error with the code ``mbus_no_reply``."""
        super().__init__(message, code="mbus_no_reply")


class MeterGarbled(MeterError):
    """A garbled or unexpected reply, after one retry."""

    def __init__(self, message: str) -> None:
        """Create the error with the code ``mbus_garbled_reply``."""
        super().__init__(message, code="mbus_garbled_reply")


@dataclass(frozen=True, slots=True)
class MeterReading:
    """One meter's telegrams: the first header and every record, in order."""

    header: MeterHeader
    records: tuple[MBusRecord, ...]


# Transports


class MBusTransport(Protocol):
    """A byte link to the bus (a TCP gateway or a serial port).

    Every failure of the link raises ``BusTransportError``; ``close`` never
    raises and may be called at any time, also after a failed ``open``.
    """

    async def open(self) -> None:
        """Open the link."""
        ...

    async def write(self, data: bytes) -> None:
        """Send one request frame."""
        ...

    async def read_frame(self, timeout_s: float) -> Frame | None:
        """The next frame within ``timeout_s``; ``None`` on silence.

        Raises ``GarbledFrame`` for bad bytes or a frame still incomplete
        at the timeout.
        """
        ...

    async def flush_input(self) -> None:
        """Discard everything received and not yet read."""
        ...

    async def close(self) -> None:
        """Close the link and release its resources."""
        ...

    def describe(self) -> str:
        """``"TCP gateway"`` or ``"Serial <baud> baud"``; never the endpoint."""
        ...


class _FramedTransport:
    """Frame assembly shared by the transports, over a ``_receive`` primitive."""

    def __init__(self) -> None:
        self._frames = FrameReader()

    async def _receive(self, timeout_s: float) -> bytes:
        """Bytes received within ``timeout_s``; ``b""`` on silence."""
        raise NotImplementedError

    async def read_frame(self, timeout_s: float) -> Frame | None:
        """Assemble the next frame by length within ``timeout_s``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            frame = self._frames.next_frame()
            if frame is not None:
                return frame
            remaining = deadline - loop.time()
            data = await self._receive(remaining) if remaining > 0 else b""
            if data:
                self._frames.feed(data)
                continue
            if self._frames.buffered:
                self._frames = FrameReader()
                raise GarbledFrame("the reply was incomplete at the timeout")
            return None

    def _reset_frames(self) -> None:
        self._frames = FrameReader()


class _TcpStream(asyncio.Protocol):
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.arrived = asyncio.Event()
        self.lost = False

    def data_received(self, data: bytes) -> None:
        self.buffer += data
        self.arrived.set()

    def eof_received(self) -> bool:
        self.lost = True
        self.arrived.set()
        return False

    def connection_lost(self, exc: Exception | None) -> None:
        self.lost = True
        self.arrived.set()


class TcpTransport(_FramedTransport):
    """A transparent M-Bus/TCP gateway (asyncio)."""

    def __init__(self, host: str, port: int, connect_timeout_s: float) -> None:
        """Create a closed transport; nothing is connected until ``open``."""
        super().__init__()
        self._host = host
        self._port = port
        self._connect_timeout_s = connect_timeout_s
        self._transport: asyncio.Transport | None = None
        self._stream: _TcpStream | None = None

    def describe(self) -> str:
        """The transport kind, never the endpoint."""
        return "TCP gateway"

    async def open(self) -> None:
        """Connect within the Connection timeout."""
        await self.close()
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(self._connect_timeout_s):
                transport, stream = await loop.create_connection(
                    _TcpStream, self._host, self._port
                )
        except TimeoutError as error:
            raise BusTransportError(
                "mbus_timeout",
                "The M-Bus gateway did not accept the connection in time.",
            ) from error
        except OSError as error:
            raise BusTransportError(
                "mbus_connect_failed",
                f"Could not connect to the M-Bus gateway: {error}",
            ) from error
        self._transport = transport
        self._stream = stream
        self._reset_frames()

    def _open_stream(self) -> tuple[asyncio.Transport, _TcpStream]:
        transport, stream = self._transport, self._stream
        if transport is None or stream is None:
            raise BusTransportError("mbus_not_connected")
        if stream.lost or transport.is_closing():
            raise BusTransportError("mbus_connection_lost")
        return transport, stream

    async def write(self, data: bytes) -> None:
        """Send one request frame."""
        transport, _ = self._open_stream()
        try:
            transport.write(data)
        except OSError as error:
            raise BusTransportError("mbus_connection_lost") from error

    async def _receive(self, timeout_s: float) -> bytes:
        stream = self._stream
        if stream is None:
            raise BusTransportError("mbus_not_connected")
        if not stream.buffer and not stream.lost:
            stream.arrived.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(timeout_s):
                    await stream.arrived.wait()
        if stream.buffer:
            data = bytes(stream.buffer)
            stream.buffer.clear()
            return data
        if stream.lost:
            raise BusTransportError("mbus_connection_lost")
        return b""

    async def flush_input(self) -> None:
        """Discard what has arrived, including bytes still in the socket."""
        _, stream = self._open_stream()
        # Two loop iterations: the selector reports the socket readable in
        # the first, the data is delivered in the second.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        stream.buffer.clear()
        self._reset_frames()
        self._open_stream()

    async def close(self) -> None:
        """Close the socket."""
        transport, self._transport, self._stream = self._transport, None, None
        if transport is not None:
            transport.close()


class SerialTransport(_FramedTransport):
    """A serial port (USB level converter), 8E1, via pyserial.

    pyserial blocks, so every call runs in a single-thread executor the
    transport owns; ``close`` shuts it down, so no thread outlives the port.
    """

    def __init__(self, device: str, baud_rate: int) -> None:
        """Create a closed transport; nothing is opened until ``open``."""
        super().__init__()
        self._device = device
        self._baud_rate = baud_rate
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._port: Any = None
        self._stop = threading.Event()

    def describe(self) -> str:
        """The transport kind and speed, never the device."""
        return f"Serial {self._baud_rate} baud"

    async def open(self) -> None:
        """Open the port, 8 data bits, even parity, 1 stop bit."""
        await self.close()
        self._stop = threading.Event()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mbus-serial"
        )
        try:
            self._port = await self._call(self._open_port)
        except BusTransportError as error:
            await self.close()
            raise BusTransportError(
                "mbus_connect_failed", f"Could not open the serial port: {error}"
            ) from error
        self._reset_frames()

    def _open_port(self) -> Any:
        return serial.Serial(
            self._device,
            self._baud_rate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=_SERIAL_POLL_S,
            write_timeout=_SERIAL_WRITE_TIMEOUT_S,
        )

    async def _call[T](self, function: Callable[[], T]) -> T:
        """Run ``function`` on the port's thread; map pyserial failures."""
        executor = self._executor
        if executor is None:
            raise BusTransportError("mbus_not_connected")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(executor, function)
        except serial.SerialTimeoutException as error:
            raise BusTransportError("mbus_timeout") from error
        except (serial.SerialException, OSError, ValueError) as error:
            raise BusTransportError("mbus_connection_lost", str(error)) from error

    def _open_port_or_raise(self) -> Any:
        port = self._port
        if port is None:
            raise BusTransportError("mbus_not_connected")
        return port

    async def write(self, data: bytes) -> None:
        """Send one request frame."""
        port = self._open_port_or_raise()
        await self._call(lambda: port.write(data))

    async def _receive(self, timeout_s: float) -> bytes:
        port = self._open_port_or_raise()
        deadline = time.monotonic() + timeout_s
        stop = self._stop

        def read_some() -> bytes:
            while not stop.is_set():
                data = bytes(port.read(max(1, min(port.in_waiting, _SERIAL_CHUNK))))
                if data:
                    return data
                if time.monotonic() >= deadline:
                    break
            return b""

        return await self._call(read_some)

    async def flush_input(self) -> None:
        """Discard what the port has received."""
        port = self._open_port_or_raise()
        await self._call(port.reset_input_buffer)
        self._reset_frames()

    async def close(self) -> None:
        """Close the port and shut the thread down (waiting for it)."""
        executor, self._executor = self._executor, None
        port, self._port = self._port, None
        if executor is None:
            return
        self._stop.set()  # a read in progress returns within _SERIAL_POLL_S
        try:
            if port is not None:
                with contextlib.suppress(Exception):
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(executor, port.close)
        finally:
            # Also when cancelled: the queued close still runs before shutdown.
            executor.shutdown(wait=True)


# The bus


def _ack(frame: Frame) -> Ack:
    if not isinstance(frame, Ack):
        raise GarbledFrame("expected E5")
    return frame


def _same_meter(first: MeterHeader, other: MeterHeader) -> bool:
    return (first.id_digits, first.manufacturer_code, first.version, first.medium) == (
        other.id_digits,
        other.manufacturer_code,
        other.version,
        other.medium,
    )


def _rsp_ud(
    primary: int | None, first: MeterHeader | None
) -> Callable[[Frame], Telegram]:
    """Accept an RSP_UD from ``primary`` (if given) and from ``first``'s meter."""

    def accept(frame: Frame) -> Telegram:
        if not isinstance(frame, LongFrame):
            raise GarbledFrame("expected an RSP_UD telegram")
        if primary is not None and frame.address != primary:
            raise GarbledFrame(f"reply from primary address {frame.address}")
        telegram = parse_rsp_ud(frame)
        if first is not None and not _same_meter(first, telegram.header):
            raise GarbledFrame("follow-up telegram from another meter")
        return telegram

    return accept


def _repeated(header: MeterHeader, previous: tuple[int, ...]) -> bool:
    """Whether ``header`` repeats a telegram of the meter's previous read."""
    counts = len(previous) == 1 or len(set(previous)) == len(previous)
    return counts and header.access_no in previous


@dataclass(frozen=True, slots=True)
class _Read:
    reading: MeterReading
    complete: bool


class MBusBus:
    """The bus of one M-Bus Connection, shared by polling and Panel actions.

    ``open``, ``close`` and every ``turn`` take the same lock: do not open or
    close the bus from inside a turn.
    """

    def __init__(
        self,
        transport_factory: Callable[[], MBusTransport],
        *,
        timeout_s: float,
        max_telegrams: int,
    ) -> None:
        """Create a closed bus; ``transport_factory`` makes a fresh transport."""
        self._factory = transport_factory
        self.timeout_s = timeout_s
        self.max_telegrams = max_telegrams
        self._lock = asyncio.Lock()
        self._transport: MBusTransport | None = None
        # Link state per meter identity (ID digits), whatever the address form
        # a read used: the FCB of the meter's next REQ_UD2 (absent: unknown,
        # so SND_NKE first, or FCB 1 for a selected meter), the access
        # numbers of its last read's telegrams (to spot a repeated telegram),
        # and the ID last read at each primary address.
        self._fcb: dict[str, bool] = {}
        self._access: dict[str, tuple[int, ...]] = {}
        self._ids: dict[int, str] = {}
        # Primary addresses just reset by a probe's SND_NKE (ID not known).
        self._reset_primaries: set[int] = set()
        # A meter may still be selected (state unknown, or a turn ended early).
        self._maybe_selected = True
        # Loop time until which a reply to the last request may still arrive.
        self._quiet_at = 0.0

    @property
    def is_open(self) -> bool:
        """Whether the transport is open."""
        return self._transport is not None

    async def open(self) -> None:
        """Open the transport (no-op when open); raises ``BusTransportError``."""
        async with self._lock:
            if self._transport is not None:
                return
            transport = self._factory()
            try:
                await transport.open()
            except BaseException:
                await transport.close()
                raise
            self._transport = transport
            self._fcb.clear()
            self._reset_primaries.clear()
            self._maybe_selected = True
            self._quiet_at = 0.0

    async def close(self) -> None:
        """Close the transport (after the current turn)."""
        async with self._lock:
            await self._drop()

    async def _drop(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()

    @contextlib.asynccontextmanager
    async def turn(self) -> AsyncIterator[BusTurn]:
        """Wait for the bus, flush it, and yield it for one exchange.

        A transport error closes the bus (``is_open`` turns false) and is
        re-raised; meter errors leave it open.
        """
        async with self._lock:
            transport = self._transport
            if transport is None:
                raise BusTransportError("mbus_not_connected")
            turn = BusTurn(self, transport)
            try:
                await self._settle(transport)
                yield turn
                if turn.selected:
                    await turn.deselect()
            except BusTransportError as error:
                LOGGER.debug("mbus_transport_error", extra={"code": error.code})
                await self._drop()
                raise
            finally:
                if turn.selected:
                    self._maybe_selected = True

    async def _settle(self, transport: MBusTransport) -> None:
        """Wait out a pending reply window, then discard the input."""
        delay = self._quiet_at - asyncio.get_running_loop().time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._quiet_at = 0.0
        await transport.flush_input()


class BusTurn:
    """The bus for the duration of one turn; see ``MBusBus.turn``."""

    def __init__(self, bus: MBusBus, transport: MBusTransport) -> None:
        """Bind to the bus holding the lock."""
        self._bus = bus
        self._transport = transport
        self.selected = False
        self._select_timeout_s = bus.timeout_s

    # Requests

    async def _request[T](
        self, request: bytes, timeout_s: float, accept: Callable[[Frame], T]
    ) -> T | None:
        """Send ``request`` and take its reply: ``None`` on silence.

        A garbled or unexpected reply raises ``GarbledFrame`` once the rest
        of its reply window has passed and the input has been discarded.
        """
        bus = self._bus
        await bus._settle(self._transport)
        await self._transport.write(request)
        bus._quiet_at = asyncio.get_running_loop().time() + timeout_s
        try:
            frame = await self._transport.read_frame(timeout_s)
            result = None if frame is None else accept(frame)
        except GarbledFrame:
            await bus._settle(self._transport)
            raise
        bus._quiet_at = 0.0
        return result

    async def _retried[T](
        self, request: bytes, timeout_s: float, accept: Callable[[Frame], T], what: str
    ) -> T:
        garbled: str | None = None
        for attempt in range(_ATTEMPTS):
            try:
                result = await self._request(request, timeout_s, accept)
            except GarbledFrame as error:
                garbled = str(error)
                LOGGER.debug(
                    "mbus_garbled_reply",
                    extra={"request": what, "attempt": attempt + 1, "reason": garbled},
                )
                continue
            if result is not None:
                return result
            garbled = None
            LOGGER.debug(
                "mbus_no_reply", extra={"request": what, "attempt": attempt + 1}
            )
        if garbled is not None:
            raise MeterGarbled(f"Garbled reply to {what} ({garbled}).")
        raise MeterNoReply(f"No reply to {what}.")

    # Meter reads

    async def read_meter(self, address: MeterAddress) -> MeterReading:
        """Read every telegram (up to ``max_telegrams``) of one meter.

        Raises ``MeterNoReply`` or ``MeterGarbled`` after one retry.
        """
        if address.primary is not None:
            return await self._read_primary(address.primary)
        assert address.secondary is not None
        return await self._read_secondary(address.secondary)

    def _take_fcb(self, meter_id: str | None) -> bool | None:
        """The meter's next FCB, forgotten until the read succeeds."""
        return None if meter_id is None else self._bus._fcb.pop(meter_id, None)

    async def _read_primary(self, primary: int) -> MeterReading:
        bus = self._bus
        # Popped: a failed read leaves the meter to be initialised again.
        fcb = self._take_fcb(bus._ids.get(primary))
        if primary in bus._reset_primaries:
            bus._reset_primaries.discard(primary)
            fcb = True
        if fcb is None:
            await self._retried(
                snd_nke(primary), bus.timeout_s, _ack, f"SND_NKE to {primary}"
            )
            fcb = True
        result = await self._telegrams(primary, fcb)
        bus._ids[primary] = result.reading.header.id_digits
        return result.reading

    async def _read_secondary(self, address: SecondaryAddress) -> MeterReading:
        bus = self._bus
        if bus._maybe_selected:
            await self.deselect()
        self.selected = True
        self._select_timeout_s = bus.timeout_s
        try:
            await self._retried(
                select_secondary(address.pattern()),
                bus.timeout_s,
                _ack,
                f"the selection of {address.id_digits}",
            )
            # A full ID shares the FCB state of the meter's primary reads.
            known = None if "F" in address.id_digits else address.id_digits
            fcb = self._take_fcb(known)
            result = await self._telegrams(None, True if fcb is None else fcb)
        except MeterError:
            await self.deselect()
            raise
        await self.deselect()
        return result.reading

    async def _telegrams(self, primary: int | None, fcb: bool) -> _Read:
        """REQ_UD2 and follow-ups; on success the meter's state is remembered.

        A first telegram whose access number is one of the meter's previous
        read is a repeat (the meter saw this FCB last time): the FCB is
        toggled and the telegram requested once more. Not for a
        multi-telegram meter whose telegrams shared one access number (it
        does not count them): the extra request would skip its telegram 1.
        """
        bus = self._bus
        address = ADDRESS_SECONDARY if primary is None else primary
        first: MeterHeader | None = None
        records: list[MBusRecord] = []
        access: list[int] = []
        complete = False
        for number in range(bus.max_telegrams):
            what = f"REQ_UD2 {number + 1} to {address}"
            telegram = await self._retried(
                req_ud2(address, fcb), bus.timeout_s, _rsp_ud(primary, first), what
            )
            fcb = not fcb
            if first is None:
                header = telegram.header
                if _repeated(header, bus._access.get(header.id_digits, ())):
                    LOGGER.debug(
                        "mbus_repeated_telegram",
                        extra={"address": address, "access_no": header.access_no},
                    )
                    telegram = await self._retried(
                        req_ud2(address, fcb),
                        bus.timeout_s,
                        _rsp_ud(primary, header),
                        what,
                    )
                    fcb = not fcb
                first = telegram.header
            access.append(telegram.header.access_no)
            records.extend(telegram.records)
            if not telegram.more_follows:
                complete = True
                break
        assert first is not None
        meter_id = first.id_digits
        bus._access[meter_id] = tuple(access)
        # Stopped at max_telegrams with more to follow: the meter would go on
        # with its next telegram, so the next read starts it over.
        if complete:
            bus._fcb[meter_id] = fcb
        else:
            bus._fcb.pop(meter_id, None)
        return _Read(MeterReading(first, tuple(records)), complete)

    async def deselect(self) -> None:
        """SND_NKE to 253; a garbled reply or none is harmless."""
        try:
            reply = await self._request(
                snd_nke(ADDRESS_SECONDARY), self._select_timeout_s, _ack
            )
        except GarbledFrame as error:
            LOGGER.debug("mbus_deselect_garbled", extra={"reason": str(error)})
        else:
            if reply is None:
                LOGGER.debug("mbus_deselect_no_reply")
        self.selected = False
        self._bus._maybe_selected = False

    # Scan probes

    async def probe_primary(self, address: int, timeout_s: float) -> ProbeResult:
        """SND_NKE to ``address``: ``"ack"``, ``"none"`` or ``"collision"``."""
        bus = self._bus
        meter_id = bus._ids.get(address)
        self._take_fcb(meter_id)
        bus._reset_primaries.discard(address)
        try:
            reply = await self._request(snd_nke(address), timeout_s, _ack)
        except GarbledFrame:
            return "collision"
        if reply is None:
            return "none"
        # SND_NKE reset the meter: its next REQ_UD2 may use FCB 1.
        if meter_id is None:
            bus._reset_primaries.add(address)
        else:
            bus._fcb[meter_id] = True
        return "ack"

    async def probe_secondary(self, pattern: bytes, timeout_s: float) -> ProbeResult:
        """Select by ``pattern`` (8 bytes): ``"ack"``, ``"none"``, ``"collision"``.

        After ``"ack"`` or ``"collision"`` the turn ends with a deselect.
        """
        self.selected = True
        self._select_timeout_s = timeout_s
        try:
            reply = await self._request(select_secondary(pattern), timeout_s, _ack)
        except GarbledFrame:
            return "collision"
        if reply is None:
            # Nobody matched; a selection deselects every meter it does not match.
            self.selected = False
            self._bus._maybe_selected = False
            return "none"
        return "ack"

    async def read_selected_header(self, timeout_s: float) -> MeterHeader | None:
        """The selected meter's header (REQ_UD2 to 253); ``None`` on silence.

        Raises ``MeterGarbled`` after one retry.
        """
        try:
            telegram = await self._retried(
                req_ud2(ADDRESS_SECONDARY, True),
                timeout_s,
                _rsp_ud(None, None),
                "REQ_UD2 to the selected meter",
            )
        except MeterNoReply:
            return None
        return telegram.header
