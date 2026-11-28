"""Endpoint helpers shared by the TCP- and serial-based protocols."""

from __future__ import annotations

from urllib.parse import urlsplit

from plc_data_collector.domain import ConfigurationError


def parse_endpoint(endpoint: str, *, protocol_label: str = "Modbus") -> tuple[str, int]:
    """Split ``tcp://host:port`` into host and port.

    ``protocol_label`` only names the protocol in the error message and code.
    """
    try:
        parts = urlsplit(endpoint)
        port = parts.port
        host = parts.hostname
    except ValueError as error:
        raise _endpoint_error(endpoint, protocol_label) from error
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
        raise _endpoint_error(endpoint, protocol_label)
    return host, port


def _endpoint_error(endpoint: str, protocol_label: str) -> ConfigurationError:
    return ConfigurationError(
        f"Invalid {protocol_label} endpoint {endpoint!r}; "
        "use tcp://host:port with a port from 1 to 65535.",
        code=f"invalid_{protocol_label.lower().replace('-', '')}_endpoint",
    )


_SERIAL_SCHEME = "serial://"


def is_serial_endpoint(endpoint: str) -> bool:
    """Return whether the endpoint names a serial port (``serial://...``)."""
    return endpoint[: len(_SERIAL_SCHEME)].lower() == _SERIAL_SCHEME


def parse_serial_endpoint(endpoint: str) -> str:
    """Return the device of ``serial://<device>``.

    ``serial:///dev/ttyUSB0`` gives ``/dev/ttyUSB0`` and ``serial://COM3``
    gives ``COM3``. The device is not checked for existence.
    """
    device = endpoint[len(_SERIAL_SCHEME) :]
    if (
        not is_serial_endpoint(endpoint)
        or not device
        or any(char.isspace() or not char.isprintable() for char in device)
        or "?" in device
        or "#" in device
    ):
        raise ConfigurationError(
            f"Invalid serial endpoint {endpoint!r}; use serial://<device>, "
            "for example serial:///dev/ttyUSB0 or serial://COM3.",
            code="invalid_serial_endpoint",
        )
    return device


def format_endpoint(host: str, port: int) -> str:
    """Build ``tcp://host:port``, bracketing IPv6 hosts."""
    shown = f"[{host}]" if ":" in host else host
    return f"tcp://{shown}:{port}"
