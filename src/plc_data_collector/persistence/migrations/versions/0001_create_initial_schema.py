"""Create initial relational schema.

The 0.1.0 tables are spelled out here instead of read from the live metadata,
so later revisions can add columns and tables without this one creating them
first.

Revision ID: 0001
Revises:
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from plc_data_collector.persistence.types import UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create persistence tables."""
    op.create_table(
        "connections",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("protocol", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(1024), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("timeout_ms", sa.Integer(), nullable=False),
        sa.Column("protocol_options", sa.JSON(), nullable=False),
        sa.CheckConstraint("timeout_ms > 0", name="ck_connections_timeout_positive"),
    )
    op.create_table(
        "tag_groups",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column(
            "connection_id",
            sa.String(128),
            sa.ForeignKey("connections.id"),
            nullable=False,
        ),
        sa.Column("interval_ms", sa.Integer(), nullable=False),
        sa.Column("timeout_ms", sa.Integer(), nullable=False),
        sa.Column("overlap_policy", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint("interval_ms > 0", name="ck_tag_groups_interval_positive"),
        sa.CheckConstraint("timeout_ms > 0", name="ck_tag_groups_timeout_positive"),
    )
    op.create_table(
        "tags",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column(
            "tag_group_id",
            sa.String(128),
            sa.ForeignKey("tag_groups.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("address", sa.String(1024), nullable=False),
        sa.Column("value_type", sa.String(32), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "poll_executions",
        sa.Column("execution_id", sa.String(64), primary_key=True),
        sa.Column(
            "connection_id",
            sa.String(128),
            sa.ForeignKey("connections.id"),
            nullable=False,
        ),
        sa.Column(
            "tag_group_id",
            sa.String(128),
            sa.ForeignKey("tag_groups.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("requested_tags", sa.Integer(), nullable=False),
        sa.Column("successful_tags", sa.Integer(), nullable=False),
        sa.Column("failed_tags", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.String(2048), nullable=True),
        sa.CheckConstraint(
            "requested_tags >= 0", name="ck_poll_requested_non_negative"
        ),
        sa.CheckConstraint("successful_tags >= 0", name="ck_poll_success_non_negative"),
        sa.CheckConstraint("failed_tags >= 0", name="ck_poll_failed_non_negative"),
    )
    op.create_table(
        "tag_readings",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column(
            "connection_id",
            sa.String(128),
            sa.ForeignKey("connections.id"),
            nullable=False,
        ),
        sa.Column(
            "tag_group_id",
            sa.String(128),
            sa.ForeignKey("tag_groups.id"),
            nullable=False,
        ),
        sa.Column("tag_id", sa.String(128), sa.ForeignKey("tags.id"), nullable=False),
        sa.Column("source_timestamp", UTCDateTime(), nullable=True),
        sa.Column("received_at", UTCDateTime(), nullable=False),
        sa.Column("quality", sa.String(32), nullable=False),
        sa.Column("value_type", sa.String(32), nullable=False),
        sa.Column("numeric_value", sa.Float(), nullable=True),
        sa.Column("integer_value", sa.Integer(), nullable=True),
        sa.Column("boolean_value", sa.Boolean(), nullable=True),
        sa.Column("text_value", sa.String(4096), nullable=True),
        sa.Column("binary_value", sa.LargeBinary(), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_message", sa.String(2048), nullable=True),
        sa.UniqueConstraint("event_id", name="uq_tag_readings_event_id"),
    )
    op.create_table(
        "runtime_components",
        sa.Column("component_id", sa.String(256), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("message", sa.String(1024), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_message", sa.String(2048), nullable=True),
    )


def downgrade() -> None:
    """Drop persistence tables."""
    for table in (
        "runtime_components",
        "tag_readings",
        "poll_executions",
        "tags",
        "tag_groups",
        "connections",
    ):
        op.drop_table(table)
