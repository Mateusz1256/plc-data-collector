"""Widen value and error columns that PostgreSQL enforces.

``integer_value`` becomes BIGINT so 64-bit counters fit, and the free-text
value and error columns become unbounded TEXT. SQLite never enforced these
limits; on SQLite the tables are rebuilt in batch mode so the declared types
still match the schema.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen integer, text and error columns."""
    with op.batch_alter_table("tag_readings") as batch:
        batch.alter_column(
            "integer_value",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )
        batch.alter_column(
            "text_value",
            existing_type=sa.String(4096),
            type_=sa.Text(),
            existing_nullable=True,
        )
        batch.alter_column(
            "error_message",
            existing_type=sa.String(2048),
            type_=sa.Text(),
            existing_nullable=True,
        )
    for table in ("poll_executions", "runtime_components"):
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "error_message",
                existing_type=sa.String(2048),
                type_=sa.Text(),
                existing_nullable=True,
            )


def downgrade() -> None:
    """Restore the 0.1.0 column types (fails if stored values do not fit)."""
    for table in ("runtime_components", "poll_executions"):
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "error_message",
                existing_type=sa.Text(),
                type_=sa.String(2048),
                existing_nullable=True,
            )
    with op.batch_alter_table("tag_readings") as batch:
        batch.alter_column(
            "error_message",
            existing_type=sa.Text(),
            type_=sa.String(2048),
            existing_nullable=True,
        )
        batch.alter_column(
            "text_value",
            existing_type=sa.Text(),
            type_=sa.String(4096),
            existing_nullable=True,
        )
        batch.alter_column(
            "integer_value",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
