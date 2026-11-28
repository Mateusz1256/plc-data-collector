"""Scan bus: find the meters of one M-Bus Connection, taking turns with polling.

One scan job per Connection at a time, held in memory (lost on restart). A
job runs as an asyncio task on the bus ``borrow_bus`` lends (the running
worker's, or a temporary one) and takes **one turn per probe**, so polls go
on between probes: slower during a scan, but without gaps.

* Primary scan: SND_NKE to each address 0-250, waiting the scan reply
  timeout for ``E5``; a meter that answers is read for its header (ID,
  manufacturer, medium) in the same turn. A garbled reply means two or more
  meters share the address.
* Secondary scan: wildcard search, one ID digit at a time: select
  ``0FFFFFFF`` ... ``9FFFFFFF``; no reply → skip; a clean ``E5`` → read the
  selected meter's header and deselect it; a garbled reply → the same digits
  with each next digit fixed, down to the full 8-digit ID.

``ScanJob.cancel`` stops a scan after the current probe; ``cancel_all`` (an
apply) stops every scan at once and closes the temporary buses.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from plc_data_collector.app.mbus_access import (
    BusAccessRefused,
    BusHost,
    TemporaryBuses,
    borrow_bus,
    check_access,
)
from plc_data_collector.domain import CollectorError, ConnectionConfig
from plc_data_collector.protocols.mbus_bus import BusTurn, MBusBus, MeterError
from plc_data_collector.protocols.mbus_codec import MeterHeader
from plc_data_collector.protocols.mbus_tags import (
    MAX_PRIMARY_ADDRESS,
    MeterAddress,
    SecondaryAddress,
)

LOGGER = logging.getLogger(__name__)

ScanKind = Literal["primary", "secondary"]
PRIMARY_ADDRESSES = MAX_PRIMARY_ADDRESS + 1
CANCELLED_MESSAGE = "Scan cancelled."
ALREADY_RUNNING_MESSAGE = "A scan is already running on this Connection."
_ID_LENGTH = 8
_DIGITS = "0123456789"


@dataclass(frozen=True, slots=True)
class ScanProgress:
    """Where a scan is.

    ``done`` counts probes; ``total`` is 251 for a primary scan and None for
    a secondary one (its length depends on the meters). ``message`` says why
    a finished scan stopped early (cancelled, the bus failed); None while it
    runs and when it ran to the end.
    """

    done: int
    total: int | None
    found: int
    current: str
    finished: bool
    message: str | None


@dataclass(frozen=True, slots=True)
class FoundMeter:
    """A meter a scan found; ``primary`` is None for a secondary scan."""

    primary: int | None
    header: MeterHeader


class ScanAlreadyRunning(BusAccessRefused):
    """The Connection already has a scan running."""

    def __init__(self) -> None:
        """Create the refusal with the code ``mbus_scan_running``."""
        super().__init__(ALREADY_RUNNING_MESSAGE, code="mbus_scan_running")


class _Cancelled(Exception):
    """``ScanJob.cancel`` was called; raised between probes."""


class ScanJob:
    """One scan of one Connection, running as an asyncio task."""

    def __init__(
        self, host: BusHost, connection: ConnectionConfig, kind: ScanKind
    ) -> None:
        """Create the job; ``ScanRegistry.start`` starts it."""
        self.connection = connection
        self.kind: ScanKind = kind
        self._host = host
        self._done = 0
        self._current = ""
        self._finished = False
        self._message: str | None = None
        self._meters: list[FoundMeter] = []
        self._notes: list[str] = []
        self._cancel_requested = False
        self._stop_message: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def progress(self) -> ScanProgress:
        """A snapshot of the scan's progress."""
        return ScanProgress(
            done=self._done,
            total=PRIMARY_ADDRESSES if self.kind == "primary" else None,
            found=len(self._meters),
            current=self._current,
            finished=self._finished,
            message=self._message,
        )

    @property
    def meters(self) -> tuple[FoundMeter, ...]:
        """The meters found so far, in the order found."""
        return tuple(self._meters)

    @property
    def notes(self) -> tuple[str, ...]:
        """What the scan could not resolve (shared addresses, silent meters)."""
        return tuple(self._notes)

    def cancel(self) -> None:
        """Stop the scan after the current probe."""
        self._cancel_requested = True

    async def wait(self) -> None:
        """Wait until the scan has finished (without cancelling it)."""
        if self._task is not None:
            await asyncio.wait([self._task])

    def _start(self) -> None:
        self._task = asyncio.create_task(
            self._run(), name=f"mbus-scan-{self.connection.id}"
        )

    async def _stop(self, message: str) -> None:
        """Stop the scan now, mid-probe, and finish with ``message``."""
        task = self._task
        if task is None or task.done():
            return
        self._stop_message = message
        task.cancel()
        await asyncio.wait([task])
        if not self._finished:  # cancelled before its first step
            self._finished = True
            self._message = message

    async def _run(self) -> None:
        LOGGER.info(
            "mbus_scan_started",
            extra={"connection_id": self.connection.id, "kind": self.kind},
        )
        try:
            async with borrow_bus(self._host, self.connection) as bus:
                if self.kind == "primary":
                    await self._primary(bus)
                else:
                    await self._secondary(bus, "")
        except _Cancelled:
            self._message = CANCELLED_MESSAGE
        except asyncio.CancelledError:
            if self._stop_message is None:
                self._message = CANCELLED_MESSAGE
                raise
            self._message = self._stop_message
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
        except BusAccessRefused as error:
            self._message = error.message
        except CollectorError as error:
            self._message = f"Scan stopped: {error.message} ({error.code})"
        except Exception as error:
            LOGGER.exception(
                "mbus_scan_failed",
                extra={
                    "connection_id": self.connection.id,
                    "error_type": type(error).__name__,
                },
            )
            self._message = (
                f"Scan stopped by an unexpected error ({type(error).__name__}). "
                "Check the collector log."
            )
        finally:
            self._finished = True
            self._current = ""
            LOGGER.info(
                "mbus_scan_finished",
                extra={
                    "connection_id": self.connection.id,
                    "kind": self.kind,
                    "probes": self._done,
                    "found": len(self._meters),
                    "stopped": self._message,
                },
            )

    def _next_probe(self, current: str) -> None:
        if self._cancel_requested:
            raise _Cancelled
        self._current = current

    # Primary scan

    async def _primary(self, bus: MBusBus) -> None:
        for address in range(PRIMARY_ADDRESSES):
            self._next_probe(f"primary address {address}")
            async with bus.turn() as turn:
                result = await turn.probe_primary(address, bus.scan_timeout_s)
                if result == "ack":
                    await self._read_primary(turn, address)
            if result == "collision":
                self._notes.append(
                    f"Two or more meters share primary address {address}; use "
                    "secondary addresses."
                )
            self._done += 1

    async def _read_primary(self, turn: BusTurn, address: int) -> None:
        try:
            reading = await turn.read_meter(
                MeterAddress(primary=address, secondary=None)
            )
        except MeterError as error:
            self._notes.append(
                f"The meter at primary address {address} answered, but did not "
                f"send its data ({error.code})."
            )
            return
        self._meters.append(FoundMeter(primary=address, header=reading.header))

    # Secondary scan

    async def _secondary(self, bus: MBusBus, prefix: str) -> None:
        for digit in _DIGITS:
            digits = prefix + digit
            id_pattern = digits.ljust(_ID_LENGTH, "F")
            self._next_probe(f"secondary address {id_pattern}")
            pattern = SecondaryAddress(id_pattern, None, None, None).pattern()
            async with bus.turn() as turn:
                result = await turn.probe_secondary(pattern, bus.scan_timeout_s)
                if result == "ack":
                    # The telegram takes as long as a poll's, not a probe's.
                    await self._read_selected(turn, id_pattern, bus.timeout_s)
                    await turn.deselect()
            self._done += 1
            if result != "collision":
                continue
            if len(digits) < _ID_LENGTH:
                await self._secondary(bus, digits)
            else:
                self._notes.append(
                    f"Two or more meters share the ID {digits}; give their Tags "
                    "the full secondary address (ID, manufacturer, version and "
                    "medium)."
                )

    async def _read_selected(
        self, turn: BusTurn, id_pattern: str, timeout_s: float
    ) -> None:
        try:
            header = await turn.read_selected_header(timeout_s)
        except MeterError as error:
            header = None
            reason = error.code
        else:
            reason = "mbus_no_reply"
        if header is None:
            self._notes.append(
                f"A meter answered the selection {id_pattern}, but did not send "
                f"its data ({reason})."
            )
            return
        identity = (header.id_digits, header.manufacturer_code)
        if any(
            (m.header.id_digits, m.header.manufacturer_code) == identity
            for m in self._meters
        ):
            return
        self._meters.append(FoundMeter(primary=None, header=header))


class ScanRegistry:
    """The process's scan jobs (one per Connection) and temporary buses."""

    def __init__(self, host: BusHost) -> None:
        """Create an empty registry for ``host``."""
        self._host = host
        self._jobs: dict[str, ScanJob] = {}
        self.temporary_buses = TemporaryBuses()

    def start(self, connection: ConnectionConfig, kind: ScanKind) -> ScanJob:
        """Start a scan of ``connection``'s bus; call it on the event loop.

        Raises ``ScanAlreadyRunning`` when the Connection has a scan running,
        and refuses as ``check_access`` does (apply in progress, other
        settings). Errors opening the bus end the job with a message.
        """
        current = self._jobs.get(connection.id)
        if current is not None and not current.progress.finished:
            raise ScanAlreadyRunning
        check_access(self._host, connection)
        job = ScanJob(self._host, connection, kind)
        self._jobs[connection.id] = job
        job._start()
        return job

    def get(self, connection_id: str) -> ScanJob | None:
        """The Connection's running or last scan."""
        return self._jobs.get(connection_id)

    async def cancel_all(self, message: str) -> None:
        """Stop every running scan with ``message``; close the temporary buses."""
        running = [job for job in self._jobs.values() if not job.progress.finished]
        await asyncio.gather(*(job._stop(message) for job in running))
        await self.temporary_buses.close_all()
