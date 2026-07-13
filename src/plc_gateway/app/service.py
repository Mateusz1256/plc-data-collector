"""Long-running process host helpers for service deployments."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from plc_gateway._version import get_version
from plc_gateway.app.logging import configure_logging

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServicePaths:
    """Filesystem paths used by the long-running process."""

    data_dir: Path
    log_dir: Path
    run_dir: Path
    config_file: Path | None = None
    pid_file: Path | None = None
    log_file: Path | None = None

    @classmethod
    def from_values(
        cls,
        *,
        data_dir: str | Path | None = None,
        log_dir: str | Path | None = None,
        run_dir: str | Path | None = None,
        config_file: str | Path | None = None,
        pid_file: str | Path | None = None,
        log_file: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> ServicePaths:
        """Build paths from CLI values, environment, and platform defaults."""
        env = os.environ if environ is None else environ
        default_data_dir = _default_data_dir(env)
        resolved_data_dir = _path_from_value(
            data_dir,
            env.get("PLC_GATEWAY_DATA_DIR"),
            default_data_dir,
        )
        resolved_log_dir = _path_from_value(
            log_dir,
            env.get("PLC_GATEWAY_LOG_DIR"),
            resolved_data_dir / "logs",
        )
        resolved_run_dir = _path_from_value(
            run_dir,
            env.get("PLC_GATEWAY_RUN_DIR"),
            resolved_data_dir / "run",
        )
        return cls(
            data_dir=resolved_data_dir,
            log_dir=resolved_log_dir,
            run_dir=resolved_run_dir,
            config_file=_optional_path(config_file or env.get("PLC_GATEWAY_CONFIG")),
            pid_file=_path_from_value(
                pid_file,
                env.get("PLC_GATEWAY_PID_FILE"),
                resolved_run_dir / "plc-gateway.pid",
            ),
            log_file=_path_from_value(
                log_file,
                env.get("PLC_GATEWAY_LOG_FILE"),
                resolved_log_dir / "plc-gateway.log",
            ),
        )

    def ensure_directories(self) -> None:
        """Create data, log, and run directories."""
        for directory in (self.data_dir, self.log_dir, self.run_dir):
            directory.mkdir(parents=True, exist_ok=True)


class PidFile:
    """PID file lifecycle manager."""

    def __init__(self, path: Path) -> None:
        """Create a PID file manager."""
        self._path = path
        self._written = False

    @property
    def path(self) -> Path:
        """Return the PID file path."""
        return self._path

    def write(self, pid: int | None = None) -> None:
        """Write the current process PID."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            f"{os.getpid() if pid is None else pid}\n",
            encoding="utf-8",
        )
        self._written = True

    def remove(self) -> None:
        """Remove the PID file if this manager wrote it."""
        if not self._written:
            return
        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        finally:
            self._written = False


async def run_service_until_stopped(
    *,
    paths: ServicePaths,
    log_level: str,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Run the service host until a stop event or process signal is received."""
    paths.ensure_directories()
    configure_logging(log_level, log_file=paths.log_file)
    LOGGER.info(
        "service_starting",
        extra={
            "data_dir": str(paths.data_dir),
            "log_dir": str(paths.log_dir),
            "run_dir": str(paths.run_dir),
            "config_file": str(paths.config_file) if paths.config_file else None,
            "version": get_version(),
        },
    )
    owned_stop_event = stop_event or asyncio.Event()
    signal_callbacks = install_signal_handlers(owned_stop_event)
    pid_file = PidFile(paths.pid_file or paths.run_dir / "plc-gateway.pid")
    try:
        pid_file.write()
        LOGGER.info(
            "service_started",
            extra={"pid_file": str(pid_file.path), "pid": os.getpid()},
        )
        await owned_stop_event.wait()
        LOGGER.info("service_stopping")
        return 0
    finally:
        for callback in signal_callbacks:
            callback()
        pid_file.remove()
        LOGGER.info("service_stopped")


def run_service(paths: ServicePaths, *, log_level: str) -> int:
    """Run the async service host from synchronous CLI code."""
    return asyncio.run(run_service_until_stopped(paths=paths, log_level=log_level))


def install_signal_handlers(stop_event: asyncio.Event) -> list[Callable[[], None]]:
    """Install supported process signal handlers for graceful shutdown."""
    loop = asyncio.get_running_loop()
    callbacks: list[Callable[[], None]] = []
    for sig in _shutdown_signals():
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            previous_handler = signal.getsignal(sig)

            def handler(
                _signum: int,
                _frame: FrameType | None,
                *,
                event: asyncio.Event = stop_event,
            ) -> None:
                event.set()

            signal.signal(sig, handler)

            def restore(
                *,
                restore_signal: signal.Signals = sig,
                restore_handler: Any = previous_handler,
            ) -> None:
                signal.signal(restore_signal, restore_handler)

            callbacks.append(restore)
        else:

            def remove(*, remove_signal: signal.Signals = sig) -> None:
                loop.remove_signal_handler(remove_signal)

            callbacks.append(remove)
    return callbacks


def _shutdown_signals() -> Sequence[signal.Signals]:
    signals = [signal.SIGTERM, signal.SIGINT]
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    return tuple(signals)


def _default_data_dir(environ: Mapping[str, str]) -> Path:
    if sys.platform == "win32":
        root = environ.get("PROGRAMDATA")
        if root:
            return Path(root) / "PLC Gateway"
        return Path.home() / "AppData" / "Local" / "PLC Gateway"
    state_home = environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "plc-gateway"
    return Path.home() / ".local" / "state" / "plc-gateway"


def _path_from_value(
    cli_value: str | Path | None,
    env_value: str | None,
    default: Path,
) -> Path:
    return Path(cli_value or env_value or default).expanduser().resolve()


def _optional_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser().resolve()
