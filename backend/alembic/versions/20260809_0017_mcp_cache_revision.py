"""add MCP cache namespace revision

Revision ID: 20260809_0017
Revises: 20260809_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0017"
down_revision: str | None = "20260809_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_server_configs",
        sa.Column("cache_revision", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("mcp_server_configs", "cache_revision")
