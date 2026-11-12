"""Process-level runtime settings read from the environment."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from plc_data_collector.app.config import ENV_PREFIX
from plc_data_collector.app.service import ServicePaths
from plc_data_collector.domain import ConfigurationError

DATABASE_FILE_NAME = "collector.db"
SPOOL_FILE_NAME = "spool.db"


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Settings that wire the collector runtime together."""

    database_url: str
    config_file: Path
    data_dir: Path
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    database_wait_timeout_s: float = 60.0
    queue_max_size: int = 1000
    writer_batch_size: int = 100
    writer_flush_interval_s: float = 1.0
    storage_timeout_s: float = 5.0
    spool_max_items: int = 100000

    def __post_init__(self) -> None:
        """Reject values the runtime components cannot work with."""
        if not 0 < self.api_port < 65536:
            raise _invalid("api_port", "must be between 1 and 65535")
        for name in (
            "database_wait_timeout_s",
            "queue_max_size",
            "writer_batch_size",
            "writer_flush_interval_s",
            "storage_timeout_s",
            "spool_max_items",
        ):
            if getattr(self, name) <= 0:
                raise _invalid(name, "must be positive")

    @property
    def spool_path(self) -> Path:
        """Return the durable Spool file location inside the data directory."""
        return self.data_dir / SPOOL_FILE_NAME

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> RuntimeSettings:
        """Build settings from ``PLC_DATA_COLLECTOR_*`` variables.

        Keyword overrides win over the environment. A ``None`` override counts
        as "not given", so CLI code can pass optional arguments straight through.
        """
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise TypeError(f"Unknown runtime setting(s): {', '.join(unknown)}.")
        env = os.environ if environ is None else environ
        given: dict[str, object] = {
            key: value for key, value in overrides.items() if value is not None
        }

        def resolve(name: str, parse: Callable[[str], object]) -> object | None:
            if name in given:
                return given[name]
            raw = env.get(f"{ENV_PREFIX}{_ENV_NAMES.get(name, name.upper())}")
            if raw is None or not raw.strip():
                return None
            try:
                return parse(raw.strip())
            except ValueError:
                raise _invalid(name, f"cannot parse {raw!r}") from None

        config_file = resolve("config_file", Path)
        if config_file is None:
            raise ConfigurationError(
                f"Collector configuration file is required; set {ENV_PREFIX}CONFIG.",
                code="config_file_required",
            )
        data_dir = ServicePaths.from_values(
            data_dir=_as_path(given.get("data_dir")),
            environ=env,
        ).data_dir
        values: dict[str, object] = {
            "config_file": Path(str(config_file)).expanduser(),
            "data_dir": data_dir,
            "database_url": resolve("database_url", str)
            or f"sqlite:///{data_dir / DATABASE_FILE_NAME}",
        }
        for name, parse in _PARSERS.items():
            value = resolve(name, parse)
            if value is not None:
                values[name] = value
        return cls(**values)  # type: ignore[arg-type]


_ENV_NAMES = {"config_file": "CONFIG"}
_PARSERS: dict[str, Callable[[str], object]] = {
    "api_host": str,
    "api_port": int,
    "database_wait_timeout_s": float,
    "queue_max_size": int,
    "writer_batch_size": int,
    "writer_flush_interval_s": float,
    "storage_timeout_s": float,
    "spool_max_items": int,
}


def _as_path(value: object) -> Path | None:
    return None if value is None else Path(str(value))


def _invalid(name: str, reason: str) -> ConfigurationError:
    return ConfigurationError(
        f"Invalid runtime setting {name}: {reason}.",
        code="invalid_runtime_setting",
        details={"setting": name},
    )
