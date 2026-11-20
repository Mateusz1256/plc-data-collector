"""Add protocol-specific options to tags.

Tags gain a JSON ``protocol_options`` column (not null, default ``{}``) that
carries per-Tag protocol settings such as a Modbus data type.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``tags.protocol_options``; existing Tags get ``{}``."""
    with op.batch_alter_table("tags") as batch:
        batch.add_column(
            sa.Column(
                "protocol_options",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )


def downgrade() -> None:
    """Drop ``tags.protocol_options`` (per-Tag protocol settings are lost)."""
    with op.batch_alter_table("tags") as batch:
        batch.drop_column("protocol_options")
