"""context compaction and user controlled memory

Revision ID: 20260808_0012
Revises: 20260808_0011
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0012"
down_revision: str | None = "20260808_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "compact_summary", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("summary_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("compacted_through_message_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("entity_state", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
    )

    op.create_table(
        "memory_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("normalized_hash", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="1", nullable=False),
        sa.Column("source_kind", sa.String(length=32), server_default="explicit", nullable=False),
        sa.Column("source_session_id", sa.String(length=100), nullable=True),
        sa.Column("source_message_id", sa.String(length=36), nullable=True),
        sa.Column("source_excerpt", sa.String(length=500), nullable=True),
        sa.Column("pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_message_id"], ["chat_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_session_id"], ["chat_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "normalized_hash", name="uq_memory_user_hash"),
    )
    op.create_index("ix_memory_items_user_id", "memory_items", ["user_id"])
    op.create_index(
        "ix_memory_items_user_active", "memory_items", ["user_id", "enabled", "updated_at"]
    )

    op.create_table(
        "memory_item_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("memory_item_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_excerpt", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["memory_item_id"], ["memory_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("memory_item_id", "version", name="uq_memory_item_version"),
    )
    op.create_index(
        "ix_memory_item_versions_memory_item_id", "memory_item_versions", ["memory_item_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_memory_item_versions_memory_item_id", table_name="memory_item_versions")
    op.drop_table("memory_item_versions")
    op.drop_index("ix_memory_items_user_active", table_name="memory_items")
    op.drop_index("ix_memory_items_user_id", table_name="memory_items")
    op.drop_table("memory_items")
    op.drop_column("chat_sessions", "entity_state")
    op.drop_column("chat_sessions", "compacted_through_message_id")
    op.drop_column("chat_sessions", "summary_version")
    op.drop_column("chat_sessions", "compact_summary")
