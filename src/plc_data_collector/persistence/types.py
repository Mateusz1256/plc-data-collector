"""SQLAlchemy persistence column types."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import String, TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Store timezone-aware datetimes as UTC ISO-8601 strings."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> str | None:
        """Normalize datetime values to UTC before storage."""
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime values must be timezone-aware.")
        return value.astimezone(UTC).isoformat()

    def process_result_value(
        self,
        value: Any,
        dialect: Dialect,
    ) -> datetime | None:
        """Load stored UTC ISO-8601 strings as aware datetimes."""
        del dialect
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("UTCDateTime storage value must be a string.")
        return datetime.fromisoformat(value).astimezone(UTC)
