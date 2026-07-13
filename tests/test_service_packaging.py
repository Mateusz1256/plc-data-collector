from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from plc_gateway.__main__ import main
from plc_gateway.app.logging import configure_logging
from plc_gateway.app.service import PidFile, ServicePaths, run_service_until_stopped


def test_service_paths_use_environment_and_create_directories(
    tmp_path: Path,
) -> None:
    paths = ServicePaths.from_values(
        environ={
            "PLC_GATEWAY_DATA_DIR": str(tmp_path / "data"),
            "PLC_GATEWAY_LOG_DIR": str(tmp_path / "logs"),
            "PLC_GATEWAY_RUN_DIR": str(tmp_path / "run"),
            "PLC_GATEWAY_CONFIG": str(tmp_path / "gateway.json"),
        }
    )

    paths.ensure_directories()

    assert paths.data_dir == (tmp_path / "data").resolve()
    assert paths.log_dir.is_dir()
    assert paths.run_dir.is_dir()
    assert paths.config_file == (tmp_path / "gateway.json").resolve()
    assert paths.pid_file == (tmp_path / "run" / "plc-gateway.pid").resolve()
    assert paths.log_file == (tmp_path / "logs" / "plc-gateway.log").resolve()


def test_pid_file_writes_and_removes_current_process_id(tmp_path: Path) -> None:
    pid_path = tmp_path / "run" / "plc-gateway.pid"
    pid_file = PidFile(pid_path)

    pid_file.write(pid=1234)
    assert pid_path.read_text(encoding="utf-8") == "1234\n"

    pid_file.remove()
    assert not pid_path.exists()


def test_configure_logging_writes_json_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "plc-gateway.log"
    configure_logging("INFO", log_file=log_file)

    import logging

    logging.getLogger("test").info("hello")

    payload = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert payload["level"] == "INFO"
    assert payload["message"] == "hello"


@pytest.mark.asyncio
async def test_service_host_creates_paths_writes_pid_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = asyncio.Event()
    paths = ServicePaths.from_values(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        run_dir=tmp_path / "run",
    )

    monkeypatch.setattr(
        "plc_gateway.app.service.install_signal_handlers",
        lambda _event: [],
    )

    task = asyncio.create_task(
        run_service_until_stopped(paths=paths, log_level="INFO", stop_event=stop_event)
    )
    await wait_until(lambda: paths.pid_file is not None and paths.pid_file.exists())

    stop_event.set()
    exit_code = await task

    assert exit_code == 0
    assert paths.data_dir.is_dir()
    assert paths.log_file is not None
    assert paths.log_file.exists()
    assert paths.pid_file is not None
    assert not paths.pid_file.exists()


def test_main_runs_service_with_configurable_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run_service(paths: ServicePaths, *, log_level: str) -> int:
        calls.append({"paths": paths, "log_level": log_level})
        return 0

    monkeypatch.setattr("plc_gateway.__main__.run_service", fake_run_service)

    exit_code = main(
        [
            "--run-service",
            "--data-dir",
            str(tmp_path / "data"),
            "--log-dir",
            str(tmp_path / "logs"),
            "--run-dir",
            str(tmp_path / "run"),
            "--config",
            str(tmp_path / "gateway.json"),
            "--log-level",
            "DEBUG",
        ]
    )

    assert exit_code == 0
    assert calls[0]["log_level"] == "DEBUG"
    paths = calls[0]["paths"]
    assert isinstance(paths, ServicePaths)
    assert paths.data_dir == (tmp_path / "data").resolve()
    assert paths.log_dir == (tmp_path / "logs").resolve()
    assert paths.run_dir == (tmp_path / "run").resolve()
    assert paths.config_file == (tmp_path / "gateway.json").resolve()


async def wait_until(condition: object, *, timeout_s: float = 1.0) -> None:
    """Wait until a predicate returns true."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if callable(condition) and condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")
