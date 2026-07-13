"""Build metadata helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass

from plc_gateway._version import get_version


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Application build metadata visible through CLI and API."""

    version: str
    commit_sha: str | None


def get_build_info(environ: dict[str, str] | None = None) -> BuildInfo:
    """Return build metadata from package version and build environment."""
    env = os.environ if environ is None else environ
    commit_sha = env.get("PLC_GATEWAY_COMMIT_SHA") or None
    return BuildInfo(version=get_version(), commit_sha=commit_sha)
