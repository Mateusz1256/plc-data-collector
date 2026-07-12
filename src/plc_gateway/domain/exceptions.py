"""Domain exception hierarchy."""

from __future__ import annotations

from collections.abc import Mapping


class GatewayError(Exception):
    """Base class for PLC Gateway domain errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """Create a domain error with optional structured details."""
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = dict(details or {})


class ConfigurationError(GatewayError):
    """Raised when configuration or domain invariants are invalid."""


class TransientCommunicationError(GatewayError):
    """Raised for communication failures that may succeed after retry."""


class PermanentProtocolError(GatewayError):
    """Raised for protocol failures that should not be retried blindly."""


class StorageError(GatewayError):
    """Raised when persistence fails."""
