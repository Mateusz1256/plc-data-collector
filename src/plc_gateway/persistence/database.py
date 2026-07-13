"""Database engine helpers for persistence adapters."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import Engine, create_engine, event

from plc_gateway.persistence.schema import metadata


class _CursorLike(Protocol):
    def execute(self, statement: str) -> object:
        """Execute a DB-API statement."""
        ...

    def close(self) -> None:
        """Close the cursor."""
        ...


class _ConnectionLike(Protocol):
    def cursor(self) -> _CursorLike:
        """Return a DB-API cursor."""
        ...


def create_sqlite_engine(database_url: str = "sqlite:///plc_gateway.db") -> Engine:
    """Create a SQLite SQLAlchemy engine with foreign keys enabled."""
    engine = create_engine(database_url, future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(
        dbapi_connection: _ConnectionLike,
        connection_record: object,
    ) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def initialize_schema(engine: Engine) -> None:
    """Create all persistence tables on the given engine."""
    metadata.create_all(engine)
