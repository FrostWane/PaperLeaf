"""persist selected skill and harness trace

Revision ID: 20260808_0013
Revises: 20260808_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0013"
down_revision: str | None = "20260808_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("selected_skill", sa.String(length=64)))
    op.add_column("agent_runs", sa.Column("skill_version", sa.Integer()))
    op.add_column(
        "agent_runs",
        sa.Column(
            "harness_trace",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "harness_trace")
    op.drop_column("agent_runs", "skill_version")
    op.drop_column("agent_runs", "selected_skill")
