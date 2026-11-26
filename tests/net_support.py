"""Network helpers shared by the protocol test support modules."""

from __future__ import annotations

import socket


def free_port() -> int:
    """Return a TCP port on 127.0.0.1 that was free a moment ago."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
