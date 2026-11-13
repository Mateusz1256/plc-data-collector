"""Add database configuration columns, application settings and API keys.

Connections gain a display ``name`` (backfilled from ``id``), encrypted
``secret_options`` and ``archived_at``; Tag Groups and Tags gain
``archived_at``. ``app_settings`` and ``api_keys`` are new, and three indexes
support history queries and retention.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from plc_data_collector.persistence.types import UTCDateTime

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add configuration columns, new tables and indexes."""
    with op.batch_alter_table("connections") as batch:
        batch.add_column(sa.Column("name", sa.String(256), nullable=True))
        batch.add_column(sa.Column("secret_options", sa.Text(), nullable=True))
        batch.add_column(sa.Column("archived_at", UTCDateTime(), nullable=True))
    op.execute(sa.text("UPDATE connections SET name = id"))
    with op.batch_alter_table("connections") as batch:
        batch.alter_column("name", existing_type=sa.String(256), nullable=False)
    for table in ("tag_groups", "tags"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("archived_at", UTCDateTime(), nullable=True))

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_used_at", UTCDateTime(), nullable=True),
        sa.Column("revoked_at", UTCDateTime(), nullable=True),
    )

    op.create_index("ix_tag_readings_received_at", "tag_readings", ["received_at"])
    op.create_index(
        "ix_tag_readings_tag_id_received_at",
        "tag_readings",
        ["tag_id", "received_at"],
    )
    op.create_index("ix_poll_executions_started_at", "poll_executions", ["started_at"])


def downgrade() -> None:
    """Remove everything added by upgrade (API keys and settings are lost)."""
    op.drop_index("ix_poll_executions_started_at", table_name="poll_executions")
    op.drop_index("ix_tag_readings_tag_id_received_at", table_name="tag_readings")
    op.drop_index("ix_tag_readings_received_at", table_name="tag_readings")
    op.drop_table("api_keys")
    op.drop_table("app_settings")
    for table in ("tags", "tag_groups"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("archived_at")
    with op.batch_alter_table("connections") as batch:
        batch.drop_column("archived_at")
        batch.drop_column("secret_options")
        batch.drop_column("name")
