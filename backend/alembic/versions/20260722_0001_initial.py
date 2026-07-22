"""创建 PaperLeaf 初始数据结构。

Revision ID: 20260722_0001
Revises:
"""

from alembic import op
from paperleaf_api import models  # noqa: F401
from paperleaf_api.db import Base

revision = "20260722_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    Base.metadata.create_all(bind=op.get_bind())
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_paper_chunks_fts "
        "ON paper_chunks USING GIN (to_tsvector('simple', text))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_paper_chunks_trgm "
        "ON paper_chunks USING GIN (text gin_trgm_ops)"
    )


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
