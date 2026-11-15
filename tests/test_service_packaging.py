from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
from pathlib import Path
from typing import Any

import pytest

from plc_data_collector.__main__ import main
from plc_data_collector.app.logging import configure_logging
from plc_data_collector.app.service import (
    PidFile,
    ServicePaths,
    run_service_until_stopped,
)


def test_service_paths_use_environment_and_create_directories(
    tmp_path: Path,
) -> None:
    paths = ServicePaths.from_values(
        environ={
            "PLC_DATA_COLLECTOR_DATA_DIR": str(tmp_path / "data"),
            "PLC_DATA_COLLECTOR_LOG_DIR": str(tmp_path / "logs"),
            "PLC_DATA_COLLECTOR_RUN_DIR": str(tmp_path / "run"),
            "PLC_DATA_COLLECTOR_CONFIG": str(tmp_path / "collector.config.json"),
        }
    )

    paths.ensure_directories()

    assert paths.data_dir == (tmp_path / "data").resolve()
    assert paths.log_dir.is_dir()
    assert paths.run_dir.is_dir()
    assert paths.config_file == (tmp_path / "collector.config.json").resolve()
    assert paths.pid_file == (tmp_path / "run" / "plc-data-collector.pid").resolve()
    assert paths.log_file == (tmp_path / "logs" / "plc-data-collector.log").resolve()


def test_pid_file_writes_and_removes_current_process_id(tmp_path: Path) -> None:
    pid_path = tmp_path / "run" / "plc-data-collector.pid"
    pid_file = PidFile(pid_path)

    pid_file.write(pid=1234)
    assert pid_path.read_text(encoding="utf-8") == "1234\n"

    pid_file.remove()
    assert not pid_path.exists()


def test_configure_logging_writes_json_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "plc-data-collector.log"
    configure_logging("INFO", log_file=log_file)

    logging.getLogger("test").info("hello")

    payload = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert payload["level"] == "INFO"
    assert payload["message"] == "hello"


def test_configure_logging_writes_extra_fields(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "plc-data-collector.log"
    configure_logging("INFO", log_file=log_file)

    logging.getLogger("test").error(
        "service_start_failed",
        extra={"error_code": "config_file_required", "path": tmp_path},
    )

    payload = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert payload["message"] == "service_start_failed"
    assert payload["error_code"] == "config_file_required"
    assert payload["path"] == str(tmp_path)
    assert "args" not in payload


@pytest.mark.asyncio
async def test_service_host_creates_paths_writes_pid_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = asyncio.Event()
    config_file = tmp_path / "collector.config.json"
    config_file.write_text(
        json.dumps(mock_config()),
        encoding="utf-8",
    )
    paths = ServicePaths.from_values(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        run_dir=tmp_path / "run",
        config_file=config_file,
    )

    monkeypatch.setattr(
        "plc_data_collector.app.service.install_signal_handlers",
        lambda _event: [],
    )

    task = asyncio.create_task(
        run_service_until_stopped(
            paths=paths,
            log_level="INFO",
            stop_event=stop_event,
            environ={"PLC_DATA_COLLECTOR_API_PORT": str(free_port())},
        )
    )
    await wait_until(
        lambda: paths.pid_file is not None and paths.pid_file.exists(),
        timeout_s=10.0,
    )

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

    def fake_run_service(
        paths: ServicePaths,
        *,
        log_level: str,
        api_host: str | None,
        api_port: int | None,
    ) -> int:
        calls.append(
            {
                "paths": paths,
                "log_level": log_level,
                "api_host": api_host,
                "api_port": api_port,
            }
        )
        return 0

    monkeypatch.setattr("plc_data_collector.__main__.run_service", fake_run_service)

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
            str(tmp_path / "collector.config.json"),
            "--log-level",
            "DEBUG",
            "--api-host",
            "0.0.0.0",
            "--api-port",
            "9191",
        ]
    )

    assert exit_code == 0
    assert calls[0]["log_level"] == "DEBUG"
    assert calls[0]["api_host"] == "0.0.0.0"
    assert calls[0]["api_port"] == 9191
    paths = calls[0]["paths"]
    assert isinstance(paths, ServicePaths)
    assert paths.data_dir == (tmp_path / "data").resolve()
    assert paths.log_dir == (tmp_path / "logs").resolve()
    assert paths.run_dir == (tmp_path / "run").resolve()
    assert paths.config_file == (tmp_path / "collector.config.json").resolve()


@pytest.mark.asyncio
async def test_service_host_starts_without_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without a seed file the service starts with an empty configuration;
    # configuration is added through the Panel.
    paths = ServicePaths.from_values(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        run_dir=tmp_path / "run",
        environ={},
    )
    monkeypatch.setattr(
        "plc_data_collector.app.service.install_signal_handlers",
        lambda _event: [],
    )
    # Tests that run Alembic with alembic.ini call fileConfig(), which disables
    # loggers that already exist in this test process.
    for name in ("plc_data_collector.app.service", "plc_data_collector.app.supervisor"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    stop_event = asyncio.Event()
    assert paths.log_file is not None
    log_file = paths.log_file

    task = asyncio.create_task(
        run_service_until_stopped(
            paths=paths,
            log_level="INFO",
            stop_event=stop_event,
            environ={"PLC_DATA_COLLECTOR_API_PORT": str(free_port())},
        )
    )
    await wait_until(
        lambda: (
            log_file.exists()
            and "configuration_empty" in log_file.read_text(encoding="utf-8")
        ),
        timeout_s=10.0,
    )
    stop_event.set()
    exit_code = await asyncio.wait_for(task, timeout=15.0)

    assert exit_code == 0
    assert "service_start_failed" not in log_file.read_text(encoding="utf-8")
    assert paths.pid_file is not None
    assert not paths.pid_file.exists()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def mock_config() -> dict[str, object]:
    return {
        "connections": [
            {
                "id": "line_1",
                "protocol": "mock",
                "endpoint": "mock://line_1",
                "protocol_options": {"tags": {"line_1_temperature": {"value": 1.0}}},
            }
        ],
        "tag_groups": [{"id": "fast", "connection_id": "line_1", "interval_ms": 100}],
        "tags": [
            {
                "id": "line_1_temperature",
                "tag_group_id": "fast",
                "name": "Line 1 Temperature",
                "address": "mock://line_1_temperature",
                "value_type": "numeric",
            }
        ],
    }


async def wait_until(condition: object, *, timeout_s: float = 1.0) -> None:
    """Wait until a predicate returns true."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if callable(condition) and condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_compose_publishes_the_collector_on_loopback_only() -> None:
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    published = re.findall(r'^\s+- "([^"]*:8080)"', compose, re.MULTILINE)
    assert published == ["127.0.0.1:${COLLECTOR_API_PORT:-8080}:8080"]
