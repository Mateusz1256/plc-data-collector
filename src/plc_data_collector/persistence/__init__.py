"""Persistence adapters for relational storage."""

from plc_data_collector.persistence.database import (
    create_database_engine,
    create_sqlite_engine,
    initialize_schema,
    wait_for_database,
)
from plc_data_collector.persistence.migrate import run_migrations
from plc_data_collector.persistence.records import TagReadingRecord
from plc_data_collector.persistence.repositories import (
    ConfigurationRepository,
    ReadingRepository,
    RuntimeStatusRepository,
)
from plc_data_collector.persistence.schema import metadata
from plc_data_collector.persistence.spool import (
    DurableSqliteSpool,
    SpoolEntry,
    SpoolMetrics,
)
from plc_data_collector.persistence.writer import (
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
    "create_database_engine",
    "create_sqlite_engine",
    "initialize_schema",
    "metadata",
    "run_migrations",
    "wait_for_database",
]
