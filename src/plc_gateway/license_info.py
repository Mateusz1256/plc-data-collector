"""Project license and third-party credits helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from importlib import resources

from plc_gateway.build_info import BuildInfo, get_build_info

PROJECT_LICENSE_STATUS = "selected"
PROJECT_LICENSE_NAME = "PLC Collector Proprietary License Notice"
PROJECT_LICENSE_SPDX = "LicenseRef-PLC-Gateway-Proprietary"
PROJECT_LICENSE_FILE = "LICENSE"
THIRD_PARTY_NOTICES_FILE = "THIRD_PARTY_NOTICES.md"


@dataclass(frozen=True, slots=True)
class ProjectLicense:
    """Project license metadata."""

    status: str
    name: str
    spdx_id: str
    license_file: str


@dataclass(frozen=True, slots=True)
class DependencyCredit:
    """Verified third-party dependency credit."""

    name: str
    version: str
    purpose: str
    license: str
    source: str
    copyright_notice: str
    bundled_in_distribution: bool
    notes: str


@dataclass(frozen=True, slots=True)
class LicenseReport:
    """Complete license report returned by CLI and API."""

    project: ProjectLicense
    build: BuildInfo
    third_party_notices_file: str
    dependencies: tuple[DependencyCredit, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable report."""
        return {
            "project": asdict(self.project),
            "build": asdict(self.build),
            "third_party_notices_file": self.third_party_notices_file,
            "dependencies": [asdict(dependency) for dependency in self.dependencies],
        }


def get_project_license() -> ProjectLicense:
    """Return project license metadata."""
    return ProjectLicense(
        status=PROJECT_LICENSE_STATUS,
        name=PROJECT_LICENSE_NAME,
        spdx_id=PROJECT_LICENSE_SPDX,
        license_file=PROJECT_LICENSE_FILE,
    )


def load_license_report() -> LicenseReport:
    """Load generated third-party credits bundled with the package."""
    credits = _load_credits_json()
    dependencies_data = credits.get("dependencies")
    if not isinstance(dependencies_data, list):
        raise RuntimeError("credits.json dependencies must contain a list.")

    dependencies: list[DependencyCredit] = []
    for item in dependencies_data:
        if not isinstance(item, dict):
            raise RuntimeError("credits.json dependency entries must be objects.")
        dependencies.append(
            DependencyCredit(
                name=str(item["name"]),
                version=str(item["version"]),
                purpose=str(item["purpose"]),
                license=str(item["license"]),
                source=str(item["source"]),
                copyright_notice=str(item["copyright_notice"]),
                bundled_in_distribution=bool(item["bundled_in_distribution"]),
                notes=str(item["notes"]),
            )
        )

    return LicenseReport(
        project=get_project_license(),
        build=get_build_info(),
        third_party_notices_file=THIRD_PARTY_NOTICES_FILE,
        dependencies=tuple(dependencies),
    )


def _load_credits_json() -> dict[str, object]:
    data = (
        resources.files("plc_gateway.license_data")
        .joinpath("credits.json")
        .read_text(encoding="utf-8")
    )
    loaded = json.loads(data)
    if not isinstance(loaded, dict):
        raise RuntimeError("credits.json must contain an object.")
    return loaded
