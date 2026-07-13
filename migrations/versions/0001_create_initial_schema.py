"""Create initial relational schema.

Revision ID: 0001
Revises:
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from plc_gateway.persistence.schema import metadata

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create persistence tables."""
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    """Drop persistence tables."""
    metadata.drop_all(op.get_bind())
