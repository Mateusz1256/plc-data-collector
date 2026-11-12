"""Key-value application settings stored in the database."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, Engine, select

from plc_data_collector.domain import ConfigurationError
from plc_data_collector.persistence.schema import app_settings
from plc_data_collector.persistence.sql import insert_for

CONFIG_REVISION_KEY = "config_revision"
RETENTION_KEY = "retention"


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    """Retention of stored readings."""

    enabled: bool = False
    days: int = 30

    def __post_init__(self) -> None:
        """Validate that days is within 1..3650."""
        if isinstance(self.days, bool) or not 1 <= self.days <= 3650:
            raise ConfigurationError(
                "Retention days must be between 1 and 3650.",
                code="invalid_retention_days",
            )


class AppSettingsStore:
    """Read and write application settings."""

    def __init__(self, engine: Engine) -> None:
        """Create a store."""
        self._engine = engine

    def get(self, key: str) -> object | None:
        """Return the stored value, or None when the key is unset."""
        with self._engine.connect() as connection:
            return get_setting(connection, key)

    def set(
        self, key: str, value: object, *, connection: Connection | None = None
    ) -> None:
        """Store a value, inside the given transaction when one is passed."""
        if connection is not None:
            set_setting(connection, key, value)
            return
        with self._engine.begin() as own:
            set_setting(own, key, value)

    def retention(self) -> RetentionSettings:
        """Return the retention settings (defaults when unset)."""
        stored = self.get(RETENTION_KEY)
        if not isinstance(stored, dict):
            return RetentionSettings()
        return RetentionSettings(
            enabled=bool(stored.get("enabled", False)),
            days=int(stored.get("days", 30)),
        )

    def set_retention(self, settings: RetentionSettings) -> None:
        """Store the retention settings."""
        self.set(RETENTION_KEY, {"enabled": settings.enabled, "days": settings.days})


def get_setting(connection: Connection, key: str) -> object | None:
    """Read one setting within an open connection."""
    row = connection.execute(
        select(app_settings.c.value).where(app_settings.c.key == key)
    ).first()
    return None if row is None else row[0]


def set_setting(connection: Connection, key: str, value: object) -> None:
    """Upsert one setting within an open transaction."""
    statement = insert_for(connection, app_settings).values(key=key, value=value)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[app_settings.c.key],
            set_={"value": statement.excluded.value},
        )
    )
