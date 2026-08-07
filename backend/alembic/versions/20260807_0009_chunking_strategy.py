"""记录论文当前索引的切分策略。

Revision ID: 20260807_0009
Revises: 20260806_0008
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260807_0009"
down_revision = "20260806_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("papers")}
    if "chunking_strategy" not in columns:
        op.add_column(
            "papers",
            sa.Column(
                "chunking_strategy",
                sa.String(length=48),
                nullable=False,
                server_default="fixed_window_v1",
            ),
        )


def downgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("papers")}
    if "chunking_strategy" in columns:
        op.drop_column("papers", "chunking_strategy")
