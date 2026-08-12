"""为 Agent Run 冻结研究编排版本。

Revision ID: 20260812_0019
Revises: 20260810_0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260812_0019"
down_revision = "20260810_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {item["name"] for item in inspect(connection).get_columns("agent_runs")}
    if "orchestration_version" not in columns:
        op.add_column(
            "agent_runs",
            sa.Column(
                "orchestration_version",
                sa.String(length=64),
                nullable=False,
                server_default="single_agent_v1",
            ),
        )


def downgrade() -> None:
    connection = op.get_bind()
    columns = {item["name"] for item in inspect(connection).get_columns("agent_runs")}
    if "orchestration_version" in columns:
        op.drop_column("agent_runs", "orchestration_version")
