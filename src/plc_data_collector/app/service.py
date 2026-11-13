"""Long-running process host helpers for service deployments."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

import uvicorn

from plc_data_collector._version import get_version
from plc_data_collector.app.logging import configure_logging
from plc_data_collector.domain import CollectorError

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
            env.get("PLC_DATA_COLLECTOR_DATA_DIR"),
            default_data_dir,
        )
        resolved_log_dir = _path_from_value(
            log_dir,
            env.get("PLC_DATA_COLLECTOR_LOG_DIR"),
            resolved_data_dir / "logs",
        )
        resolved_run_dir = _path_from_value(
            run_dir,
            env.get("PLC_DATA_COLLECTOR_RUN_DIR"),
            resolved_data_dir / "run",
        )
        return cls(
            data_dir=resolved_data_dir,
            log_dir=resolved_log_dir,
            run_dir=resolved_run_dir,
            config_file=_optional_path(
                config_file or env.get("PLC_DATA_COLLECTOR_CONFIG")
            ),
            pid_file=_path_from_value(
                pid_file,
                env.get("PLC_DATA_COLLECTOR_PID_FILE"),
                resolved_run_dir / "plc-data-collector.pid",
            ),
            log_file=_path_from_value(
                log_file,
                env.get("PLC_DATA_COLLECTOR_LOG_FILE"),
                resolved_log_dir / "plc-data-collector.log",
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
    environ: Mapping[str, str] | None = None,
    api_host: str | None = None,
    api_port: int | None = None,
) -> int:
    """Run the collector runtime and the API until a stop event or signal.

    ``api_host`` and ``api_port`` come from the CLI and win over the
    environment; ``None`` falls back to the environment, then the default.
    Returns 0 after a requested stop (even one that arrives while the runtime
    is still starting) and 1 when the runtime or the API fails to start.
    """
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
    pid_file = PidFile(paths.pid_file or paths.run_dir / "plc-data-collector.pid")
    try:
        pid_file.write()
        LOGGER.info(
            "service_started",
            extra={"pid_file": str(pid_file.path), "pid": os.getpid()},
        )
        return await _run_runtime_and_api(
            paths,
            owned_stop_event,
            environ,
            api_overrides={"api_host": api_host, "api_port": api_port},
        )
    finally:
        for callback in signal_callbacks:
            callback()
        pid_file.remove()
        LOGGER.info("service_stopped")


async def _run_runtime_and_api(
    paths: ServicePaths,
    stop_event: asyncio.Event,
    environ: Mapping[str, str] | None,
    *,
    api_overrides: Mapping[str, object],
) -> int:
    # Imported here: settings imports ServicePaths from this module.
    from plc_data_collector.api import create_api_app
    from plc_data_collector.app.settings import RuntimeSettings
    from plc_data_collector.app.supervisor import RuntimeSupervisor

    try:
        settings = RuntimeSettings.from_env(
            environ,
            data_dir=paths.data_dir,
            config_file=paths.config_file,
            **api_overrides,
        )
    except CollectorError as error:
        _log_start_failed(error)
        return 1

    supervisor = RuntimeSupervisor(settings)
    server = _ServiceApiServer(
        uvicorn.Config(
            create_api_app(supervisor.api_state),
            host=settings.api_host,
            port=settings.api_port,
            log_config=None,
            # Bounds in-flight requests so supervisor.stop() still fits in
            # Docker's stop grace period.
            timeout_graceful_shutdown=5,
        )
    )
    # The API serves while the supervisor prepares the database and starts the
    # runtime, so /health/live answers and /health/ready reports 503 until the
    # runtime is running.
    server_task = asyncio.create_task(_serve_api(server), name="service-api")
    runtime_task = asyncio.create_task(supervisor.start(), name="service-runtime")
    stop_task = asyncio.create_task(stop_event.wait(), name="service-stop")
    exit_code = 0
    try:
        pending: set[asyncio.Task[Any]] = {server_task, runtime_task, stop_task}
        while stop_task in pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            failure = _start_failure(done, runtime_task, server_task)
            if failure is not None:
                _log_start_failed(failure)
                exit_code = 1
                break
    finally:
        LOGGER.info("service_stopping")
        stop_task.cancel()
        server.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)
        await supervisor.stop()
        # A stop during start() makes start() raise runtime_start_aborted,
        # which is a clean shutdown, not a failure.
        await asyncio.gather(runtime_task, return_exceptions=True)
    return exit_code


def _start_failure(
    done: set[asyncio.Task[Any]],
    runtime_task: asyncio.Task[None],
    server_task: asyncio.Task[None],
) -> BaseException | None:
    """Return the error that ends the service early, if any."""
    if runtime_task in done and not runtime_task.cancelled():
        error = runtime_task.exception()
        if error is not None:
            return error
    if server_task in done:
        if server_task.cancelled():
            return CollectorError("API server was cancelled.", code="api_stopped")
        return server_task.exception() or CollectorError(
            "API server stopped unexpectedly.", code="api_stopped"
        )
    return None


def _log_start_failed(error: BaseException) -> None:
    # Only the code and type are logged: messages may quote URLs or options.
    LOGGER.error(
        "service_start_failed",
        extra={
            "error_code": getattr(error, "code", None) or "unexpected_error",
            "error_type": type(error).__name__,
        },
    )


class _ServiceApiServer(uvicorn.Server):
    """uvicorn server that leaves signal handling to the service host.

    ``Server.serve()`` normally replaces the SIGTERM/SIGINT handlers and
    re-raises the signal after it returns, which would kill the process before
    the runtime is stopped. The service host owns the signals instead and
    stops the server through ``should_exit``.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


async def _serve_api(server: uvicorn.Server) -> None:
    try:
        await server.serve()
    except SystemExit:
        # uvicorn calls sys.exit(1) when it cannot bind its socket.
        raise CollectorError(
            "API server failed to start.", code="api_start_failed"
        ) from None
    finally:
        # serve() skips its own shutdown when a stop arrives during startup.
        for listener in getattr(server, "servers", ()):
            listener.close()


def run_service(
    paths: ServicePaths,
    *,
    log_level: str,
    api_host: str | None = None,
    api_port: int | None = None,
) -> int:
    """Run the async service host from synchronous CLI code."""
    return asyncio.run(
        run_service_until_stopped(
            paths=paths,
            log_level=log_level,
            api_host=api_host,
            api_port=api_port,
        )
    )


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
            return Path(root) / "PLC Data Collector"
        return Path.home() / "AppData" / "Local" / "PLC Data Collector"
    state_home = environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "plc-data-collector"
    return Path.home() / ".local" / "state" / "plc-data-collector"


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
