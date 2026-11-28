"""M-Bus byte fixtures: hex text files in tests/fixtures/mbus/.

Each file names its source in a comment; ``#`` starts a comment, whitespace
between bytes is ignored.
"""

from __future__ import annotations

from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mbus"


def fixture_names() -> list[str]:
    """All fixture file names, sorted."""
    return sorted(path.name for path in FIXTURE_DIR.glob("*.hex"))


def read_frame(name: str) -> bytes:
    """The bytes of one fixture file."""
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    hex_digits = "".join(line.split("#", 1)[0] for line in text.splitlines())
    return bytes.fromhex(hex_digits)


def source_of(name: str) -> str:
    """The fixture's ``# Source:`` line (every fixture must have one)."""
    for line in (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines():
        if line.startswith("# Source:"):
            return line.removeprefix("# Source:").strip()
    raise AssertionError(f"{name} names no source")
