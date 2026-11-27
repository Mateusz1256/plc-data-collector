"""What the Panel needs from the process that runs the collector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from plc_data_collector.api.state import RuntimeApiState
    from plc_data_collector.app.config import CollectorConfig
    from plc_data_collector.app.mbus_scan import ScanRegistry
    from plc_data_collector.app.stores import Stores
    from plc_data_collector.protocols import DriverRegistry
    from plc_data_collector.protocols.mbus_bus import MBusBus
    from plc_data_collector.runtime.live_values import LiveValueCache


class CollectorHost(Protocol):
    """The ``RuntimeSupervisor`` surface the Panel reads and drives.

    ``RuntimeSupervisor`` satisfies it; tests use a scripted fake.
    """

    @property
    def stores(self) -> Stores | None:
        """Return the database stores, or None while the database is not ready."""
        ...

    @property
    def live_values(self) -> LiveValueCache:
        """Return the live value cache the runtime fills on every poll."""
        ...

    @property
    def running_revision(self) -> int | None:
        """Return the configuration revision the runtime runs, or None."""
        ...

    @property
    def running_config(self) -> CollectorConfig | None:
        """Return the configuration the runtime runs, or None."""
        ...

    @property
    def last_error_code(self) -> str | None:
        """Return the code of the last failed start or apply, if any."""
        ...

    @property
    def applying(self) -> bool:
        """Return whether an apply is in progress."""
        ...

    @property
    def registry(self) -> DriverRegistry:
        """Return the protocol driver registry the runtimes use."""
        ...

    @property
    def mbus_scans(self) -> ScanRegistry:
        """Return the M-Bus scan jobs and temporary buses."""
        ...

    def mbus_bus(self, connection_id: str) -> MBusBus | None:
        """Return the bus of the running M-Bus worker for the Connection, if any."""
        ...

    def api_state(self) -> RuntimeApiState:
        """Return a fresh runtime snapshot."""
        ...

    def trigger_retention(self) -> None:
        """Run retention now in the background; call it on the event loop."""
        ...

    async def apply(self) -> None:
        """Restart collection with the configuration saved in the database."""
        ...
