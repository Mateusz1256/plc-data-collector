"""Dialect-aware SQL helpers shared by persistence modules."""

from __future__ import annotations

from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import dml as postgresql_dml
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import dml as sqlite_dml
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql.schema import Table


def insert_for(
    connection: Connection, table: Table
) -> sqlite_dml.Insert | postgresql_dml.Insert:
    """Return an INSERT that supports ON CONFLICT for the connection's dialect."""
    # SQLite and PostgreSQL expose the same ON CONFLICT API on separate classes.
    if connection.dialect.name == "postgresql":
        return postgresql_insert(table)
    return sqlite_insert(table)
