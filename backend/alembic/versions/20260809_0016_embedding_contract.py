"""add embedding index contract metadata

Revision ID: 20260809_0016
Revises: 20260808_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0016"
down_revision: str | None = "20260808_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_items", sa.Column("embedding_fingerprint", sa.String(64), nullable=True)
    )
    op.add_column("papers", sa.Column("embedding_provider", sa.String(80), nullable=True))
    op.add_column("papers", sa.Column("embedding_model", sa.String(240), nullable=True))
    op.add_column("papers", sa.Column("embedding_dimensions", sa.Integer(), nullable=True))
    op.add_column(
        "papers", sa.Column("embedding_index_revision", sa.Integer(), nullable=True)
    )
    op.add_column("papers", sa.Column("embedding_fingerprint", sa.String(64), nullable=True))
    op.add_column(
        "papers",
        sa.Column(
            "embedding_status",
            sa.String(24),
            nullable=False,
            server_default="unavailable",
        ),
    )
    # 历史向量没有模型与修订信息，维度相同也不能证明位于同一向量空间。
    op.execute("UPDATE papers SET embedding_status = 'stale'")


def downgrade() -> None:
    op.drop_column("memory_items", "embedding_fingerprint")
    op.drop_column("papers", "embedding_status")
    op.drop_column("papers", "embedding_fingerprint")
    op.drop_column("papers", "embedding_index_revision")
    op.drop_column("papers", "embedding_dimensions")
    op.drop_column("papers", "embedding_model")
    op.drop_column("papers", "embedding_provider")
