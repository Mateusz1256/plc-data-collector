"""Database engine helpers for persistence adapters."""

from __future__ import annotations

import time
from typing import Protocol

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url

from plc_data_collector.domain import ConfigurationError, StorageError
from plc_data_collector.persistence.schema import metadata

# pg8000 waits forever on a black-holed host by default. This bounds every
# socket operation (connect, read, write); keep it above storage_timeout_s.
PG_SOCKET_TIMEOUT_S = 10.0


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


def create_database_engine(database_url: str) -> Engine:
    """Create an engine for a supported database URL (SQLite or PostgreSQL)."""
    try:
        url = make_url(database_url)
    except Exception:
        raise ConfigurationError(
            "Database URL is not valid.",
            code="unsupported_database_url",
        ) from None
    backend = url.get_backend_name()
    driver = url.get_driver_name()
    if backend == "sqlite":
        return _create_sqlite_engine(database_url)
    if backend == "postgresql" and driver == "pg8000":
        return create_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
            connect_args={"timeout": PG_SOCKET_TIMEOUT_S},
        )
    raise ConfigurationError(
        f"Unsupported database URL scheme {url.drivername!r}; "
        "use sqlite or postgresql+pg8000.",
        code="unsupported_database_url",
        details={"scheme": url.drivername},
    )


def create_sqlite_engine(
    database_url: str = "sqlite:///plc_data_collector.db",
) -> Engine:
    """Create a SQLite SQLAlchemy engine with foreign keys enabled."""
    return _create_sqlite_engine(database_url)


def _create_sqlite_engine(database_url: str) -> Engine:
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


def wait_for_database(
    engine: Engine,
    *,
    timeout_s: float,
    interval_s: float = 1.0,
) -> None:
    """Block until the database answers ``SELECT 1`` or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except Exception as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StorageError(
                    f"Database did not become available within {timeout_s:g}s.",
                    code="database_unavailable",
                    details={"error_type": type(error).__name__},
                ) from None
            time.sleep(min(interval_s, remaining))
