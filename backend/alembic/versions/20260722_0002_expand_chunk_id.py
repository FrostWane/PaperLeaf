"""扩展可解释 Chunk 标识长度。

Revision ID: 20260722_0002
Revises: 20260722_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "20260722_0002"
down_revision = "20260722_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "paper_chunks",
        "id",
        existing_type=sa.String(length=36),
        type_=sa.String(length=160),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "paper_chunks",
        "id",
        existing_type=sa.String(length=160),
        type_=sa.String(length=36),
        existing_nullable=False,
    )
