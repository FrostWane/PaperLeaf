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


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
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


class PaperRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    title: str
    authors: list[str]
    year: Optional[int]
    abstract: Optional[str]
    doi: Optional[str]
    arxiv_id: Optional[str]
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    page_count: Optional[int]
    status: PaperStatus
    created_at: datetime
    updated_at: datetime


class ChatMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=12_000)
    scope: Literal["paper", "selection", "library"] = "library"
    selected_paper_ids: list[str] = Field(default_factory=list, max_length=50)
    web_enabled: bool = False


class AgentRunRead(BaseModel):
    run_id: str
    session_id: str
    status: Literal["pending", "running", "interrupted", "completed", "failed", "cancelled"]
    answer: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
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


class CollectionUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)


class CollectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    name: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class TagUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    color: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class TagRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    name: str
    color: Optional[str]
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
