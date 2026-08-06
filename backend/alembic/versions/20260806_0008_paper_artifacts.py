"""持久化论文总结与研究结构图产物。

Revision ID: 20260806_0008
Revises: 20260806_0007
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260806_0008"
down_revision = "20260806_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "paper_artifacts" in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        "paper_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column("structured_payload", sa.JSON(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("paper_id", "type", name="uq_paper_artifact_type"),
    )
    op.create_index("ix_paper_artifacts_owner_id", "paper_artifacts", ["owner_id"])
    op.create_index("ix_paper_artifacts_paper_id", "paper_artifacts", ["paper_id"])
    op.create_index("ix_paper_artifacts_status", "paper_artifacts", ["status"])
    op.create_index(
        "ix_paper_artifacts_paper_status",
        "paper_artifacts",
        ["paper_id", "status"],
    )


def downgrade() -> None:
    if "paper_artifacts" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("paper_artifacts")
