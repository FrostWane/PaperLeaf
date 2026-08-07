"""公开 API 的 Pydantic 类型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import JobStatus, PaperStatus, UserRole

TranslationLanguage = Literal["zh-CN", "zh-TW", "en", "ja", "ko"]


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=8, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    temporary_password: str = Field(min_length=12, max_length=256)
    role: UserRole = UserRole.user


class UserUpdate(BaseModel):
    active: Optional[bool] = None
    role: Optional[UserRole] = None


class UserPreferences(BaseModel):
    font_scale: Literal["small", "standard", "large"] = "standard"
    pdf_zoom: int = Field(default=100, ge=50, le=200)
    left_panel_open: bool = True
    assistant_panel_open: bool = True
    translation_language: TranslationLanguage = "zh-CN"
    arxiv_search_enabled: bool = False


class UserPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    font_scale: Optional[Literal["small", "standard", "large"]] = None
    pdf_zoom: Optional[int] = Field(default=None, ge=50, le=200)
    left_panel_open: Optional[bool] = None
    assistant_panel_open: Optional[bool] = None
    translation_language: Optional[TranslationLanguage] = None
    arxiv_search_enabled: Optional[bool] = None


class UserPreferencesRead(UserPreferences):
    display_name: Optional[str] = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    display_name: Optional[str]
    preferences: UserPreferences = Field(default_factory=UserPreferences)
    role: UserRole
    active: bool
    must_change_password: bool
    created_at: datetime


class PaperUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    authors: Optional[list[str]] = None
    year: Optional[int] = Field(default=None, ge=1400, le=2200)
    abstract: Optional[str] = Field(default=None, max_length=50_000)
    doi: Optional[str] = Field(default=None, max_length=255)
    publication: Optional[str] = Field(default=None, max_length=1000)


class PaperRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    title: str
    authors: list[str]
    year: Optional[int]
    abstract: Optional[str]
    doi: Optional[str]
    publication: Optional[str]
    arxiv_id: Optional[str]
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    page_count: Optional[int]
    status: PaperStatus
    archived_at: Optional[datetime]
    last_opened_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class PaperBulkActionRequest(BaseModel):
    paper_ids: list[str] = Field(min_length=1, max_length=100)
    action: Literal[
        "archive",
        "unarchive",
        "add_collection",
        "remove_collection",
    ]
    target_id: Optional[str] = None


class PaperBulkActionResponse(BaseModel):
    action: str
    affected: int
    paper_ids: list[str]


class ChatMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=12_000)
    web_enabled: bool = False


class ChatSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="新会话", min_length=1, max_length=200)
    type: Literal["paper", "collection", "library"] = "library"
    paper_id: Optional[str] = None
    collection_id: Optional[str] = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("会话标题不能为空")
        return normalized


class ChatSessionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("会话标题不能为空")
        return normalized


class ChatSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    type: Literal["paper", "collection", "library"]
    paper_id: Optional[str]
    collection_id: Optional[str]
    current_run_id: Optional[str] = None
    current_run_status: Optional[
        Literal["pending", "running", "interrupted", "completed", "failed", "cancelled"]
    ] = None
    created_at: datetime
    updated_at: datetime


class ChatMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: Literal["user", "assistant"]
    sequence: int = Field(ge=1)
    status: Literal["pending", "streaming", "completed", "failed", "cancelled"]
    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    run_id: Optional[str]
    created_at: datetime
    updated_at: datetime


class ChatSubmissionRead(BaseModel):
    session_id: str
    message_id: str
    run_id: str
    status: Literal["pending"] = "pending"
    replayed: bool = False


class AgentRunRead(BaseModel):
    run_id: str
    session_id: str
    status: Literal["pending", "running", "interrupted", "completed", "failed", "cancelled"]
    cancel_requested: bool = False
    scope_snapshot: dict[str, Any] = Field(default_factory=dict)
    pending_action: Optional[dict[str, Any]] = None
    answer: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_quality: dict[str, Any] = Field(default_factory=dict)
    node_trace: list[dict[str, Any]] = Field(default_factory=list)
    model_attempts: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class AgentRunEventRead(BaseModel):
    id: int
    sequence: int
    event: Literal[
        "run_started",
        "node_started",
        "node_finished",
        "tool_started",
        "tool_finished",
        "message_delta",
        "citation",
        "interrupt",
        "error",
        "run_finished",
    ]
    run_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AgentResumeRequest(BaseModel):
    action_id: str
    decision: Literal["approve", "reject"]


class ArxivSearchResponse(BaseModel):
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    published: str
    pdf_url: str


class ArxivImportRequest(BaseModel):
    arxiv_id: str = Field(pattern=r"^[0-9]{4}\.[0-9]{4,5}(v[0-9]+)?$")


class ArtifactCitation(BaseModel):
    chunk_id: str
    physical_page: int
    quote: Optional[str] = None


class SummaryFact(BaseModel):
    text: str
    citations: list[ArtifactCitation]


class SummarySection(BaseModel):
    key: Literal[
        "research_question",
        "core_method",
        "experimental_setup",
        "main_results",
        "limitations_scope",
    ]
    title: str
    facts: list[SummaryFact]


class SummaryResponse(BaseModel):
    paper_id: str
    status: Literal["ready", "processing", "stale", "failed"]
    stale: bool = False
    fallback_reason: Optional[str] = None
    sections: list[SummarySection]
    content: str
    citations: list[ArtifactCitation]
    mode: Literal["model", "extractive"]


class StructureNode(BaseModel):
    id: str
    type: Literal["研究问题", "背景", "方法", "数据", "实验", "结果", "局限"]
    label: str
    summary: str
    citations: list[ArtifactCitation]


class StructureEdge(BaseModel):
    source: str
    target: str


class StructureGraphResponse(BaseModel):
    paper_id: str
    status: Literal["ready", "processing", "failed", "stale"]
    stale: bool = False
    fallback_reason: Optional[str] = None
    nodes: list[StructureNode]
    edges: list[StructureEdge]
    mermaid: str
    evidence_excerpt: str = ""


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    parent_id: Optional[str] = None


class CollectionUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    parent_id: Optional[str] = None


class CollectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    parent_id: Optional[str]
    name: str
    description: Optional[str]
    paper_ids: list[str] = Field(default_factory=list)
    recursive_paper_count: int = 0
    children: list[CollectionRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    paper_id: Optional[str]
    translation_id: Optional[str] = None
    type: str
    status: JobStatus
    progress: int
    attempts: int
    max_attempts: int
    error_code: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime


class TranslationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_language: TranslationLanguage = "zh-CN"
    priority_page: Optional[int] = Field(default=None, ge=1)
    refresh: bool = False


class TranslationPageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    translation_id: str
    physical_page: int
    status: Literal[
        "queued",
        "running",
        "completed",
        "no_text",
        "failed",
        "cancelled",
    ]
    translated_text: Optional[str]
    attempts: int
    max_attempts: int
    error_code: Optional[str]
    error_message: Optional[str]
    updated_at: datetime


class PaperTranslationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    paper_id: str
    target_language: TranslationLanguage
    source_revision: str
    status: Literal[
        "queued",
        "running",
        "completed",
        "partial",
        "failed",
        "cancelled",
    ]
    total_pages: int
    completed_pages: int
    failed_pages: int
    priority_page: Optional[int]
    cancel_requested: bool
    error_code: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime


class Citation(BaseModel):
    paper_id: str
    paper_title: str
    physical_page: int = Field(ge=1)
    chunk_id: str
    excerpt: str
    viewer_url: str


class PendingAction(BaseModel):
    action_id: str
    type: Literal["confirm_arxiv_import"]
    candidates: list[dict[str, Any]]
    risk_message: str
    allowed_decisions: list[Literal["approve", "reject"]] = ["approve", "reject"]


class SSEEvent(BaseModel):
    event: Literal[
        "run_started",
        "node_started",
        "node_finished",
        "tool_started",
        "tool_finished",
        "message_delta",
        "citation",
        "interrupt",
        "error",
        "run_finished",
    ]
    run_id: str
    data: dict[str, Any] = Field(default_factory=dict)

    def encode(self) -> str:
        return f"event: {self.event}\ndata: {self.model_dump_json()}\n\n"
