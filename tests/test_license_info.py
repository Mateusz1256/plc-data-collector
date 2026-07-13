from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from plc_gateway.license_info import load_license_report


def load_generator_module() -> ModuleType:
    """Load the credits generator script as a module for unit tests."""
    script_path = Path(__file__).resolve().parents[1] / "tools" / "generate_credits.py"
    spec = importlib.util.spec_from_file_location("generate_credits", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load generate_credits.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_license_report_loads_bundled_credits() -> None:
    report = load_license_report()

    assert report.project.spdx_id == "LicenseRef-PLC-Gateway-Proprietary"
    assert report.build.version == "0.0.0"
    assert any(dependency.name == "asyncua" for dependency in report.dependencies)
    assert all(dependency.license for dependency in report.dependencies)


def test_credits_generator_fails_when_runtime_notice_is_missing() -> None:
    generator = load_generator_module()

    with pytest.raises(RuntimeError, match="Missing verified runtime notices"):
        generator.build_credits(
            {"missing-package": "1.0.0"},
            {},
        )


def test_credits_generator_fails_on_version_mismatch() -> None:
    generator = load_generator_module()
    notices = {
        "example": generator.NoticeEntry(
            name="example",
            version="1.0.0",
            purpose="test",
            license="MIT",
            source="https://example.invalid",
            copyright_notice="test",
            bundled_in_distribution=True,
            notes="test",
        )
    }

    with pytest.raises(RuntimeError, match="Version mismatch"):
        generator.build_credits({"example": "2.0.0"}, notices)
