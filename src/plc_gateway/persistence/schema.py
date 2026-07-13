"""Relational schema definition for PLC Collector persistence."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

from plc_gateway.persistence.types import UTCDateTime

metadata = MetaData()

connections = Table(
    "connections",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("protocol", String(64), nullable=False),
    Column("endpoint", String(1024), nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("timeout_ms", Integer, nullable=False),
    Column("protocol_options", JSON, nullable=False, default=dict),
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
    Column("error_message", String(2048), nullable=True),
    CheckConstraint("requested_tags >= 0", name="ck_poll_requested_non_negative"),
    CheckConstraint("successful_tags >= 0", name="ck_poll_success_non_negative"),
    CheckConstraint("failed_tags >= 0", name="ck_poll_failed_non_negative"),
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
    Column("integer_value", Integer, nullable=True),
    Column("boolean_value", Boolean, nullable=True),
    Column("text_value", String(4096), nullable=True),
    Column("binary_value", LargeBinary, nullable=True),
    Column("error_code", String(128), nullable=True),
    Column("error_message", String(2048), nullable=True),
    UniqueConstraint("event_id", name="uq_tag_readings_event_id"),
)

runtime_components = Table(
    "runtime_components",
    metadata,
    Column("component_id", String(256), primary_key=True),
    Column("state", String(32), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("message", String(1024), nullable=True),
    Column("error_code", String(128), nullable=True),
    Column("error_message", String(2048), nullable=True),
)
