"""Bus access for M-Bus Panel actions: the running worker's bus, or a temporary one.

A Connection has one bus (a serial port cannot be opened twice, and many
gateways take one client), so Test connection, Read meter and Scan bus never
open a second link while the Connection runs: they borrow the worker's bus
and take turns with polling. When the Connection does not run, the action
opens a **temporary** bus for its duration; actions on one stopped
Connection share it, so they take turns too. An apply closes every temporary
bus first (``ScanRegistry.cancel_all``), so the new worker can open the port.

A running bus is lent only for the same endpoint and, on a serial port, the
same baud rate; other option differences (timeouts, telegram limit) do not
count, and the running bus keeps its own values until the next apply.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from plc_data_collector.domain import (
    CollectorError,
    ConfigurationError,
    ConnectionConfig,
)
from plc_data_collector.protocols.mbus import MBusDriver
from plc_data_collector.protocols.mbus_bus import MBusBus
from plc_data_collector.protocols.mbus_tags import (
    MBUS_PROTOCOL,
    MBusConnectionOptions,
    endpoint_key,
)

if TYPE_CHECKING:
    from plc_data_collector.app.config import CollectorConfig
    from plc_data_collector.app.mbus_scan import ScanRegistry
    from plc_data_collector.protocols import DriverRegistry

SETTINGS_DIFFER_MESSAGE = (
    "This Connection is running with other settings. Apply your changes first, "
    "or disable the Connection."
)
TEMPORARY_DIFFERS_MESSAGE = (
    "Another action is using this Connection with other settings. Try again "
    "when it has finished."
)
APPLYING_MESSAGE = "Changes are being applied. Try again when they are."
PORT_IN_USE_MESSAGE = (
    "This port is in use by Connection '{connection_id}'. Use that Connection, "
    "or disable it first."
)


class BusAccessRefused(CollectorError):
    """A Panel action cannot have the bus now; the message is for the Panel."""


class BusSettingsDiffer(BusAccessRefused):
    """The bus in use has another endpoint or baud rate than the action's."""

    def __init__(self, message: str = SETTINGS_DIFFER_MESSAGE) -> None:
        """Create the refusal with the code ``mbus_bus_settings_differ``."""
        super().__init__(message, code="mbus_bus_settings_differ")


class BusInUse(BusAccessRefused):
    """Another Connection holds the endpoint (its worker or a temporary bus)."""

    def __init__(self, connection_id: str) -> None:
        """Create the refusal with the code ``bus_in_use``."""
        super().__init__(
            PORT_IN_USE_MESSAGE.format(connection_id=connection_id), code="bus_in_use"
        )


class BusHost(Protocol):
    """What bus access needs from the collector host (``CollectorHost`` has it)."""

    @property
    def applying(self) -> bool:
        """Whether an apply is in progress."""
        ...

    @property
    def running_config(self) -> CollectorConfig | None:
        """The configuration the runtime runs, or None."""
        ...

    @property
    def registry(self) -> DriverRegistry:
        """The driver registry; temporary buses are built like a worker's."""
        ...

    @property
    def mbus_scans(self) -> ScanRegistry:
        """The process's scan jobs and temporary buses."""
        ...

    def mbus_bus(self, connection_id: str) -> MBusBus | None:
        """The bus of the running worker for ``connection_id``, if one runs."""
        ...


def bus_settings(connection: ConnectionConfig) -> tuple[object, ...]:
    """What makes two Connection versions the same bus.

    The normalised endpoint, plus the baud rate on a serial port. Raises
    ``ConfigurationError`` for an invalid endpoint or options.
    """
    key = endpoint_key(connection.endpoint)
    if key[0] != "serial":
        return key
    options = MBusConnectionOptions.from_options(connection.protocol_options)
    return (*key, options.baud_rate)


def check_access(host: BusHost, connection: ConnectionConfig) -> MBusBus | None:
    """The running bus to lend, or None when a temporary bus is due.

    Raises ``BusAccessRefused`` while an apply runs, ``BusSettingsDiffer``
    when the bus in use has other settings, ``BusInUse`` when a temporary bus
    is due but another Connection holds the endpoint (a running worker or a
    temporary bus), and ``ConfigurationError`` for invalid settings.
    """
    if host.applying:
        raise BusAccessRefused(APPLYING_MESSAGE, code="apply_in_progress")
    settings = bus_settings(connection)
    bus = host.mbus_bus(connection.id)
    if bus is None:
        endpoint = endpoint_key(connection.endpoint)
        _check_running_holders(host, connection.id, endpoint)
        host.mbus_scans.temporary_buses.check(connection.id, settings, endpoint)
        return None
    running = _running_connection(host, connection.id)
    if running is None or bus_settings(running) != settings:
        raise BusSettingsDiffer
    return bus


@contextlib.asynccontextmanager
async def borrow_bus(
    host: BusHost, connection: ConnectionConfig
) -> AsyncIterator[MBusBus]:
    """The bus for one Panel action on ``connection``.

    The running worker's bus when the settings are the same (the worker keeps
    opening and closing it); otherwise, when the Connection does not run, a
    temporary bus that is open inside the block and closed after it (shared
    with other actions on the Connection). Raises as ``check_access`` does,
    and ``BusTransportError`` when a temporary bus cannot open.
    """
    bus = check_access(host, connection)
    if bus is not None:
        yield bus
        return
    temporary = host.mbus_scans.temporary_buses
    async with temporary.lease(host.registry, connection) as bus:
        yield bus


def _check_running_holders(
    host: BusHost, connection_id: str, endpoint: tuple[object, ...]
) -> None:
    """Raise ``BusInUse`` when another running worker holds ``endpoint``."""
    config = host.running_config
    if config is None:
        return
    for other in config.connections:
        if other.id == connection_id or other.protocol != MBUS_PROTOCOL:
            continue
        if host.mbus_bus(other.id) is None:
            continue  # not running: holds nothing
        try:
            held = endpoint_key(other.endpoint)
        except ConfigurationError:
            continue
        if held == endpoint:
            raise BusInUse(other.id)


def _running_connection(host: BusHost, connection_id: str) -> ConnectionConfig | None:
    config = host.running_config
    if config is None:
        return None
    return next((c for c in config.connections if c.id == connection_id), None)


@dataclass(slots=True)
class _Lease:
    bus: MBusBus
    settings: tuple[object, ...]
    endpoint: tuple[object, ...]
    users: int = 0


class TemporaryBuses:
    """The temporary buses of Connections that do not run, one per Connection."""

    def __init__(self) -> None:
        """Start with no bus."""
        self._leases: dict[str, _Lease] = {}

    def check(
        self,
        connection_id: str,
        settings: tuple[object, ...],
        endpoint: tuple[object, ...],
    ) -> None:
        """Refuse a bus the Connection or another one already holds otherwise.

        ``BusSettingsDiffer`` when the Connection's temporary bus has other
        settings; ``BusInUse`` when another Connection's temporary bus holds
        the endpoint.
        """
        for holder, held in self._leases.items():
            if holder != connection_id and held.endpoint == endpoint:
                raise BusInUse(holder)
        lease = self._leases.get(connection_id)
        if lease is not None and lease.settings != settings:
            raise BusSettingsDiffer(TEMPORARY_DIFFERS_MESSAGE)

    @contextlib.asynccontextmanager
    async def lease(
        self, registry: DriverRegistry, connection: ConnectionConfig
    ) -> AsyncIterator[MBusBus]:
        """The Connection's temporary bus, opened; closed by its last user."""
        settings = bus_settings(connection)
        endpoint = endpoint_key(connection.endpoint)
        self.check(connection.id, settings, endpoint)
        lease = self._leases.get(connection.id)
        if lease is None:
            lease = _Lease(_new_bus(registry, connection), settings, endpoint)
            self._leases[connection.id] = lease
        lease.users += 1
        try:
            await lease.bus.open()
            yield lease.bus
        finally:
            lease.users -= 1
            if lease.users == 0:
                if self._leases.get(connection.id) is lease:
                    del self._leases[connection.id]
                await lease.bus.close()

    async def close_all(self) -> None:
        """Close every temporary bus; their users' next turn fails."""
        leases = list(self._leases.values())
        self._leases.clear()
        for lease in leases:
            await lease.bus.close()


def _new_bus(registry: DriverRegistry, connection: ConnectionConfig) -> MBusBus:
    """A closed bus built exactly as the Connection's worker would build it."""
    driver = registry.create_driver(connection)
    if not isinstance(driver, MBusDriver):
        raise ConfigurationError(
            f"Connection {connection.id!r} is not an M-Bus Connection.",
            code="invalid_mbus_connection",
        )
    return driver.bus
