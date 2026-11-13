"""Persistence adapters for relational storage."""

from plc_data_collector.persistence.app_settings import (
    AppSettingsStore,
    RetentionSettings,
)
from plc_data_collector.persistence.config_store import ConfigStore
from plc_data_collector.persistence.database import (
    create_database_engine,
    create_sqlite_engine,
    initialize_schema,
    wait_for_database,
)
from plc_data_collector.persistence.migrate import run_migrations
from plc_data_collector.persistence.records import TagReadingRecord
from plc_data_collector.persistence.repositories import (
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
    "AppSettingsStore",
    "ConfigStore",
    "DatabaseWriter",
    "DatabaseWriterMetrics",
    "DatabaseWriterShutdown",
    "DurableSqliteSpool",
    "ReadingRepository",
    "RetentionSettings",
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
