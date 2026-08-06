"""核心持久化模型。"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - 仅简化无可选依赖的静态检查
    Vector = JSON  # type: ignore[misc,assignment]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class UserRole(str, enum.Enum):
    user = "user"
    admin = "admin"


class PaperStatus(str, enum.Enum):
    uploaded = "uploaded"
    queued = "queued"
    extracting = "extracting"
    ocr = "ocr"
    indexing = "indexing"
    ready = "ready"
    partial = "partial"
    failed = "failed"
    deleting = "deleting"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


paper_collections = Table(
    "paper_collections",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("collection_id", ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True),
)

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.user)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    papers: Mapped[list[Paper]] = relationship(back_populates="owner")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Paper(Base):
    __tablename__ = "papers"
    __table_args__ = (
        Index("ix_papers_owner_sha256", "owner_id", "sha256", unique=True),
        Index("ix_papers_owner_doi", "owner_id", "doi", unique=False),
        Index("ix_papers_owner_archived", "owner_id", "archived_at"),
        Index("ix_papers_owner_last_opened", "owner_id", "last_opened_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(1000))
    authors: Mapped[list[str]] = mapped_column(JSON, default=list)
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    abstract: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    doi: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    publication: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    arxiv_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    filename: Mapped[str] = mapped_column(String(500))
    storage_key: Mapped[str] = mapped_column(String(1000))
    mime_type: Mapped[str] = mapped_column(String(100), default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[PaperStatus] = mapped_column(Enum(PaperStatus), default=PaperStatus.uploaded)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    owner: Mapped[User] = relationship(back_populates="papers")
    pages: Mapped[list[PaperPage]] = relationship(
        back_populates="paper", cascade="all,delete-orphan"
    )
    collections: Mapped[list[Collection]] = relationship(
        secondary=paper_collections, back_populates="papers"
    )


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "parent_id",
            "name",
            name="uq_collection_owner_parent_name",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(
            "collections.id",
            name="fk_collections_parent_id_collections",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    papers: Mapped[list[Paper]] = relationship(
        secondary=paper_collections, back_populates="collections"
    )


class PaperPage(Base):
    __tablename__ = "paper_pages"
    __table_args__ = (Index("ix_page_paper_number", "paper_id", "physical_page", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    physical_page: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    extraction_method: Mapped[str] = mapped_column(String(32), default="text")

    paper: Mapped[Paper] = relationship(back_populates="pages")
    chunks: Mapped[list[PaperChunk]] = relationship(
        back_populates="page", cascade="all,delete-orphan"
    )


class PaperChunk(Base):
    __tablename__ = "paper_chunks"
    __table_args__ = (
        Index(
            "ix_paper_chunks_fts",
            sql_text("to_tsvector('simple', text)"),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_paper_chunks_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ).ddl_if(dialect="postgresql"),
    )

    # 页级 Chunk 使用可解释的 `{paper_id}:p{page}:c{index}` 稳定键，长度会超过 UUID。
    id: Mapped[str] = mapped_column(String(160), primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(
        ForeignKey("paper_pages.id", ondelete="CASCADE"), index=True
    )
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    physical_page: Mapped[int] = mapped_column(Integer)
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(), nullable=True)

    page: Mapped[PaperPage] = relationship(back_populates="chunks")


class PaperArtifact(Base):
    __tablename__ = "paper_artifacts"
    __table_args__ = (
        UniqueConstraint("paper_id", "type", name="uq_paper_artifact_type"),
        Index("ix_paper_artifacts_paper_status", "paper_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32))
    source_revision: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="ready", index=True)
    fallback_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    structured_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    markdown: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperTranslation(Base):
    __tablename__ = "paper_translations"
    __table_args__ = (
        UniqueConstraint(
            "paper_id",
            "target_language",
            name="uq_paper_translation_language",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    target_language: Mapped[str] = mapped_column(String(32))
    source_revision: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    total_pages: Mapped[int] = mapped_column(Integer, default=0)
    completed_pages: Mapped[int] = mapped_column(Integer, default=0)
    failed_pages: Mapped[int] = mapped_column(Integer, default=0)
    priority_page: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperTranslationPage(Base):
    __tablename__ = "paper_translation_pages"
    __table_args__ = (
        UniqueConstraint(
            "translation_id",
            "physical_page",
            name="uq_translation_physical_page",
        ),
        Index("ix_translation_pages_work", "translation_id", "status", "priority"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    translation_id: Mapped[str] = mapped_column(
        ForeignKey("paper_translations.id", ondelete="CASCADE"), index=True
    )
    physical_page: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    translated_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_text_hash: Mapped[str] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer, default=1000)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "available_at", "created_at"),
        Index("uq_jobs_translation_id", "translation_id", unique=True),
        Index("uq_jobs_agent_run_id", "agent_run_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    translation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("paper_translations.id", ondelete="SET NULL"), nullable=True
    )
    agent_run_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(64))
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.queued)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (Index("ix_chat_sessions_user_updated", "user_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(100), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    type: Mapped[str] = mapped_column(String(32), default="library")
    paper_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"), nullable=True
    )
    collection_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("collections.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "client_message_id", name="uq_chat_message_client_id"
        ),
        UniqueConstraint("session_id", "sequence", name="uq_chat_message_sequence"),
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey(
            "chat_sessions.id",
            name="fk_chat_messages_session_id_chat_sessions",
            ondelete="CASCADE",
        ),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16))
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[dict]] = mapped_column(JSON, default=list)
    run_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(
            "agent_runs.id",
            name="fk_chat_messages_run_id_agent_runs",
            ondelete="CASCADE",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
        index=True,
    )
    client_message_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    request_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index(
            "uq_agent_runs_active_session",
            "session_id",
            unique=True,
            postgresql_where=sql_text(
                "status IN ('pending', 'running', 'interrupted')"
            ),
            sqlite_where=sql_text(
                "status IN ('pending', 'running', 'interrupted')"
            ),
        ),
        Index("uq_agent_runs_user_message_id", "user_message_id", unique=True),
        Index("uq_agent_runs_assistant_message_id", "assistant_message_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey(
            "chat_sessions.id",
            name="fk_agent_runs_session_id_chat_sessions",
            ondelete="CASCADE",
        ),
        index=True,
    )
    thread_id: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    tool_steps: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    token_usage: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    pending_action: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    scope_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    user_message_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(
            "chat_messages.id",
            name="fk_agent_runs_user_message_id_chat_messages",
            ondelete="SET NULL",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    assistant_message_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(
            "chat_messages.id",
            name="fk_agent_runs_assistant_message_id_chat_messages",
            ondelete="SET NULL",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    request_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    legacy_session_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    resume_action_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    resume_decision: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRunEvent(Base):
    __tablename__ = "agent_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_run_event_sequence"),
        UniqueConstraint("run_id", "event_key", name="uq_agent_run_event_key"),
        Index("ix_agent_run_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    event: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
