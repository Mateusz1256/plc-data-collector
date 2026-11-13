"""Database stores the Panel and the data API use, built over one engine."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine

from plc_data_collector.app.secrets import SecretBox
from plc_data_collector.persistence import AppSettingsStore, ConfigStore


@dataclass(frozen=True, slots=True)
class Stores:
    """Stores over the supervisor's database engine."""

    config: ConfigStore
    settings: AppSettingsStore


def build_stores(engine: Engine, secret_box: SecretBox) -> Stores:
    """Create every store over a migrated database engine."""
    return Stores(
        config=ConfigStore(engine, secret_box),
        settings=AppSettingsStore(engine),
    )
