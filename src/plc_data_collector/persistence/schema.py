"""Relational schema definition for PLC Data Collector persistence."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from plc_data_collector.persistence.types import UTCDateTime

metadata = MetaData()

connections = Table(
    "connections",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("name", String(256), nullable=False),
    Column("protocol", String(64), nullable=False),
    Column("endpoint", String(1024), nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("timeout_ms", Integer, nullable=False),
    Column("protocol_options", JSON, nullable=False, default=dict),
    Column("secret_options", Text, nullable=True),
    Column("archived_at", UTCDateTime(), nullable=True),
    CheckConstraint("timeout_ms > 0", name="ck_connections_timeout_positive"),
)

tag_groups = Table(
    "tag_groups",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("connection_id", ForeignKey("connections.id"), nullable=False),
    Column("interval_ms", Integer, nullable=False),
    Column("timeout_ms", Integer, nullable=False),
    Column("overlap_policy", String(32), nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("archived_at", UTCDateTime(), nullable=True),
    CheckConstraint("interval_ms > 0", name="ck_tag_groups_interval_positive"),
    CheckConstraint("timeout_ms > 0", name="ck_tag_groups_timeout_positive"),
)

tags = Table(
    "tags",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("tag_group_id", ForeignKey("tag_groups.id"), nullable=False),
    Column("name", String(256), nullable=False),
    Column("address", String(1024), nullable=False),
    Column("value_type", String(32), nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("metadata_json", JSON, nullable=False, default=dict),
    Column("archived_at", UTCDateTime(), nullable=True),
)

poll_executions = Table(
    "poll_executions",
    metadata,
    Column("execution_id", String(64), primary_key=True),
    Column("connection_id", ForeignKey("connections.id"), nullable=False),
    Column("tag_group_id", ForeignKey("tag_groups.id"), nullable=False),
    Column("status", String(32), nullable=False),
    Column("started_at", UTCDateTime(), nullable=False),
    Column("finished_at", UTCDateTime(), nullable=True),
    Column("requested_tags", Integer, nullable=False),
    Column("successful_tags", Integer, nullable=False),
    Column("failed_tags", Integer, nullable=False),
    Column("error_message", Text, nullable=True),
    CheckConstraint("requested_tags >= 0", name="ck_poll_requested_non_negative"),
    CheckConstraint("successful_tags >= 0", name="ck_poll_success_non_negative"),
    CheckConstraint("failed_tags >= 0", name="ck_poll_failed_non_negative"),
    Index("ix_poll_executions_started_at", "started_at"),
)

tag_readings = Table(
    "tag_readings",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("connection_id", ForeignKey("connections.id"), nullable=False),
    Column("tag_group_id", ForeignKey("tag_groups.id"), nullable=False),
    Column("tag_id", ForeignKey("tags.id"), nullable=False),
    Column("source_timestamp", UTCDateTime(), nullable=True),
    Column("received_at", UTCDateTime(), nullable=False),
    Column("quality", String(32), nullable=False),
    Column("value_type", String(32), nullable=False),
    Column("numeric_value", Float, nullable=True),
    Column("integer_value", BigInteger, nullable=True),
    Column("boolean_value", Boolean, nullable=True),
    Column("text_value", Text, nullable=True),
    Column("binary_value", LargeBinary, nullable=True),
    Column("error_code", String(128), nullable=True),
    Column("error_message", Text, nullable=True),
    UniqueConstraint("event_id", name="uq_tag_readings_event_id"),
    Index("ix_tag_readings_received_at", "received_at"),
    Index("ix_tag_readings_tag_id_received_at", "tag_id", "received_at"),
)

runtime_components = Table(
    "runtime_components",
    metadata,
    Column("component_id", String(256), primary_key=True),
    Column("state", String(32), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("message", String(1024), nullable=True),
    Column("error_code", String(128), nullable=True),
    Column("error_message", Text, nullable=True),
)

app_settings = Table(
    "app_settings",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", JSON, nullable=False),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("prefix", String(16), nullable=False),
    Column("key_hash", String(64), nullable=False, unique=True),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("last_used_at", UTCDateTime(), nullable=True),
    Column("revoked_at", UTCDateTime(), nullable=True),
)
