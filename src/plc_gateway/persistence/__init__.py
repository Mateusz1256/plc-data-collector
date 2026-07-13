"""Persistence adapters for relational storage."""

from plc_gateway.persistence.database import create_sqlite_engine, initialize_schema
from plc_gateway.persistence.records import TagReadingRecord
from plc_gateway.persistence.repositories import (
    ConfigurationRepository,
    ReadingRepository,
    RuntimeStatusRepository,
)
from plc_gateway.persistence.schema import metadata
from plc_gateway.persistence.spool import DurableSqliteSpool, SpoolEntry, SpoolMetrics
from plc_gateway.persistence.writer import (
    DatabaseWriter,
    DatabaseWriterMetrics,
    DatabaseWriterShutdown,
)

__all__ = [
    "ConfigurationRepository",
    "DatabaseWriter",
    "DatabaseWriterMetrics",
    "DatabaseWriterShutdown",
    "DurableSqliteSpool",
    "ReadingRepository",
    "RuntimeStatusRepository",
    "SpoolEntry",
    "SpoolMetrics",
    "TagReadingRecord",
    "create_sqlite_engine",
    "initialize_schema",
    "metadata",
]
