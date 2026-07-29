"""增加文献归档与最近阅读状态。

Revision ID: 20260729_0003
Revises: 20260722_0002
"""

import sqlalchemy as sa

from alembic import op

revision = "20260729_0003"
down_revision = "20260722_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 是早期开发阶段的 metadata migration。新装环境会从当前模型创建表，
    # 升级环境则仍缺少以下列；逐项检查可同时保证两条路径可重复执行。
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("papers")}
    if "archived_at" not in columns:
        op.add_column(
            "papers", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )
    if "last_opened_at" not in columns:
        op.add_column(
            "papers", sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=True)
        )

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("papers")}
    if "ix_papers_owner_archived" not in indexes:
        op.create_index("ix_papers_owner_archived", "papers", ["owner_id", "archived_at"])
    if "ix_papers_owner_last_opened" not in indexes:
        op.create_index(
            "ix_papers_owner_last_opened", "papers", ["owner_id", "last_opened_at"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("papers")}
    if "ix_papers_owner_last_opened" in indexes:
        op.drop_index("ix_papers_owner_last_opened", table_name="papers")
    if "ix_papers_owner_archived" in indexes:
        op.drop_index("ix_papers_owner_archived", table_name="papers")
    columns = {item["name"] for item in sa.inspect(bind).get_columns("papers")}
    if "last_opened_at" in columns:
        op.drop_column("papers", "last_opened_at")
    if "archived_at" in columns:
        op.drop_column("papers", "archived_at")
