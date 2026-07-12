"""Minimal application health status."""

from __future__ import annotations

from typing import TypedDict

from plc_gateway._version import get_version


class HealthStatus(TypedDict):
    """Serializable health status returned by the bootstrap command."""

    application: str
    status: str
    version: str


def build_health_status() -> HealthStatus:
    """Build a minimal health status payload."""
    return {
        "application": "plc-gateway",
        "status": "ok",
        "version": get_version(),
    }
