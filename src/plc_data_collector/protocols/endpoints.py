"""Endpoint helpers shared by the TCP-based protocols."""

from __future__ import annotations

from urllib.parse import urlsplit

from plc_data_collector.domain import ConfigurationError


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    """Split ``tcp://host:port`` into host and port."""
    try:
        parts = urlsplit(endpoint)
        port = parts.port
        host = parts.hostname
    except ValueError as error:
        raise _endpoint_error(endpoint) from error
    if (
        parts.scheme != "tcp"
        or "@" in parts.netloc
        or parts.path
        or parts.query
        or parts.fragment
        or "?" in endpoint
        or "#" in endpoint
        or not host
        or port is None
        or not 1 <= port <= 65535
    ):
        raise _endpoint_error(endpoint)
    return host, port


def _endpoint_error(endpoint: str) -> ConfigurationError:
    return ConfigurationError(
        f"Invalid Modbus endpoint {endpoint!r}; "
        "use tcp://host:port with a port from 1 to 65535.",
        code="invalid_modbus_endpoint",
    )


def format_endpoint(host: str, port: int) -> str:
    """Build ``tcp://host:port``, bracketing IPv6 hosts."""
    shown = f"[{host}]" if ":" in host else host
    return f"tcp://{shown}:{port}"
