"""Read-only HTTP API for PLC Gateway runtime status."""

from plc_gateway.api.app import create_api_app
from plc_gateway.api.state import RuntimeApiState

__all__ = [
    "RuntimeApiState",
    "create_api_app",
]
