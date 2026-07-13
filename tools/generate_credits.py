"""Generate verified third-party credits from installed package metadata."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYPROJECT = PROJECT_ROOT / "pyproject.toml"
DEFAULT_NOTICES = PROJECT_ROOT / "THIRD_PARTY_NOTICES.md"
DEFAULT_OUTPUT = PROJECT_ROOT / "src" / "plc_gateway" / "license_data" / "credits.json"


@dataclass(frozen=True, slots=True)
class NoticeEntry:
    """Verified dependency notice parsed from THIRD_PARTY_NOTICES.md."""

    name: str
    version: str
    purpose: str
    license: str
    source: str
    copyright_notice: str
    bundled_in_distribution: bool
    notes: str


def main() -> int:
    """Run the credits generator."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT)
    parser.add_argument("--notices", type=Path, default=DEFAULT_NOTICES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    direct_dependencies = read_direct_runtime_dependencies(args.pyproject)
    runtime_dependencies = collect_runtime_dependency_closure(direct_dependencies)
    notices = parse_runtime_notices(args.notices)
    credits = build_credits(runtime_dependencies, notices)
    write_credits(args.output, credits)
    return 0


def read_direct_runtime_dependencies(pyproject_path: Path) -> set[str]:
    """Read direct runtime dependencies from pyproject.toml."""
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    dependencies = data["project"]["dependencies"]
    if not isinstance(dependencies, list):
        raise RuntimeError("project.dependencies must be a list.")
    return {_normalize_name(_require_dependency_name(item)) for item in dependencies}


def collect_runtime_dependency_closure(direct_dependencies: set[str]) -> dict[str, str]:
    """Return installed runtime dependency closure as normalized name -> version."""
    resolved: dict[str, str] = {}
    pending = list(sorted(direct_dependencies))
    while pending:
        normalized_name = pending.pop()
        if normalized_name in resolved:
            continue
        distribution = metadata.distribution(normalized_name)
        resolved[normalized_name] = distribution.version
        for requirement in distribution.requires or ():
            parsed_requirement = Requirement(requirement)
            if _skip_requirement(parsed_requirement):
                continue
            dependency_normalized = _normalize_name(parsed_requirement.name)
            if dependency_normalized not in resolved:
                pending.append(dependency_normalized)
    return resolved


def parse_runtime_notices(notices_path: Path) -> dict[str, NoticeEntry]:
    """Parse runtime dependency entries from THIRD_PARTY_NOTICES.md."""
    text = notices_path.read_text(encoding="utf-8")
    runtime_section = _between(
        text,
        "## Runtime Dependencies",
        "## Development Dependencies",
    )
    entries: dict[str, NoticeEntry] = {}
    for block in re.split(r"(?m)^### ", runtime_section):
        stripped = block.strip()
        if not stripped or stripped.startswith("The following packages"):
            continue
        lines = stripped.splitlines()
        name = lines[0].strip()
        fields = _parse_notice_fields(lines[1:])
        entry = NoticeEntry(
            name=name,
            version=_required_field(fields, "Version"),
            purpose=_required_field(fields, "Purpose"),
            license=_required_field(fields, "License"),
            source=_required_field(fields, "Source"),
            copyright_notice=_required_field(fields, "Copyright notice"),
            bundled_in_distribution=_parse_yes_no(
                _required_field(fields, "Bundled in distribution")
            ),
            notes=_required_field(fields, "Notes"),
        )
        entries[_normalize_name(name)] = entry
    return entries


def build_credits(
    runtime_dependencies: dict[str, str],
    notices: dict[str, NoticeEntry],
) -> dict[str, object]:
    """Validate notices and build JSON-serializable credits."""
    missing = sorted(set(runtime_dependencies) - set(notices))
    if missing:
        raise RuntimeError(f"Missing verified runtime notices: {', '.join(missing)}")

    credits: list[dict[str, object]] = []
    for normalized_name in sorted(runtime_dependencies):
        entry = notices[normalized_name]
        installed_version = runtime_dependencies[normalized_name]
        if entry.version != installed_version:
            raise RuntimeError(
                f"Version mismatch for {entry.name}: notices={entry.version}, "
                f"installed={installed_version}"
            )
        if not entry.license.strip():
            raise RuntimeError(f"Missing verified license for {entry.name}.")
        credits.append(asdict(entry))

    return {
        "schema_version": 1,
        "generated_from": "THIRD_PARTY_NOTICES.md",
        "dependencies": credits,
    }


def write_credits(output_path: Path, credits: dict[str, object]) -> None:
    """Write generated credits JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(credits, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parse_notice_fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in lines:
        if line.startswith("- "):
            raw_key, _, raw_value = line[2:].partition(":")
            current_key = raw_key.strip()
            fields[current_key] = raw_value.strip()
        elif current_key is not None and line.startswith("  "):
            fields[current_key] = f"{fields[current_key]} {line.strip()}".strip()
    return fields


def _between(text: str, start: str, end: str) -> str:
    try:
        after_start = text.split(start, 1)[1]
        return after_start.split(end, 1)[0]
    except IndexError as error:
        msg = f"Could not find section between {start} and {end}."
        raise RuntimeError(msg) from error


def _required_field(fields: dict[str, str], key: str) -> str:
    value = fields.get(key)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing notice field: {key}")
    return value


def _parse_yes_no(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "yes":
        return True
    if normalized == "no":
        return False
    raise RuntimeError(f"Expected yes/no value, got {value!r}.")


def _skip_requirement(requirement: Requirement) -> bool:
    if requirement.marker is None:
        return False
    return not requirement.marker.evaluate()


def _require_dependency_name(requirement: object) -> str:
    if not isinstance(requirement, str):
        raise RuntimeError(f"Invalid dependency requirement: {requirement!r}")
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    if match is None:
        raise RuntimeError(f"Could not parse dependency requirement: {requirement}")
    return match.group(1)


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


if __name__ == "__main__":
    raise SystemExit(main())
