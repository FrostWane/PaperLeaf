"""公开 API 的 Pydantic 类型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .models import JobStatus, PaperStatus, UserRole


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
    translation_language: str = Field(default="zh-CN", min_length=2, max_length=32)
    arxiv_search_enabled: bool = False


class UserPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    font_scale: Optional[Literal["small", "standard", "large"]] = None
    pdf_zoom: Optional[int] = Field(default=None, ge=50, le=200)
    left_panel_open: Optional[bool] = None
    assistant_panel_open: Optional[bool] = None
    translation_language: Optional[str] = Field(default=None, min_length=2, max_length=32)
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
    scope: Literal["paper", "selection", "collection", "library"] = "library"
    selected_paper_ids: list[str] = Field(default_factory=list, max_length=50)
    selected_collection_id: Optional[str] = None
    web_enabled: bool = False


class AgentRunRead(BaseModel):
    run_id: str
    session_id: str
    status: Literal["pending", "running", "interrupted", "completed", "failed", "cancelled"]
    answer: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_quality: dict[str, Any] = Field(default_factory=dict)
    node_trace: list[dict[str, Any]] = Field(default_factory=list)
    model_attempts: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: Optional[int] = None
    error: Optional[str] = None


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


class SummaryResponse(BaseModel):
    paper_id: str
    content: str
    citations: list[ArtifactCitation]
    mode: Literal["model", "extractive"]


class StructureNode(BaseModel):
    id: str
    label: str
    physical_page: int
    chunk_id: str


class StructureEdge(BaseModel):
    source: str
    target: str


class StructureGraphResponse(BaseModel):
    paper_id: str
    nodes: list[StructureNode]
    edges: list[StructureEdge]
    mermaid: str


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
    type: str
    status: JobStatus
    progress: int
    attempts: int
    max_attempts: int
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
