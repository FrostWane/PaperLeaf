"""创建 PaperLeaf 初始数据结构。

Revision ID: 20260722_0001
Revises:

初始迁移必须冻结为发布当时的表结构，不能引用会随版本增长的 ORM
``Base.metadata``。否则全新安装会先创建未来迁移中的表，随后在对应迁移中
再次创建并触发 ``DuplicateTableError``。
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision = "20260722_0001"
down_revision = None
branch_labels = None
depends_on = None


def _initial_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    user_role = sa.Enum("user", "admin", name="userrole")
    paper_status = sa.Enum(
        "uploaded",
        "queued",
        "extracting",
        "ocr",
        "indexing",
        "ready",
        "partial",
        "failed",
        "deleting",
        name="paperstatus",
    )
    job_status = sa.Enum("queued", "running", "completed", "failed", name="jobstatus")

    sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True, index=True),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    sa.Table(
        "user_sessions",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    sa.Table(
        "papers",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("authors", sa.JSON(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("doi", sa.String(255), nullable=True),
        sa.Column("arxiv_id", sa.String(64), nullable=True),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("storage_key", sa.String(1000), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("status", paper_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Index("ix_papers_owner_sha256", "owner_id", "sha256", unique=True),
        sa.Index("ix_papers_owner_doi", "owner_id", "doi"),
    )
    sa.Table(
        "collections",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "name", name="uq_collection_owner_name"),
    )
    sa.Table(
        "tags",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("color", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "name", name="uq_tag_owner_name"),
    )
    sa.Table(
        "paper_collections",
        metadata,
        sa.Column(
            "paper_id",
            sa.String(36),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "collection_id",
            sa.String(36),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    sa.Table(
        "paper_tags",
        metadata,
        sa.Column(
            "paper_id",
            sa.String(36),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.String(36),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    sa.Table(
        "paper_pages",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "paper_id",
            sa.String(36),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("physical_page", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("extraction_method", sa.String(32), nullable=False),
        sa.Index("ix_page_paper_number", "paper_id", "physical_page", unique=True),
    )
    sa.Table(
        "paper_chunks",
        metadata,
        sa.Column("id", sa.String(160), primary_key=True),
        sa.Column(
            "page_id",
            sa.String(36),
            sa.ForeignKey("paper_pages.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "paper_id",
            sa.String(36),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("physical_page", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(), nullable=True),
    )
    sa.Table(
        "jobs",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "paper_id",
            sa.String(36),
            sa.ForeignKey("papers.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Index("ix_jobs_claim", "status", "available_at", "created_at"),
    )
    sa.Table(
        "agent_runs",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("session_id", sa.String(100), nullable=False, index=True),
        sa.Column("thread_id", sa.String(300), nullable=False, unique=True, index=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("tool_steps", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("token_usage", sa.JSON(), nullable=True),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("pending_action", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    return metadata


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    _initial_metadata().create_all(bind=op.get_bind())
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_paper_chunks_fts "
        "ON paper_chunks USING GIN (to_tsvector('simple', text))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_paper_chunks_trgm "
        "ON paper_chunks USING GIN (text gin_trgm_ops)"
    )


def downgrade() -> None:
    _initial_metadata().drop_all(bind=op.get_bind())
