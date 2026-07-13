"""Shared asynchronous contract for communication protocol drivers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from plc_gateway.domain import ConnectionConfig, TagRequest, TagResult


@dataclass(frozen=True, slots=True)
class DriverCapabilities:
    """Observable capabilities declared by a communication driver."""

    protocol: str
    supports_batch_read: bool = True
    supports_health_check: bool = True
    max_batch_size: int | None = None

    def __post_init__(self) -> None:
        """Validate capability invariants."""
        protocol = self.protocol.strip().lower()
        if not protocol:
            raise ValueError("protocol cannot be empty.")
        if self.max_batch_size is not None and self.max_batch_size <= 0:
            raise ValueError("max_batch_size must be positive when provided.")
        object.__setattr__(self, "protocol", protocol)


@runtime_checkable
class CommunicationDriver(Protocol):
    """Protocol-independent async communication driver.

    Implementations own exactly one protocol client/connection instance and must
    not expose concrete protocol-library objects to runtime callers.

    Timeout contract:
    - connection lifecycle and health operations honor the timeout from the
      `ConnectionConfig` used by the factory,
    - reads honor `TagRequest.timeout_ms` when set,
    - drivers may apply stricter internal protocol timeouts, but must surface
      timeout failures as `TransientCommunicationError` unless the protocol can
      classify them as permanent.

    Cancellation contract:
    - implementations must not swallow `asyncio.CancelledError`,
    - cancellation should trigger best-effort cleanup without blocking shutdown.
    """

    @property
    def capabilities(self) -> DriverCapabilities:
        """Return driver capabilities visible to runtime orchestration."""
        ...

    async def connect(self) -> None:
        """Open the driver-owned protocol connection."""
        ...

    async def disconnect(self) -> None:
        """Close the protocol connection and release owned resources."""
        ...

    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]:
        """Read a batch of tags and return one result per requested tag."""
        ...

    async def health_check(self) -> bool:
        """Return whether the owned protocol connection appears healthy."""
        ...


class DriverFactory(Protocol):
    """Factory that creates a driver for one validated connection config."""

    def __call__(self, connection: ConnectionConfig) -> CommunicationDriver:
        """Create a new driver instance owned by a single worker."""
        ...
