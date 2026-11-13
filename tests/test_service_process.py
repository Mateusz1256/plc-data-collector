"""End-to-end tests for ``plc-data-collector --run-service`` as a real process."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

STARTUP_TIMEOUT_S = 15.0
SHUTDOWN_TIMEOUT_S = 15.0
UNREACHABLE_DATABASE_URL = "postgresql+pg8000://u:p@127.0.0.1:1/x"


def mock_config() -> dict[str, object]:
    return {
        "connections": [
            {
                "id": "line_1",
                "protocol": "mock",
                "endpoint": "mock://line_1",
                "enabled": True,
                "timeout_ms": 1000,
                "protocol_options": {
                    "tags": {"line_1_temperature": {"sequence": [20.0, 21.0, 22.0]}}
                },
            }
        ],
        "tag_groups": [
            {
                "id": "fast",
                "connection_id": "line_1",
                "interval_ms": 100,
                "timeout_ms": 500,
                "enabled": True,
            }
        ],
        "tags": [
            {
                "id": "line_1_temperature",
                "tag_group_id": "fast",
                "name": "Line 1 Temperature",
                "address": "mock://line_1_temperature",
                "value_type": "numeric",
                "enabled": True,
            }
        ],
    }


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class ServiceProcess:
    """A running ``--run-service`` child process and its file locations."""

    process: subprocess.Popen[bytes]
    base_url: str
    data_dir: Path
    output_file: Path

    @property
    def pid_file(self) -> Path:
        return self.data_dir / "run" / "plc-data-collector.pid"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "logs" / "plc-data-collector.log"

    def log_text(self) -> str:
        if not self.log_file.exists():
            return ""
        return self.log_file.read_text(encoding="utf-8")

    def output(self) -> str:
        return self.output_file.read_text(encoding="utf-8", errors="replace")

    def wait(self, timeout_s: float) -> int:
        try:
            return self.process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"service did not exit within {timeout_s}s; output:\n{self.output()}"
            ) from None

    def wait_for(self, condition: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(
                    f"service exited with {self.process.returncode} while "
                    f"waiting for {what}; output:\n{self.output()}"
                )
            if condition():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}; output:\n{self.output()}")

    def get(self, path: str) -> httpx.Response | None:
        try:
            return httpx.get(f"{self.base_url}{path}", timeout=1.0)
        except httpx.TransportError:
            return None

    def status_code(self, path: str) -> int | None:
        response = self.get(path)
        return None if response is None else response.status_code


StartService = Callable[..., ServiceProcess]


@pytest.fixture
def start_service(tmp_path: Path) -> Iterator[StartService]:
    """Start service children and always kill them at teardown."""
    children: list[subprocess.Popen[bytes]] = []
    handles: list[object] = []

    def start(
        *,
        with_config: bool = True,
        extra_env: Mapping[str, str] | None = None,
        env_api_port: int | None = None,
    ) -> ServiceProcess:
        """Start a child; with ``env_api_port`` the port comes from ``--api-port``."""
        data_dir = tmp_path / "data"
        port = free_port()
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PLC_DATA_COLLECTOR_")
        }
        env["PLC_DATA_COLLECTOR_API_PORT"] = str(
            port if env_api_port is None else env_api_port
        )
        env.update(extra_env or {})
        args = [
            sys.executable,
            "-m",
            "plc_data_collector",
            "--run-service",
            "--data-dir",
            str(data_dir),
        ]
        if env_api_port is not None:
            args += ["--api-port", str(port)]
        if with_config:
            config_file = tmp_path / "collector.config.json"
            config_file.write_text(json.dumps(mock_config()), encoding="utf-8")
            args += ["--config", str(config_file)]
        output_file = tmp_path / "service-output.txt"
        output = output_file.open("wb")
        handles.append(output)
        process = subprocess.Popen(
            args,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        children.append(process)
        return ServiceProcess(
            process=process,
            base_url=f"http://127.0.0.1:{port}",
            data_dir=data_dir,
            output_file=output_file,
        )

    yield start

    for child in children:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    for handle in handles:
        handle.close()  # type: ignore[attr-defined]


def test_service_serves_live_runtime(start_service: StartService) -> None:
    service = start_service()

    service.wait_for(lambda: service.status_code("/health/ready") == 200, "readiness")

    def worker_running() -> bool:
        response = service.get("/api/runtime/workers")
        if response is None or response.status_code != 200:
            return False
        workers = response.json()["workers"]
        return len(workers) == 1 and workers[0]["state"] == "running"

    service.wait_for(worker_running, "one running worker")
    assert service.pid_file.read_text(encoding="utf-8").strip() == str(
        service.process.pid
    )


def test_service_api_port_flag_wins_over_environment(
    start_service: StartService,
) -> None:
    env_port = free_port()
    service = start_service(env_api_port=env_port)

    service.wait_for(lambda: service.status_code("/health/live") == 200, "liveness")
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{env_port}/health/live", timeout=1.0)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_service_sigterm_shuts_down_gracefully(start_service: StartService) -> None:
    service = start_service()
    service.wait_for(lambda: service.status_code("/health/ready") == 200, "readiness")

    service.process.send_signal(signal.SIGTERM)

    assert service.wait(SHUTDOWN_TIMEOUT_S) == 0, service.output()
    assert not service.pid_file.exists()
    log_text = service.log_text()
    assert "service_stopped" in log_text
    assert "runtime_stopped" in log_text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_service_sigterm_while_waiting_for_database_exits_cleanly(
    start_service: StartService,
) -> None:
    service = start_service(
        extra_env={
            "PLC_DATA_COLLECTOR_DATABASE_URL": UNREACHABLE_DATABASE_URL,
            "PLC_DATA_COLLECTOR_DATABASE_WAIT_TIMEOUT_S": "120",
        }
    )
    # The API is up while the runtime is still waiting for the database.
    service.wait_for(lambda: service.status_code("/health/live") == 200, "liveness")
    service.wait_for(lambda: "runtime_starting" in service.log_text(), "startup")
    assert service.status_code("/health/ready") == 503

    started = time.monotonic()
    service.process.send_signal(signal.SIGTERM)

    assert service.wait(5.0) == 0, service.output()
    assert time.monotonic() - started < 5.0
    assert not service.pid_file.exists()
    log_text = service.log_text()
    assert "runtime_start_aborted" in log_text
    assert "service_start_failed" not in log_text
    assert "service_stopped" in log_text
    assert ":p@" not in log_text
    assert ":p@" not in service.output()


def test_service_starts_without_config_file(start_service: StartService) -> None:
    # Without a seed file and with an empty database the service still starts:
    # configuration is added through the Panel, so readiness reflects the runtime.
    service = start_service(with_config=False)

    service.wait_for(lambda: service.status_code("/health/ready") == 200, "readiness")
    response = service.get("/api/runtime/workers")
    assert response is not None
    assert response.json()["workers"] == []
    log_text = service.log_text()
    assert "configuration_empty" in log_text
    assert "service_start_failed" not in log_text


def test_service_exits_1_when_database_never_answers(
    start_service: StartService,
) -> None:
    service = start_service(
        extra_env={
            "PLC_DATA_COLLECTOR_DATABASE_URL": UNREACHABLE_DATABASE_URL,
            "PLC_DATA_COLLECTOR_DATABASE_WAIT_TIMEOUT_S": "0.5",
        }
    )

    assert service.wait(SHUTDOWN_TIMEOUT_S) == 1, service.output()
    log_text = service.log_text()
    assert "service_start_failed" in log_text
    assert "database_unavailable" in log_text
    assert not service.pid_file.exists()
    assert ":p@" not in log_text
    assert ":p@" not in service.output()
