"""持久化论文发现批次、反馈与推荐漏斗。

Revision ID: 20260808_0010
Revises: 20260807_0009
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260808_0010"
down_revision = "20260807_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discovery_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("batch_number", sa.Integer(), nullable=False),
        sa.Column("basis_paper_count", sa.Integer(), nullable=False),
        sa.Column("seed_paper_title", sa.String(length=1000), nullable=True),
        sa.Column("profile_terms", sa.JSON(), nullable=False),
        sa.Column("strategy", sa.String(length=48), nullable=False),
        sa.Column("feedback_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "batch_number", name="uq_discovery_user_batch"),
    )
    op.create_index("ix_discovery_batches_user_id", "discovery_batches", ["user_id"])
    op.create_index(
        "ix_discovery_batches_user_created", "discovery_batches", ["user_id", "created_at"]
    )
    op.create_table(
        "discovery_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("arxiv_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("authors", sa.JSON(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=False),
        sa.Column("published", sa.String(length=32), nullable=False),
        sa.Column("pdf_url", sa.String(length=1000), nullable=False),
        sa.Column("journal_ref", sa.String(length=1000), nullable=True),
        sa.Column("matched_paper_title", sa.String(length=1000), nullable=False),
        sa.Column("matched_terms", sa.JSON(), nullable=False),
        sa.Column("match_type", sa.String(length=24), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feedback", sa.String(length=24), nullable=True),
        sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["discovery_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "arxiv_id", name="uq_discovery_batch_arxiv"),
    )
    op.create_index("ix_discovery_items_batch_id", "discovery_items", ["batch_id"])
    op.create_index("ix_discovery_items_user_id", "discovery_items", ["user_id"])
    op.create_index(
        "ix_discovery_items_user_created", "discovery_items", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_discovery_items_user_feedback", "discovery_items", ["user_id", "feedback"]
    )


def downgrade() -> None:
    op.drop_table("discovery_items")
    op.drop_table("discovery_batches")
