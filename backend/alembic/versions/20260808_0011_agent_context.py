"""保存每次 Agent Run 的上下文解析快照。

Revision ID: 20260808_0011
Revises: 20260808_0010
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260808_0011"
down_revision = "20260808_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs", sa.Column("context_snapshot", sa.JSON(), nullable=False, server_default="{}")
    )
    op.add_column(
        "agent_runs", sa.Column("context_version", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("agent_runs", sa.Column("resolved_query", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("reference_confidence", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "reference_confidence")
    op.drop_column("agent_runs", "resolved_query")
    op.drop_column("agent_runs", "context_version")
    op.drop_column("agent_runs", "context_snapshot")
