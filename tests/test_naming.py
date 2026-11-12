from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["src", "tests", "tools", "docs/deployment"]
SCAN_FILES = ["README.md", "pyproject.toml", "alembic.ini", ".env.example"]
LEGACY = re.compile(
    r"(?i:plc_gateway|plc-gateway|plcgateway|Gateway-Proprietary)"
    r"|GatewayConfig|GatewayError"
    r"|PLC Collector(?! contributors)"
)
SKIP_PARTS = {"__pycache__", ".mypy_cache", ".ruff_cache"}


def _files() -> list[Path]:
    found: list[Path] = []
    for directory in SCAN_DIRS:
        for path in (ROOT / directory).rglob("*"):
            if (
                path.is_file()
                and not SKIP_PARTS & set(path.parts)
                and not any(part.endswith(".egg-info") for part in path.parts)
            ):
                found.append(path)
    found.extend(ROOT / name for name in SCAN_FILES if (ROOT / name).is_file())
    return [p for p in found if p != Path(__file__).resolve()]


def test_no_legacy_names_in_sources() -> None:
    offenders: list[str] = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if LEGACY.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, sorted(offenders)
