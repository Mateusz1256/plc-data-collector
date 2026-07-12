from __future__ import annotations

import json

import pytest

import plc_gateway
from plc_gateway.__main__ import main
from plc_gateway.app.health import build_health_status
from plc_gateway.app.logging import configure_logging


def test_package_exposes_version() -> None:
    assert plc_gateway.__version__ == "0.0.0"
    assert plc_gateway.get_version() == "0.0.0"


def test_health_status_contains_application_version() -> None:
    assert build_health_status() == {
        "application": "plc-gateway",
        "status": "ok",
        "version": "0.0.0",
    }


def test_main_prints_health_status(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == build_health_status()


def test_main_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == "0.0.0"


def test_configure_logging_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="Unsupported log level"):
        configure_logging("definitely-not-a-level")


@pytest.mark.asyncio
async def test_asyncio_test_support_is_configured() -> None:
    assert build_health_status()["status"] == "ok"
