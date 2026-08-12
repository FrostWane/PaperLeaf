"""业务仓库接口与离线可用的内存实现。

生产部署可将相同接口替换为 SQLAlchemy 实现；权限判断始终发生在仓库查询条件中，
避免先取出他人资源再在路由层过滤。
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, Union

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session_factory
from .models import (
    AgentRun,
    AgentRunEvent,
    AgentToolArtifact,
    AgentToolCall,
    ChatMessage,
    ChatSession,
    Collection,
    DiscoveryBatch,
    DiscoveryItem,
    Job,
    JobStatus,
    McpServerConfig,
    McpToolSnapshot,
    MemoryItem,
    MemoryItemVersion,
    Paper,
    PaperArtifact,
    PaperPage,
    PaperStatus,
    PaperTranslation,
    PaperTranslationPage,
    User,
    UserRole,
    UserSession,
    paper_collections,
)
from .security import digest_session_token, hash_password, verify_password


def now() -> datetime:
    return datetime.now(timezone.utc)


def _discovery_metric_report(
    batches: int,
    impressions: int,
    opened: int,
    interested: int,
    not_interested: int,
    imported: int,
    feedback: int,
) -> dict[str, int | float]:
    return {
        "batches": batches,
        "impressions": impressions,
        "opened": opened,
        "interested": interested,
        "not_interested": not_interested,
        "imported": imported,
        "feedback_count": feedback,
        "click_through_rate": opened / impressions if impressions else 0.0,
        "interest_hit_rate": interested / feedback if feedback else 0.0,
        "feedback_rate": feedback / impressions if impressions else 0.0,
        "import_rate": imported / impressions if impressions else 0.0,
    }


ARTIFACT_JOB_TYPES = {
    "summary": "summarize_paper",
    "structure": "build_structure_graph",
}


@dataclass
class UserRecord:
    id: str
    email: str
    password_hash: str
    display_name: str | None = None
    preferences: dict = field(default_factory=dict)
    role: UserRole = UserRole.user
    active: bool = True
    must_change_password: bool = True
    created_at: datetime = field(default_factory=now)


@dataclass
class PaperRecord:
    id: str
    owner_id: str
    title: str
    authors: list[str]
    year: int | None
    abstract: str | None
    doi: str | None
    arxiv_id: str | None
    filename: str
    storage_key: str
    mime_type: str
    size_bytes: int
    sha256: str
    page_count: int | None
    publication: str | None = None
    academic_external_ids: dict[str, str] = field(default_factory=dict)
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    embedding_index_revision: int | None = None
    embedding_fingerprint: str | None = None
    embedding_status: str = "unavailable"
    status: PaperStatus = PaperStatus.uploaded
    archived_at: datetime | None = None
    last_opened_at: datetime | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class DiscoveryBatchRecord:
    id: str
    user_id: str
    batch_number: int
    basis_paper_count: int
    seed_paper_title: str | None
    profile_terms: list[str]
    strategy: str
    feedback_applied: bool = False
    created_at: datetime = field(default_factory=now)


@dataclass
class DiscoveryItemRecord:
    id: str
    batch_id: str
    user_id: str
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    published: str
    pdf_url: str
    journal_ref: str | None
    matched_paper_title: str
    matched_terms: list[str]
    match_type: str
    score: float
    rank: int
    opened_at: datetime | None = None
    feedback: str | None = None
    feedback_at: datetime | None = None
    imported_at: datetime | None = None
    created_at: datetime = field(default_factory=now)


DiscoveryBatchPage = tuple[
    Union[DiscoveryBatchRecord, DiscoveryBatch],
    list[Union[DiscoveryItemRecord, DiscoveryItem]],
]


@dataclass
class CollectionRecord:
    id: str
    owner_id: str
    name: str
    description: str | None = None
    parent_id: str | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class JobRecord:
    id: str
    paper_id: str | None
    type: str
    translation_id: str | None = None
    agent_run_id: str | None = None
    status: JobStatus = JobStatus.queued
    progress: int = 0
    attempts: int = 0
    max_attempts: int = 3
    available_at: datetime = field(default_factory=now)
    claimed_at: datetime | None = None
    claim_token: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class AgentRunRecord:
    id: str
    user_id: str
    session_id: str
    thread_id: str
    status: str = "pending"
    tool_steps: int = 0
    duration_ms: int | None = None
    token_usage: dict | None = None
    result_summary: dict | None = None
    pending_action: dict | None = None
    cancel_requested: bool = False
    scope_snapshot: dict = field(default_factory=dict)
    context_snapshot: dict = field(default_factory=dict)
    context_version: int = 1
    resolved_query: str | None = None
    reference_confidence: float | None = None
    selected_skill: str | None = None
    skill_version: int | None = None
    harness_trace: dict = field(default_factory=dict)
    orchestration_version: str = "single_agent_v1"
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    request_hash: str | None = None
    resume_action_id: str | None = None
    resume_decision: str | None = None
    error_code: str | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class TranslationRecord:
    id: str
    paper_id: str
    owner_id: str
    target_language: str
    source_revision: str
    status: str
    total_pages: int
    completed_pages: int = 0
    failed_pages: int = 0
    priority_page: int | None = None
    cancel_requested: bool = False
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class PaperArtifactRecord:
    id: str
    paper_id: str
    owner_id: str
    type: str
    source_revision: str
    status: str
    fallback_reason: str | None
    structured_payload: dict
    markdown: str
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class ChatSessionRecord:
    id: str
    user_id: str
    title: str
    type: str
    paper_id: str | None = None
    collection_id: str | None = None
    current_run_id: str | None = None
    current_run_status: str | None = None
    compact_summary: dict = field(default_factory=dict)
    summary_version: int = 1
    compacted_through_message_id: str | None = None
    entity_state: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class McpServerConfigRecord:
    id: str
    display_name: str
    endpoint_url: str
    transport: str = "streamable_http"
    enabled: bool = True
    allowed_hosts: list[str] = field(default_factory=list)
    cache_revision: int = 1
    health_status: str = "unknown"
    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None
    last_checked_at: datetime | None = None
    last_error_code: str | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class McpToolSnapshotRecord:
    id: str
    server_id: str
    normalized_name: str
    remote_name: str
    description: str
    input_schema: dict
    annotations: dict
    discovered_at: datetime = field(default_factory=now)


@dataclass
class ChatMessageRecord:
    id: str
    session_id: str
    role: str
    sequence: int
    status: str
    content: str
    citations: list[dict] = field(default_factory=list)
    run_id: str | None = None
    client_message_id: str | None = None
    request_hash: str | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class AgentRunEventRecord:
    id: int
    run_id: str
    sequence: int
    event: str
    data: dict = field(default_factory=dict)
    event_key: str | None = None
    created_at: datetime = field(default_factory=now)


@dataclass
class AgentToolCallRecord:
    id: str
    call_id: str
    run_id: str
    user_id: str
    skill_name: str
    tool_name: str
    tool_version: int = 1
    status: str = "running"
    arguments: dict = field(default_factory=dict)
    result_preview: dict | None = None
    attempt: int = 1
    duration_ms: int | None = None
    error_code: str | None = None
    requires_approval: bool = False
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class AgentToolArtifactRecord:
    id: str
    tool_call_id: str
    user_id: str
    content: dict
    token_count: int
    created_at: datetime = field(default_factory=now)


@dataclass
class MemoryItemRecord:
    id: str
    user_id: str
    type: str
    value: str
    normalized_hash: str
    confidence: float
    source_kind: str
    source_session_id: str | None = None
    source_message_id: str | None = None
    source_excerpt: str | None = None
    pinned: bool = False
    enabled: bool = True
    embedding: list[float] | None = None
    embedding_fingerprint: str | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class MemoryItemVersionRecord:
    id: str
    memory_item_id: str
    version: int
    value: str
    confidence: float
    status: str
    source_kind: str
    source_excerpt: str | None = None
    created_at: datetime = field(default_factory=now)


@dataclass(frozen=True)
class ChatSubmission:
    message: ChatMessageRecord | ChatMessage
    run: AgentRunRecord | AgentRun
    replayed: bool


class ChatIdempotencyConflictError(ValueError):
    """同一客户端消息 ID 被用于不同请求正文。"""


class ChatActiveRunError(ValueError):
    """同一会话已有尚未结束的 Agent Run。"""


@dataclass
class TranslationPageRecord:
    id: str
    translation_id: str
    physical_page: int
    status: str
    source_text_hash: str
    translated_text: str | None = None
    priority: int = 1000
    attempts: int = 0
    max_attempts: int = 3
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


class ManagedUserNotFoundError(ValueError):
    """管理员准备修改的用户不存在。"""


class CurrentAdminProtectionError(ValueError):
    """当前登录管理员不能停用自己。"""


class LastAdminProtectionError(ValueError):
    """变更会移除最后一名活跃管理员。"""


class TranslationSourceUnavailableError(ValueError):
    """论文尚无可用于翻译的已解析页面。"""


def source_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_revision(pages: list[tuple[int, str]]) -> str:
    digest = hashlib.sha256()
    for physical_page, text in sorted(pages):
        digest.update(f"{physical_page}:".encode())
        digest.update(source_text_hash(text).encode())
    return digest.hexdigest()


MAX_COLLECTION_DEPTH = 5
AGENT_JOB_LEASE = timedelta(minutes=30)


def _validate_collection_change(
    records: list[CollectionRecord | Collection],
    *,
    owner_id: str,
    name: str,
    parent_id: str | None,
    collection_id: str | None = None,
) -> None:
    """验证同级名称、父节点、循环和移动后整棵子树的深度。"""

    owned = {item.id: item for item in records if item.owner_id == owner_id}
    if parent_id is not None and parent_id not in owned:
        raise ValueError("父集合不存在")
    if collection_id is not None and parent_id == collection_id:
        raise ValueError("集合不能移动到自身或其子集合")

    descendants: set[str] = set()
    if collection_id is not None:
        pending = [collection_id]
        while pending:
            current = pending.pop()
            for item in owned.values():
                if item.parent_id == current and item.id not in descendants:
                    descendants.add(item.id)
                    pending.append(item.id)
        if parent_id in descendants:
            raise ValueError("集合不能移动到自身或其子集合")

    if any(
        item.id != collection_id
        and item.parent_id == parent_id
        and item.name.casefold() == name.casefold()
        for item in owned.values()
    ):
        raise ValueError("同级集合名称已存在")

    parent_depth = 0
    cursor = parent_id
    visited: set[str] = set()
    while cursor is not None:
        if cursor in visited:
            raise ValueError("集合层级存在循环")
        visited.add(cursor)
        parent_depth += 1
        cursor = owned[cursor].parent_id

    subtree_height = 1
    if collection_id is not None:
        levels = [(collection_id, 1)]
        while levels:
            current, height = levels.pop()
            subtree_height = max(subtree_height, height)
            levels.extend(
                (item.id, height + 1) for item in owned.values() if item.parent_id == current
            )
    if parent_depth + subtree_height > MAX_COLLECTION_DEPTH:
        raise ValueError(f"集合最多支持 {MAX_COLLECTION_DEPTH} 层")


class Repository(Protocol):
    async def find_user_by_email(self, email: str) -> UserRecord | None: ...
    async def get_user(self, user_id: str) -> UserRecord | None: ...
    async def create_user(
        self,
        email: str,
        password: str,
        role: UserRole,
        must_change_password: bool = True,
    ) -> UserRecord: ...
    async def list_users(self) -> list[UserRecord]: ...
    async def update_user(self, user_id: str, **changes: object) -> UserRecord | None: ...
    async def update_managed_user(
        self, user_id: str, acting_admin_id: str, **changes: object
    ) -> UserRecord: ...
    async def create_session(self, user_id: str, token: str, ttl_seconds: int) -> None: ...
    async def user_for_session(self, token: str) -> UserRecord | None: ...
    async def delete_session(self, token: str) -> None: ...
    async def set_password(self, user_id: str, password: str) -> UserRecord: ...
    async def create_paper(self, paper: PaperRecord) -> PaperRecord: ...

    async def mark_embedding_contract_stale(self, fingerprint: str | None) -> int: ...
    async def embedding_contract_counts(self, fingerprint: str | None) -> dict[str, int]: ...
    async def list_papers(
        self,
        owner_id: str,
        collection_id: str | None = None,
        unfiled: bool = False,
    ) -> list[PaperRecord]: ...
    async def get_latest_discovery_batch(self, user_id: str) -> DiscoveryBatchPage | None: ...
    async def list_discovery_seen_arxiv_ids(self, user_id: str) -> set[str]: ...
    async def get_discovery_feedback_signals(
        self, user_id: str, *, limit: int = 20
    ) -> tuple[list[str], list[str]]: ...
    async def create_discovery_batch(
        self, batch: DiscoveryBatchRecord, items: list[DiscoveryItemRecord]
    ) -> DiscoveryBatchPage: ...
    async def record_discovery_item_action(
        self, item_id: str, user_id: str, action: str, *, arxiv_id: str | None = None
    ) -> DiscoveryItemRecord | DiscoveryItem | None: ...
    async def discovery_metrics(self, since: datetime) -> dict[str, int | float]: ...
    async def get_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None: ...
    async def get_owned_paper_page_text(
        self, paper_id: str, physical_page: int, owner_id: str
    ) -> str | None: ...
    async def get_owned_paper_artifact(
        self, paper_id: str, owner_id: str, artifact_type: str
    ) -> PaperArtifactRecord | PaperArtifact | None: ...
    async def update_owned_paper(
        self, paper_id: str, owner_id: str, **changes: object
    ) -> PaperRecord | None: ...
    async def requeue_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None: ...
    async def get_active_paper_artifact_job(
        self, paper_id: str, owner_id: str, artifact_type: str
    ) -> JobRecord | Job | None: ...
    async def enqueue_paper_artifact(
        self,
        paper_id: str,
        owner_id: str,
        artifact_type: str,
        source_revision_value: str,
        *,
        preserve_existing: bool,
    ) -> JobRecord | Job | None: ...
    async def delete_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None: ...
    async def touch_paper_opened(self, paper_id: str, owner_id: str) -> PaperRecord | None: ...
    async def set_papers_archived(
        self, paper_ids: list[str], owner_id: str, archived: bool
    ) -> list[str] | None: ...
    async def list_collection_memberships(self, owner_id: str) -> dict[str, list[str]]: ...
    async def resolve_collection_paper_ids(
        self, collection_id: str, owner_id: str, *, ready_only: bool = False
    ) -> list[str] | None: ...
    async def create_or_resume_translation(
        self,
        paper_id: str,
        owner_id: str,
        target_language: str,
        priority_page: int | None,
        *,
        model_available: bool,
        refresh: bool = False,
    ) -> TranslationRecord | PaperTranslation | None: ...
    async def get_owned_translation(
        self, paper_id: str, translation_id: str, owner_id: str
    ) -> TranslationRecord | PaperTranslation | None: ...
    async def list_translation_pages(
        self, translation_id: str, owner_id: str
    ) -> list[TranslationPageRecord | PaperTranslationPage]: ...
    async def get_owned_translation_page(
        self, paper_id: str, translation_id: str, physical_page: int, owner_id: str
    ) -> TranslationPageRecord | PaperTranslationPage | None: ...
    async def cancel_owned_translation(
        self, paper_id: str, translation_id: str, owner_id: str
    ) -> TranslationRecord | PaperTranslation | None: ...
    async def list_agent_runs_for_observability(
        self, since: datetime, *, limit: int = 5000
    ) -> list[AgentRunRecord | AgentRun]: ...
    async def is_agent_claim_current(self, run_id: str, claim_token: str) -> bool: ...
    async def update_agent_context(
        self,
        run_id: str,
        claim_token: str,
        *,
        context_snapshot: dict,
        resolved_query: str,
        reference_confidence: float,
    ) -> AgentRunRecord | AgentRun | None: ...
    async def update_agent_skill(
        self,
        run_id: str,
        claim_token: str,
        *,
        selected_skill: str,
        skill_version: int,
        harness_trace: dict,
    ) -> AgentRunRecord | AgentRun | None: ...
    async def start_agent_tool_call(
        self,
        record: AgentToolCallRecord,
        claim_token: str,
    ) -> AgentToolCallRecord | AgentToolCall | None: ...
    async def finish_agent_tool_call(
        self,
        tool_call_id: str,
        run_id: str,
        claim_token: str,
        *,
        status: str,
        attempt: int,
        duration_ms: int,
        result_preview: dict | None,
        error_code: str | None,
    ) -> AgentToolCallRecord | AgentToolCall | None: ...
    async def create_agent_tool_artifact(
        self,
        record: AgentToolArtifactRecord,
        claim_token: str,
    ) -> AgentToolArtifactRecord | AgentToolArtifact | None: ...
    async def list_agent_tool_calls_for_observability(
        self, since: datetime, *, limit: int = 10000
    ) -> list[AgentToolCallRecord | AgentToolCall]: ...
    async def memory_observability_counts(self) -> dict[str, object]: ...
    async def ensure_mcp_server_config(
        self, record: McpServerConfigRecord
    ) -> McpServerConfigRecord | McpServerConfig: ...
    async def list_mcp_server_configs(
        self,
    ) -> list[McpServerConfigRecord | McpServerConfig]: ...
    async def get_mcp_server_config(
        self, server_id: str
    ) -> McpServerConfigRecord | McpServerConfig | None: ...
    async def update_mcp_server_config(
        self, server_id: str, **changes: object
    ) -> McpServerConfigRecord | McpServerConfig | None: ...
    async def replace_mcp_tool_snapshots(
        self, server_id: str, records: list[McpToolSnapshotRecord]
    ) -> list[McpToolSnapshotRecord | McpToolSnapshot]: ...
    async def list_mcp_tool_snapshots(
        self, server_id: str
    ) -> list[McpToolSnapshotRecord | McpToolSnapshot]: ...
    async def update_session_compaction(
        self,
        session_id: str,
        user_id: str,
        *,
        compact_summary: dict,
        compacted_through_message_id: str | None,
        entity_state: dict,
    ) -> ChatSessionRecord | ChatSession | None: ...
    async def list_memories(
        self, user_id: str, *, enabled_only: bool = False
    ) -> list[MemoryItemRecord | MemoryItem]: ...
    async def create_memory_item(
        self, record: MemoryItemRecord
    ) -> MemoryItemRecord | MemoryItem: ...
    async def update_owned_memory(
        self, memory_id: str, user_id: str, **changes: object
    ) -> MemoryItemRecord | MemoryItem | None: ...
    async def delete_owned_memory(self, memory_id: str, user_id: str) -> bool: ...
    async def clear_memories(self, user_id: str) -> int: ...


class MemoryRepository:
    """Demo/Test 仓库；单进程使用，不作为生产持久化方案。"""

    def __init__(self, session_secret: str) -> None:
        self.users: dict[str, UserRecord] = {}
        self.papers: dict[str, PaperRecord] = {}
        self.sessions: dict[str, tuple[str, datetime]] = {}
        self.collections: dict[str, CollectionRecord] = {}
        self.paper_collections: set[tuple[str, str]] = set()
        self.jobs: dict[str, JobRecord] = {}
        self.paper_pages: dict[str, dict[int, str]] = {}
        self.translations: dict[str, TranslationRecord] = {}
        self.translation_pages: dict[str, TranslationPageRecord] = {}
        self.paper_artifacts: dict[str, PaperArtifactRecord] = {}
        self.discovery_batches: dict[str, DiscoveryBatchRecord] = {}
        self.discovery_items: dict[str, DiscoveryItemRecord] = {}
        self.chat_sessions: dict[str, ChatSessionRecord] = {}
        self.chat_messages: dict[str, ChatMessageRecord] = {}
        self.agent_runs: dict[str, AgentRunRecord] = {}
        self.agent_run_events: dict[int, AgentRunEventRecord] = {}
        self.agent_tool_calls: dict[str, AgentToolCallRecord] = {}
        self.agent_tool_artifacts: dict[str, AgentToolArtifactRecord] = {}
        self.mcp_server_configs: dict[str, McpServerConfigRecord] = {}
        self.mcp_tool_snapshots: dict[str, McpToolSnapshotRecord] = {}
        self.memory_items: dict[str, MemoryItemRecord] = {}
        self.memory_item_versions: dict[str, MemoryItemVersionRecord] = {}
        self._next_agent_event_id = 1
        self.session_secret = session_secret
        self._managed_user_lock = threading.Lock()

    async def ensure_admin(self, email: str, password: str) -> UserRecord:
        existing = await self.find_user_by_email(email)
        if existing:
            return existing
        return await self.create_user(email, password, UserRole.admin, must_change_password=False)

    async def find_user_by_email(self, email: str) -> UserRecord | None:
        normalized = email.strip().casefold()
        return next((user for user in self.users.values() if user.email == normalized), None)

    async def get_user(self, user_id: str) -> UserRecord | None:
        return self.users.get(user_id)

    async def create_user(
        self,
        email: str,
        password: str,
        role: UserRole,
        must_change_password: bool = True,
    ) -> UserRecord:
        normalized = email.strip().casefold()
        if await self.find_user_by_email(normalized):
            raise ValueError("邮箱已存在")
        user = UserRecord(
            id=str(uuid.uuid4()),
            email=normalized,
            password_hash=hash_password(password),
            role=role,
            must_change_password=must_change_password,
        )
        self.users[user.id] = user
        return user

    async def authenticate(self, email: str, password: str) -> UserRecord | None:
        user = await self.find_user_by_email(email)
        if not user or not user.active or not verify_password(user.password_hash, password):
            return None
        return user

    async def list_users(self) -> list[UserRecord]:
        return sorted(self.users.values(), key=lambda item: item.created_at)

    async def update_user(self, user_id: str, **changes: object) -> UserRecord | None:
        user = self.users.get(user_id)
        if not user:
            return None
        for key in (
            "active",
            "role",
            "must_change_password",
            "display_name",
            "preferences",
        ):
            if key in changes and (changes[key] is not None or key == "display_name"):
                setattr(user, key, changes[key])
        if changes.get("active") is False:
            self.sessions = {
                digest: value for digest, value in self.sessions.items() if value[0] != user_id
            }
        return user

    async def update_managed_user(
        self, user_id: str, acting_admin_id: str, **changes: object
    ) -> UserRecord:
        # MemoryRepository 也可能被多个 TestClient 线程共享；同步锁让检查与修改不可分割。
        with self._managed_user_lock:
            user = self.users.get(user_id)
            if not user:
                raise ManagedUserNotFoundError("用户不存在")
            removes_active_admin = (
                user.active
                and user.role == UserRole.admin
                and (changes.get("active") is False or changes.get("role") == UserRole.user)
            )
            if removes_active_admin:
                active_admins = sum(
                    1 for item in self.users.values() if item.active and item.role == UserRole.admin
                )
                if active_admins <= 1:
                    raise LastAdminProtectionError("不能停用或降级最后一名管理员")
            if user.id == acting_admin_id and changes.get("active") is False:
                raise CurrentAdminProtectionError("不能停用当前管理员")
            for key in ("active", "role"):
                if key in changes and changes[key] is not None:
                    setattr(user, key, changes[key])
            if changes.get("active") is False:
                self.sessions = {
                    digest: value for digest, value in self.sessions.items() if value[0] != user_id
                }
            return user

    async def create_session(self, user_id: str, token: str, ttl_seconds: int) -> None:
        digest = digest_session_token(token, self.session_secret)
        self.sessions[digest] = (user_id, now() + timedelta(seconds=ttl_seconds))

    async def user_for_session(self, token: str) -> UserRecord | None:
        digest = digest_session_token(token, self.session_secret)
        session = self.sessions.get(digest)
        if not session:
            return None
        user_id, expires_at = session
        if expires_at <= now():
            self.sessions.pop(digest, None)
            return None
        user = self.users.get(user_id)
        return user if user and user.active else None

    async def delete_session(self, token: str) -> None:
        self.sessions.pop(digest_session_token(token, self.session_secret), None)

    async def set_password(self, user_id: str, password: str) -> UserRecord:
        user = self.users[user_id]
        user.password_hash = hash_password(password)
        user.must_change_password = False
        return user

    async def create_paper(self, paper: PaperRecord) -> PaperRecord:
        duplicate = next(
            (
                item
                for item in self.papers.values()
                if item.owner_id == paper.owner_id and item.sha256 == paper.sha256
            ),
            None,
        )
        if duplicate:
            raise ValueError(f"文献已存在:{duplicate.id}")
        self.papers[paper.id] = paper
        job = JobRecord(id=str(uuid.uuid4()), paper_id=paper.id, type="parse_pdf")
        self.jobs[job.id] = job
        return paper

    async def mark_embedding_contract_stale(self, fingerprint: str | None) -> int:
        changed = 0
        for paper in self.papers.values():
            if paper.embedding_status == "ready" and (
                not fingerprint or paper.embedding_fingerprint != fingerprint
            ):
                paper.embedding_status = "stale"
                changed += 1
        return changed

    async def embedding_contract_counts(self, fingerprint: str | None) -> dict[str, int]:
        statuses = Counter(paper.embedding_status for paper in self.papers.values())
        return {
            "total": len(self.papers),
            "ready": statuses.get("ready", 0),
            "ready_current": sum(
                1
                for paper in self.papers.values()
                if paper.embedding_status == "ready"
                and fingerprint is not None
                and paper.embedding_fingerprint == fingerprint
            ),
            "stale": statuses.get("stale", 0),
            "unavailable": statuses.get("unavailable", 0),
            "failed": statuses.get("failed", 0),
        }

    async def list_papers(
        self,
        owner_id: str,
        collection_id: str | None = None,
        unfiled: bool = False,
    ) -> list[PaperRecord]:
        allowed_ids: set[str] | None = None
        if collection_id is not None:
            resolved = await self.resolve_collection_paper_ids(collection_id, owner_id)
            if resolved is None:
                return []
            allowed_ids = set(resolved)
        filed_ids = {paper_id for paper_id, _ in self.paper_collections} if unfiled else set()
        return sorted(
            (
                paper
                for paper in self.papers.values()
                if paper.owner_id == owner_id
                and paper.status != PaperStatus.deleting
                and (allowed_ids is None or paper.id in allowed_ids)
                and (not unfiled or paper.id not in filed_ids)
            ),
            key=lambda paper: paper.created_at,
            reverse=True,
        )

    async def get_latest_discovery_batch(self, user_id: str) -> DiscoveryBatchPage | None:
        batches = [item for item in self.discovery_batches.values() if item.user_id == user_id]
        if not batches:
            return None
        batch = max(batches, key=lambda item: (item.batch_number, item.created_at))
        items = sorted(
            (item for item in self.discovery_items.values() if item.batch_id == batch.id),
            key=lambda item: item.rank,
        )
        return batch, list(items)

    async def list_discovery_seen_arxiv_ids(self, user_id: str) -> set[str]:
        return {item.arxiv_id for item in self.discovery_items.values() if item.user_id == user_id}

    async def get_discovery_feedback_signals(
        self, user_id: str, *, limit: int = 20
    ) -> tuple[list[str], list[str]]:
        items = sorted(
            (
                item
                for item in self.discovery_items.values()
                if item.user_id == user_id and item.feedback in {"interested", "not_interested"}
            ),
            key=lambda item: item.feedback_at or item.created_at,
            reverse=True,
        )[:limit]
        positive = [
            f"{item.title} {item.abstract}" for item in items if item.feedback == "interested"
        ]
        negative = [
            f"{item.title} {item.abstract}" for item in items if item.feedback == "not_interested"
        ]
        return positive, negative

    async def create_discovery_batch(
        self, batch: DiscoveryBatchRecord, items: list[DiscoveryItemRecord]
    ) -> DiscoveryBatchPage:
        duplicate = next(
            (
                item
                for item in self.discovery_batches.values()
                if item.user_id == batch.user_id and item.batch_number == batch.batch_number
            ),
            None,
        )
        if duplicate:
            existing = await self.get_latest_discovery_batch(batch.user_id)
            if existing:
                return existing
        self.discovery_batches[batch.id] = batch
        for item in items:
            self.discovery_items[item.id] = item
        return batch, list(items)

    async def record_discovery_item_action(
        self, item_id: str, user_id: str, action: str, *, arxiv_id: str | None = None
    ) -> DiscoveryItemRecord | None:
        item = self.discovery_items.get(item_id)
        if not item or item.user_id != user_id or (arxiv_id and item.arxiv_id != arxiv_id):
            return None
        timestamp = now()
        if action == "opened":
            item.opened_at = item.opened_at or timestamp
        elif action in {"interested", "not_interested"}:
            item.feedback = action
            item.feedback_at = timestamp
        elif action == "imported":
            item.imported_at = item.imported_at or timestamp
        else:
            raise ValueError("不支持的推荐反馈动作")
        return item

    async def discovery_metrics(self, since: datetime) -> dict[str, int | float]:
        items = [item for item in self.discovery_items.values() if item.created_at >= since]
        batches = sum(item.created_at >= since for item in self.discovery_batches.values())
        impressions = len(items)
        opened = sum(item.opened_at is not None for item in items)
        interested = sum(item.feedback == "interested" for item in items)
        not_interested = sum(item.feedback == "not_interested" for item in items)
        imported = sum(item.imported_at is not None for item in items)
        feedback = interested + not_interested
        return _discovery_metric_report(
            batches, impressions, opened, interested, not_interested, imported, feedback
        )

    async def get_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None:
        paper = self.papers.get(paper_id)
        return paper if paper and paper.owner_id == owner_id else None

    async def get_owned_paper_page_text(
        self, paper_id: str, physical_page: int, owner_id: str
    ) -> str | None:
        if not await self.get_owned_paper(paper_id, owner_id):
            return None
        return self.paper_pages.get(paper_id, {}).get(physical_page)

    async def requeue_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None:
        paper = await self.get_owned_paper(paper_id, owner_id)
        if not paper or paper.status in {
            PaperStatus.queued,
            PaperStatus.extracting,
            PaperStatus.deleting,
        }:
            return None
        active = any(
            job.paper_id == paper.id
            and job.type == "parse_pdf"
            and job.status in {JobStatus.queued, JobStatus.running}
            for job in self.jobs.values()
        )
        if active:
            return None
        paper.status = PaperStatus.queued
        paper.updated_at = now()
        for translation in self.translations.values():
            if translation.paper_id == paper_id:
                translation.status = "failed"
                translation.error_code = "SOURCE_CHANGED"
                translation.error_message = "论文正在重新索引，既有译文已失效"
                translation.updated_at = now()
                for page in self.translation_pages.values():
                    if page.translation_id == translation.id:
                        page.status = "failed"
                        page.translated_text = None
                        page.error_code = "SOURCE_CHANGED"
                        page.error_message = "来源页面正在重新索引"
                        page.updated_at = now()
                for translate_job in self.jobs.values():
                    if translate_job.translation_id == translation.id and translate_job.status in {
                        JobStatus.queued,
                        JobStatus.running,
                    }:
                        translate_job.status = JobStatus.completed
                        translate_job.error_code = "SOURCE_CHANGED"
                        translate_job.error_message = "论文重新索引已终止旧翻译作业"
                        translate_job.updated_at = now()
        job = JobRecord(id=str(uuid.uuid4()), paper_id=paper.id, type="parse_pdf")
        self.jobs[job.id] = job
        return paper

    async def update_owned_paper(
        self, paper_id: str, owner_id: str, **changes: object
    ) -> PaperRecord | None:
        paper = await self.get_owned_paper(paper_id, owner_id)
        if not paper:
            return None
        for key in (
            "title",
            "authors",
            "year",
            "abstract",
            "doi",
            "publication",
            "status",
        ):
            if key in changes and changes[key] is not None:
                setattr(paper, key, changes[key])
        paper.updated_at = now()
        return paper

    async def get_owned_paper_artifact(
        self, paper_id: str, owner_id: str, artifact_type: str
    ) -> PaperArtifactRecord | None:
        return next(
            (
                item
                for item in self.paper_artifacts.values()
                if item.paper_id == paper_id
                and item.owner_id == owner_id
                and item.type == artifact_type
            ),
            None,
        )

    async def upsert_paper_artifact(
        self,
        paper_id: str,
        owner_id: str,
        artifact_type: str,
        source_revision_value: str,
        status: str,
        fallback_reason: str | None,
        structured_payload: dict,
        markdown: str,
    ) -> PaperArtifactRecord | None:
        if not await self.get_owned_paper(paper_id, owner_id):
            return None
        record = await self.get_owned_paper_artifact(paper_id, owner_id, artifact_type)
        if record is None:
            record = PaperArtifactRecord(
                id=str(uuid.uuid4()),
                paper_id=paper_id,
                owner_id=owner_id,
                type=artifact_type,
                source_revision=source_revision_value,
                status=status,
                fallback_reason=fallback_reason,
                structured_payload=dict(structured_payload),
                markdown=markdown,
            )
            self.paper_artifacts[record.id] = record
        else:
            record.source_revision = source_revision_value
            record.status = status
            record.fallback_reason = fallback_reason
            record.structured_payload = dict(structured_payload)
            record.markdown = markdown
            record.updated_at = now()
        return record

    async def get_active_paper_artifact_job(
        self, paper_id: str, owner_id: str, artifact_type: str
    ) -> JobRecord | None:
        if artifact_type not in ARTIFACT_JOB_TYPES:
            return None
        if not await self.get_owned_paper(paper_id, owner_id):
            return None
        job_type = ARTIFACT_JOB_TYPES[artifact_type]
        return next(
            (
                job
                for job in self.jobs.values()
                if job.paper_id == paper_id
                and job.type == job_type
                and job.status in {JobStatus.queued, JobStatus.running}
            ),
            None,
        )

    async def enqueue_paper_artifact(
        self,
        paper_id: str,
        owner_id: str,
        artifact_type: str,
        source_revision_value: str,
        *,
        preserve_existing: bool,
    ) -> JobRecord | None:
        if artifact_type not in ARTIFACT_JOB_TYPES:
            return None
        if not await self.get_owned_paper(paper_id, owner_id):
            return None
        active = await self.get_active_paper_artifact_job(paper_id, owner_id, artifact_type)
        if active:
            return active
        if not preserve_existing:
            await self.upsert_paper_artifact(
                paper_id,
                owner_id,
                artifact_type,
                source_revision_value,
                "processing",
                None,
                {},
                "",
            )
        job = JobRecord(
            id=str(uuid.uuid4()),
            paper_id=paper_id,
            type=ARTIFACT_JOB_TYPES[artifact_type],
            max_attempts=2,
        )
        self.jobs[job.id] = job
        return job

    async def mark_paper_artifacts_stale(self, paper_id: str) -> None:
        for record in self.paper_artifacts.values():
            if record.paper_id == paper_id:
                record.status = "stale"
                record.updated_at = now()

    async def delete_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None:
        paper = await self.get_owned_paper(paper_id, owner_id)
        if not paper:
            return None
        paper.status = PaperStatus.deleting
        paper.updated_at = now()
        for translation in self.translations.values():
            if translation.paper_id == paper_id and translation.status != "completed":
                await self.cancel_owned_translation(paper_id, translation.id, owner_id)
        has_delete_job = any(
            job.paper_id == paper.id
            and job.type == "delete_paper"
            and job.status in {JobStatus.queued, JobStatus.running}
            for job in self.jobs.values()
        )
        if not has_delete_job:
            job = JobRecord(id=str(uuid.uuid4()), paper_id=paper.id, type="delete_paper")
            self.jobs[job.id] = job
        return paper

    async def touch_paper_opened(self, paper_id: str, owner_id: str) -> PaperRecord | None:
        paper = await self.get_owned_paper(paper_id, owner_id)
        if not paper:
            return None
        paper.last_opened_at = now()
        paper.updated_at = now()
        return paper

    async def set_papers_archived(
        self, paper_ids: list[str], owner_id: str, archived: bool
    ) -> list[str] | None:
        unique_ids = list(dict.fromkeys(paper_ids))
        papers = [self.papers.get(paper_id) for paper_id in unique_ids]
        if any(not paper or paper.owner_id != owner_id for paper in papers):
            return None
        timestamp = now() if archived else None
        for paper in papers:
            assert paper is not None
            paper.archived_at = timestamp
            paper.updated_at = now()
        return unique_ids

    async def count_active_admins(self) -> int:
        return sum(1 for user in self.users.values() if user.active and user.role == UserRole.admin)

    async def create_collection(
        self,
        owner_id: str,
        name: str,
        description: str | None,
        parent_id: str | None = None,
    ) -> CollectionRecord:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("集合名称不能为空")
        _validate_collection_change(
            list(self.collections.values()),
            owner_id=owner_id,
            name=normalized_name,
            parent_id=parent_id,
        )
        record = CollectionRecord(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            name=normalized_name,
            description=description,
            parent_id=parent_id,
        )
        self.collections[record.id] = record
        return record

    async def list_collections(self, owner_id: str) -> list[CollectionRecord]:
        return [item for item in self.collections.values() if item.owner_id == owner_id]

    async def list_collection_memberships(self, owner_id: str) -> dict[str, list[str]]:
        owned = {item.id for item in self.collections.values() if item.owner_id == owner_id}
        memberships = {collection_id: [] for collection_id in owned}
        for paper_id, collection_id in sorted(self.paper_collections):
            if collection_id in owned:
                memberships[collection_id].append(paper_id)
        return memberships

    async def resolve_collection_paper_ids(
        self, collection_id: str, owner_id: str, *, ready_only: bool = False
    ) -> list[str] | None:
        root = self.collections.get(collection_id)
        if not root or root.owner_id != owner_id:
            return None
        descendants = {collection_id}
        pending = [collection_id]
        while pending:
            current = pending.pop()
            child_ids = [
                item.id
                for item in self.collections.values()
                if item.owner_id == owner_id
                and item.parent_id == current
                and item.id not in descendants
            ]
            descendants.update(child_ids)
            pending.extend(child_ids)
        paper_ids = {
            paper_id
            for paper_id, assigned_collection_id in self.paper_collections
            if assigned_collection_id in descendants
        }
        if ready_only:
            paper_ids = {
                paper_id
                for paper_id in paper_ids
                if (paper := self.papers.get(paper_id))
                and paper.owner_id == owner_id
                and paper.status == PaperStatus.ready
            }
        return sorted(paper_ids)

    async def update_collection(
        self, collection_id: str, owner_id: str, **changes: object
    ) -> CollectionRecord | None:
        record = self.collections.get(collection_id)
        if not record or record.owner_id != owner_id:
            return None
        if "name" in changes:
            normalized_name = str(changes["name"]).strip()
            if not normalized_name:
                raise ValueError("集合名称不能为空")
            changes["name"] = normalized_name
        proposed_name = str(changes.get("name", record.name))
        proposed_parent_id = changes["parent_id"] if "parent_id" in changes else record.parent_id
        if proposed_parent_id is not None and not isinstance(proposed_parent_id, str):
            raise ValueError("父集合无效")
        _validate_collection_change(
            list(self.collections.values()),
            owner_id=owner_id,
            name=proposed_name,
            parent_id=proposed_parent_id,
            collection_id=record.id,
        )
        for key in ("name", "description", "parent_id"):
            if key in changes:
                setattr(record, key, changes[key])
        record.updated_at = now()
        return record

    async def delete_collection(self, collection_id: str, owner_id: str) -> bool:
        record = self.collections.get(collection_id)
        if not record or record.owner_id != owner_id:
            return False
        children = [
            item
            for item in self.collections.values()
            if item.owner_id == owner_id and item.parent_id == collection_id
        ]
        siblings = [
            item
            for item in self.collections.values()
            if item.owner_id == owner_id
            and item.parent_id == record.parent_id
            and item.id != record.id
        ]
        if any(
            child.name.casefold() == sibling.name.casefold()
            for child in children
            for sibling in siblings
        ):
            raise ValueError("子集合提升后会与同级集合重名，请先重命名")
        for child in children:
            child.parent_id = record.parent_id
            child.updated_at = now()
        del self.collections[collection_id]
        self.paper_collections = {
            pair for pair in self.paper_collections if pair[1] != collection_id
        }
        return True

    async def set_paper_collection(
        self, collection_id: str, paper_id: str, owner_id: str, assigned: bool
    ) -> bool:
        collection = self.collections.get(collection_id)
        paper = await self.get_owned_paper(paper_id, owner_id)
        if not collection or collection.owner_id != owner_id or not paper:
            return False
        pair = (paper_id, collection_id)
        self.paper_collections.add(pair) if assigned else self.paper_collections.discard(pair)
        return True

    async def create_or_resume_translation(
        self,
        paper_id: str,
        owner_id: str,
        target_language: str,
        priority_page: int | None,
        *,
        model_available: bool,
        refresh: bool = False,
    ) -> TranslationRecord | None:
        paper = await self.get_owned_paper(paper_id, owner_id)
        if not paper:
            return None
        if paper.status not in {PaperStatus.ready, PaperStatus.partial}:
            raise TranslationSourceUnavailableError("文献尚未完成页面解析")
        page_items = sorted(self.paper_pages.get(paper_id, {}).items())
        if not page_items:
            raise TranslationSourceUnavailableError("文献尚未完成页面解析")
        page_numbers = {number for number, _ in page_items}
        if priority_page is not None and priority_page not in page_numbers:
            raise ValueError("优先翻译页不存在")
        revision = source_revision(page_items)
        translation = next(
            (
                item
                for item in self.translations.values()
                if item.paper_id == paper_id and item.target_language == target_language
            ),
            None,
        )
        if translation is None:
            translation_created = True
            restart_requested = False
            source_changed = False
            translation = TranslationRecord(
                id=str(uuid.uuid4()),
                paper_id=paper_id,
                owner_id=owner_id,
                target_language=target_language,
                source_revision=revision,
                status="queued",
                total_pages=len(page_items),
                priority_page=priority_page,
            )
            self.translations[translation.id] = translation
        else:
            translation_created = False
            source_changed = (
                translation.error_code == "SOURCE_CHANGED"
                or translation.source_revision != revision
            )
            restart_requested = (
                refresh
                or source_changed
                or translation.cancel_requested
                or (translation.status in {"cancelled", "failed", "partial"})
            )
            translation.source_revision = revision
            translation.priority_page = priority_page
            if restart_requested:
                translation.cancel_requested = False
                translation.error_code = None
                translation.error_message = None
                translation.updated_at = now()

        existing = {
            item.physical_page: item
            for item in self.translation_pages.values()
            if item.translation_id == translation.id
        }
        for page_number, text in page_items:
            text_hash = source_text_hash(text)
            page = existing.pop(page_number, None)
            initial_status = "queued" if text.strip() else "no_text"
            if page is None:
                page = TranslationPageRecord(
                    id=str(uuid.uuid4()),
                    translation_id=translation.id,
                    physical_page=page_number,
                    status=initial_status,
                    source_text_hash=text_hash,
                )
                self.translation_pages[page.id] = page
            elif refresh or source_changed or page.source_text_hash != text_hash:
                page.source_text_hash = text_hash
                page.status = initial_status
                page.translated_text = None
                page.attempts = 0
                page.error_code = None
                page.error_message = None
            elif restart_requested and page.status not in {"completed", "no_text"}:
                page.status = initial_status
                page.translated_text = None
                page.attempts = 0
                page.error_code = None
                page.error_message = None
            page.priority = 0 if page_number == priority_page else 1000 + page_number
            page.updated_at = now()
        for stale in existing.values():
            self.translation_pages.pop(stale.id, None)

        pages = [
            item
            for item in self.translation_pages.values()
            if item.translation_id == translation.id
        ]
        translation.total_pages = len(pages)
        translation.completed_pages = sum(item.status == "completed" for item in pages)
        translation.failed_pages = sum(item.status == "failed" for item in pages)
        queued = [item for item in pages if item.status == "queued"]
        running = [item for item in pages if item.status == "running"]
        translation_job = next(
            (job for job in self.jobs.values() if job.translation_id == translation.id),
            None,
        )
        if translation_job is None:
            translation_job_created = True
            translation_job = JobRecord(
                id=str(uuid.uuid4()),
                paper_id=paper_id,
                translation_id=translation.id,
                type="translate_paper",
            )
            self.jobs[translation_job.id] = translation_job
        elif restart_requested:
            translation_job_created = False
            # 内存仓库没有 Worker token，但仍复用唯一 Job 并重置执行代次。
            translation_job.status = JobStatus.queued
            translation_job.progress = 0
            translation_job.attempts = 0
            translation_job.error_code = None
            translation_job.error_message = None
            translation_job.available_at = now()
            translation_job.claimed_at = None
            translation_job.claim_token = None
        else:
            translation_job_created = False
        preserve_active_schedule = (
            not translation_created
            and not translation_job_created
            and not restart_requested
            and translation.status in {"queued", "running"}
            and translation_job.status in {JobStatus.queued, JobStatus.running}
        )
        if preserve_active_schedule:
            # 自动退避或正在执行只是幂等查询，不能借 POST 绕过 attempts/available_at。
            return translation
        if queued and not model_available:
            for page in queued:
                page.status = "failed"
                page.error_code = "MODEL_NOT_CONFIGURED"
                page.error_message = "尚未配置可用于全文翻译的模型"
            translation.completed_pages = sum(item.status == "completed" for item in pages)
            translation.failed_pages = sum(item.status == "failed" for item in pages)
            translation.status = "partial" if translation.completed_pages else "failed"
            translation.error_code = "MODEL_NOT_CONFIGURED"
            translation.error_message = "尚未配置可用于全文翻译的模型"
            translation_job.status = JobStatus.failed
            translation_job.error_code = "MODEL_NOT_CONFIGURED"
            translation_job.error_message = "尚未配置可用于全文翻译的模型"
        elif queued:
            translation.status = "running" if running else "queued"
            if translation_job.status != JobStatus.running and (
                translation_job_created or restart_requested
            ):
                translation_job.status = JobStatus.queued
                translation_job.progress = 0
                translation_job.attempts = 0
                translation_job.error_code = None
                translation_job.error_message = None
        elif running:
            translation.status = "running"
        elif all(item.status == "no_text" for item in pages):
            translation.status = "completed"
            translation.error_code = "NO_TRANSLATABLE_TEXT"
            translation.error_message = "此文献暂无可翻译的页面文本"
            translation_job.status = JobStatus.completed
            translation_job.progress = 100
            translation_job.error_code = "NO_TRANSLATABLE_TEXT"
            translation_job.error_message = "此文献暂无可翻译的页面文本"
        elif translation.failed_pages:
            translation.status = "partial" if translation.completed_pages else "failed"
            translation_job.status = JobStatus.failed
        else:
            translation.status = "completed"
            translation_job.status = JobStatus.completed
            translation_job.progress = 100
            translation_job.error_code = None
            translation_job.error_message = None
        translation_job.updated_at = now()
        return translation

    async def get_owned_translation(
        self, paper_id: str, translation_id: str, owner_id: str
    ) -> TranslationRecord | None:
        record = self.translations.get(translation_id)
        return (
            record
            if record and record.paper_id == paper_id and record.owner_id == owner_id
            else None
        )

    async def list_translation_pages(
        self, translation_id: str, owner_id: str
    ) -> list[TranslationPageRecord]:
        translation = self.translations.get(translation_id)
        if not translation or translation.owner_id != owner_id:
            return []
        return sorted(
            (
                page
                for page in self.translation_pages.values()
                if page.translation_id == translation_id
            ),
            key=lambda page: page.physical_page,
        )

    async def get_owned_translation_page(
        self, paper_id: str, translation_id: str, physical_page: int, owner_id: str
    ) -> TranslationPageRecord | None:
        translation = await self.get_owned_translation(paper_id, translation_id, owner_id)
        if not translation:
            return None
        return next(
            (
                page
                for page in self.translation_pages.values()
                if page.translation_id == translation_id and page.physical_page == physical_page
            ),
            None,
        )

    async def cancel_owned_translation(
        self, paper_id: str, translation_id: str, owner_id: str
    ) -> TranslationRecord | None:
        translation = await self.get_owned_translation(paper_id, translation_id, owner_id)
        if not translation:
            return None
        if translation.status != "completed":
            translation.cancel_requested = True
            translation.status = "cancelled"
            translation.error_code = "TRANSLATION_CANCELLED"
            translation.error_message = "全文翻译已取消"
            translation.updated_at = now()
            for page in await self.list_translation_pages(translation_id, owner_id):
                if page.status not in {"completed", "no_text"}:
                    page.status = "cancelled"
                    page.error_code = "TRANSLATION_CANCELLED"
                    page.error_message = "全文翻译已取消"
                    page.updated_at = now()
            for job in self.jobs.values():
                if job.translation_id == translation_id and job.status in {
                    JobStatus.queued,
                    JobStatus.running,
                }:
                    job.status = JobStatus.completed
                    job.error_code = "TRANSLATION_CANCELLED"
                    job.error_message = "用户已取消全文翻译"
                    job.updated_at = now()
        return translation

    async def list_jobs(self) -> list[JobRecord]:
        return sorted(self.jobs.values(), key=lambda item: item.created_at, reverse=True)

    async def retry_job(self, job_id: str) -> JobRecord | None:
        job = self.jobs.get(job_id)
        if not job or job.status != JobStatus.failed:
            return None
        translation: TranslationRecord | None = None
        if job.type == "translate_paper" and job.translation_id:
            translation = self.translations.get(job.translation_id)
            paper = self.papers.get(job.paper_id or "")
            page_items = sorted(self.paper_pages.get(job.paper_id or "", {}).items())
            if (
                not translation
                or not paper
                or paper.status not in {PaperStatus.ready, PaperStatus.partial}
                or translation.cancel_requested
                or translation.error_code == "SOURCE_CHANGED"
                or not page_items
                or source_revision(page_items) != translation.source_revision
            ):
                return None
        job.status = JobStatus.queued
        job.progress = 0
        job.attempts = 0
        job.error_code = None
        job.error_message = None
        job.available_at = now()
        job.claimed_at = None
        job.claim_token = None
        job.updated_at = now()
        if job.paper_id and job.type in ARTIFACT_JOB_TYPES.values():
            artifact_type = next(
                key for key, value in ARTIFACT_JOB_TYPES.items() if value == job.type
            )
            paper = self.papers.get(job.paper_id)
            artifact = (
                await self.get_owned_paper_artifact(job.paper_id, paper.owner_id, artifact_type)
                if paper
                else None
            )
            if artifact and artifact.status != "ready":
                artifact.status = "processing"
                artifact.fallback_reason = None
                artifact.structured_payload = {}
                artifact.markdown = ""
                artifact.updated_at = now()
        if job.type == "translate_paper" and job.translation_id:
            if translation:
                translation.status = "queued"
                translation.error_code = None
                translation.error_message = None
                translation.updated_at = now()
            for page in self.translation_pages.values():
                if page.translation_id == job.translation_id and page.status in {
                    "failed",
                    "cancelled",
                }:
                    page.status = "queued"
                    page.attempts = 0
                    page.error_code = None
                    page.error_message = None
                    page.updated_at = now()
            if translation:
                translation.failed_pages = sum(
                    page.status == "failed"
                    for page in self.translation_pages.values()
                    if page.translation_id == job.translation_id
                )
        return job

    def _chat_session_with_current_run(self, record: ChatSessionRecord) -> ChatSessionRecord:
        runs = sorted(
            (
                item
                for item in self.agent_runs.values()
                if item.session_id == record.id and item.user_id == record.user_id
            ),
            key=lambda item: item.created_at,
            reverse=True,
        )
        record.current_run_id = runs[0].id if runs else None
        record.current_run_status = runs[0].status if runs else None
        return record

    async def create_chat_session(
        self,
        user_id: str,
        title: str,
        session_type: str,
        paper_id: str | None,
        collection_id: str | None,
    ) -> ChatSessionRecord:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("会话标题不能为空")
        record = ChatSessionRecord(
            id=str(uuid.uuid4()),
            user_id=user_id,
            title=normalized_title,
            type=session_type,
            paper_id=paper_id,
            collection_id=collection_id,
        )
        self.chat_sessions[record.id] = record
        return record

    async def list_chat_sessions(self, user_id: str) -> list[ChatSessionRecord]:
        return [
            self._chat_session_with_current_run(item)
            for item in sorted(
                (item for item in self.chat_sessions.values() if item.user_id == user_id),
                key=lambda item: item.updated_at,
                reverse=True,
            )
        ]

    async def get_owned_chat_session(
        self, session_id: str, user_id: str
    ) -> ChatSessionRecord | None:
        record = self.chat_sessions.get(session_id)
        return (
            self._chat_session_with_current_run(record)
            if record and record.user_id == user_id
            else None
        )

    async def update_owned_chat_session(
        self, session_id: str, user_id: str, title: str
    ) -> ChatSessionRecord | None:
        record = await self.get_owned_chat_session(session_id, user_id)
        if not record:
            return None
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("会话标题不能为空")
        record.title = normalized_title
        record.updated_at = now()
        return record

    async def list_session_thread_ids(self, session_id: str, user_id: str) -> list[str] | None:
        if not await self.get_owned_chat_session(session_id, user_id):
            return None
        return [
            item.thread_id
            for item in self.agent_runs.values()
            if item.session_id == session_id and item.user_id == user_id
        ]

    async def delete_owned_chat_session(self, session_id: str, user_id: str) -> bool:
        record = await self.get_owned_chat_session(session_id, user_id)
        if not record:
            return False
        if record.current_run_status in {"pending", "running", "interrupted"}:
            raise ChatActiveRunError("会话仍有运行中或等待确认的任务，请先取消")
        run_ids = {
            item.id
            for item in self.agent_runs.values()
            if item.session_id == session_id and item.user_id == user_id
        }
        self.jobs = {
            key: item for key, item in self.jobs.items() if item.agent_run_id not in run_ids
        }
        self.agent_run_events = {
            key: item for key, item in self.agent_run_events.items() if item.run_id not in run_ids
        }
        for run_id in run_ids:
            self.agent_runs.pop(run_id, None)
        self.chat_messages = {
            key: item for key, item in self.chat_messages.items() if item.session_id != session_id
        }
        self.chat_sessions.pop(session_id, None)
        return True

    async def list_chat_messages(
        self, session_id: str, user_id: str
    ) -> list[ChatMessageRecord] | None:
        if not await self.get_owned_chat_session(session_id, user_id):
            return None
        return sorted(
            (item for item in self.chat_messages.values() if item.session_id == session_id),
            key=lambda item: item.sequence,
        )

    async def update_session_compaction(
        self,
        session_id: str,
        user_id: str,
        *,
        compact_summary: dict,
        compacted_through_message_id: str | None,
        entity_state: dict,
    ) -> ChatSessionRecord | None:
        record = await self.get_owned_chat_session(session_id, user_id)
        if not record:
            return None
        record.compact_summary = dict(compact_summary)
        record.summary_version = 1
        record.compacted_through_message_id = compacted_through_message_id
        record.entity_state = dict(entity_state)
        record.updated_at = now()
        return record

    async def list_memories(
        self, user_id: str, *, enabled_only: bool = False
    ) -> list[MemoryItemRecord]:
        records = [item for item in self.memory_items.values() if item.user_id == user_id]
        if enabled_only:
            records = [item for item in records if item.enabled]
        return sorted(records, key=lambda item: (not item.pinned, -item.updated_at.timestamp()))

    async def create_memory_item(self, record: MemoryItemRecord) -> MemoryItemRecord:
        existing = next(
            (
                item
                for item in self.memory_items.values()
                if item.user_id == record.user_id and item.normalized_hash == record.normalized_hash
            ),
            None,
        )
        if existing:
            existing.enabled = True
            existing.confidence = max(existing.confidence, record.confidence)
            existing.pinned = existing.pinned or record.pinned
            existing.updated_at = now()
            return existing
        active_count = sum(
            item.user_id == record.user_id and item.enabled for item in self.memory_items.values()
        )
        if active_count >= 200:
            raise ValueError("长期记忆已达到 200 条上限")
        self.memory_items[record.id] = record
        version = MemoryItemVersionRecord(
            id=str(uuid.uuid4()),
            memory_item_id=record.id,
            version=1,
            value=record.value,
            confidence=record.confidence,
            status="active",
            source_kind=record.source_kind,
            source_excerpt=record.source_excerpt,
        )
        self.memory_item_versions[version.id] = version
        return record

    async def update_owned_memory(
        self, memory_id: str, user_id: str, **changes: object
    ) -> MemoryItemRecord | None:
        record = self.memory_items.get(memory_id)
        if not record or record.user_id != user_id:
            return None
        if "normalized_hash" in changes:
            collision = next(
                (
                    item
                    for item in self.memory_items.values()
                    if item.id != memory_id
                    and item.user_id == user_id
                    and item.normalized_hash == changes["normalized_hash"]
                ),
                None,
            )
            if collision:
                raise ValueError("相同记忆已经存在")
        content_changed = any(key in changes for key in ("value", "confidence", "type"))
        if content_changed:
            versions = [
                item
                for item in self.memory_item_versions.values()
                if item.memory_item_id == memory_id
            ]
            for item in versions:
                if item.status == "active":
                    item.status = "superseded"
            next_version = max((item.version for item in versions), default=0) + 1
            version = MemoryItemVersionRecord(
                id=str(uuid.uuid4()),
                memory_item_id=record.id,
                version=next_version,
                value=str(changes.get("value", record.value)),
                confidence=float(changes.get("confidence", record.confidence)),
                status="active",
                source_kind="user_edit",
                source_excerpt=record.source_excerpt,
            )
            self.memory_item_versions[version.id] = version
        if any(key in changes for key in ("value", "type")):
            # 正文变化后绝不复用旧向量；调用方只有在新向量成功时才显式传入。
            record.embedding = changes.get("embedding")  # type: ignore[assignment]
            record.embedding_fingerprint = changes.get(  # type: ignore[assignment]
                "embedding_fingerprint"
            )
        for key in (
            "type",
            "value",
            "normalized_hash",
            "confidence",
            "pinned",
            "enabled",
            "embedding",
            "embedding_fingerprint",
        ):
            if key in changes:
                setattr(record, key, changes[key])
        record.updated_at = now()
        return record

    async def delete_owned_memory(self, memory_id: str, user_id: str) -> bool:
        record = self.memory_items.get(memory_id)
        if not record or record.user_id != user_id:
            return False
        self.memory_items.pop(memory_id, None)
        self.memory_item_versions = {
            key: value
            for key, value in self.memory_item_versions.items()
            if value.memory_item_id != memory_id
        }
        return True

    async def clear_memories(self, user_id: str) -> int:
        ids = {item.id for item in self.memory_items.values() if item.user_id == user_id}
        self.memory_items = {
            key: value for key, value in self.memory_items.items() if value.user_id != user_id
        }
        self.memory_item_versions = {
            key: value
            for key, value in self.memory_item_versions.items()
            if value.memory_item_id not in ids
        }
        return len(ids)

    async def submit_chat_message(
        self,
        session_id: str,
        user_id: str,
        content: str,
        client_message_id: str,
        request_hash: str,
        scope_snapshot: dict,
    ) -> ChatSubmission | None:
        chat_session = await self.get_owned_chat_session(session_id, user_id)
        if not chat_session:
            return None
        existing = next(
            (
                item
                for item in self.chat_messages.values()
                if item.session_id == session_id and item.client_message_id == client_message_id
            ),
            None,
        )
        if existing:
            if existing.request_hash != request_hash:
                raise ChatIdempotencyConflictError("客户端消息 ID 已用于不同请求")
            run = self.agent_runs.get(existing.run_id or "")
            if not run:
                raise RuntimeError("幂等消息关联的 Agent Run 不存在")
            return ChatSubmission(existing, run, True)
        if any(
            item.session_id == session_id and item.status in {"pending", "running", "interrupted"}
            for item in self.agent_runs.values()
        ):
            raise ChatActiveRunError("当前会话已有正在运行或等待确认的任务")
        next_sequence = 1 + max(
            (
                item.sequence
                for item in self.chat_messages.values()
                if item.session_id == session_id
            ),
            default=0,
        )
        run_id = str(uuid.uuid4())
        user_message = ChatMessageRecord(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role="user",
            sequence=next_sequence,
            status="completed",
            content=content,
            run_id=run_id,
            client_message_id=client_message_id,
            request_hash=request_hash,
        )
        assistant_message = ChatMessageRecord(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role="assistant",
            sequence=next_sequence + 1,
            status="pending",
            content="",
            run_id=run_id,
        )
        run = AgentRunRecord(
            id=run_id,
            user_id=user_id,
            session_id=session_id,
            thread_id=f"{user_id}:{session_id}:{run_id}",
            scope_snapshot=dict(scope_snapshot),
            orchestration_version=str(
                scope_snapshot.get("orchestration_version", "single_agent_v1")
            ),
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            request_hash=request_hash,
        )
        job = JobRecord(
            id=str(uuid.uuid4()),
            paper_id=None,
            agent_run_id=run_id,
            type="agent_run",
        )
        self.chat_messages[user_message.id] = user_message
        self.chat_messages[assistant_message.id] = assistant_message
        self.agent_runs[run.id] = run
        self.jobs[job.id] = job
        if chat_session.title == "新会话":
            chat_session.title = content.strip().replace("\n", " ")[:60]
        chat_session.updated_at = now()
        return ChatSubmission(user_message, run, False)

    async def create_agent_run(
        self, run_id: str, user_id: str, session_id: str, thread_id: str
    ) -> AgentRunRecord:
        record = AgentRunRecord(run_id, user_id, session_id, thread_id)
        self.agent_runs[run_id] = record
        return record

    async def get_owned_agent_run(self, run_id: str, user_id: str) -> AgentRunRecord | None:
        record = self.agent_runs.get(run_id)
        return record if record and record.user_id == user_id else None

    async def get_agent_run(self, run_id: str) -> AgentRunRecord | None:
        return self.agent_runs.get(run_id)

    async def list_agent_runs_for_observability(
        self, since: datetime, *, limit: int = 5000
    ) -> list[AgentRunRecord]:
        return sorted(
            (item for item in self.agent_runs.values() if item.created_at >= since),
            key=lambda item: item.created_at,
            reverse=True,
        )[:limit]

    async def get_agent_run_input(self, run_id: str) -> tuple[AgentRunRecord, str] | None:
        run = self.agent_runs.get(run_id)
        if not run or not run.user_message_id:
            return None
        message = self.chat_messages.get(run.user_message_id)
        return (run, message.content) if message else None

    async def update_owned_agent_run(
        self, run_id: str, user_id: str, **changes: object
    ) -> AgentRunRecord | None:
        record = await self.get_owned_agent_run(run_id, user_id)
        if not record:
            return None
        for key in (
            "status",
            "tool_steps",
            "duration_ms",
            "token_usage",
            "result_summary",
            "pending_action",
            "error_code",
        ):
            if key in changes:
                setattr(record, key, changes[key])
        record.updated_at = now()
        return record

    async def append_agent_run_event(
        self,
        run_id: str,
        event: str,
        data: dict,
        *,
        event_key: str | None = None,
        claim_token: str | None = None,
    ) -> AgentRunEventRecord | None:
        if run_id not in self.agent_runs:
            return None
        if claim_token is not None and not self._agent_claim_is_current(run_id, claim_token):
            return None
        if event_key:
            existing = next(
                (
                    item
                    for item in self.agent_run_events.values()
                    if item.run_id == run_id and item.event_key == event_key
                ),
                None,
            )
            if existing:
                return existing
        sequence = 1 + max(
            (item.sequence for item in self.agent_run_events.values() if item.run_id == run_id),
            default=0,
        )
        record = AgentRunEventRecord(
            id=self._next_agent_event_id,
            run_id=run_id,
            sequence=sequence,
            event=event,
            data=dict(data),
            event_key=event_key,
        )
        self._next_agent_event_id += 1
        self.agent_run_events[record.id] = record
        return record

    async def list_owned_agent_run_events(
        self, run_id: str, user_id: str, after_sequence: int = 0
    ) -> list[AgentRunEventRecord] | None:
        if not await self.get_owned_agent_run(run_id, user_id):
            return None
        return sorted(
            (
                item
                for item in self.agent_run_events.values()
                if item.run_id == run_id and item.sequence > after_sequence
            ),
            key=lambda item: item.sequence,
        )

    def _agent_claim_is_current(self, run_id: str, claim_token: str) -> bool:
        job = next((item for item in self.jobs.values() if item.agent_run_id == run_id), None)
        return bool(
            job
            and job.status == JobStatus.running
            and job.claim_token == claim_token
            and job.claimed_at
            and job.claimed_at >= now() - AGENT_JOB_LEASE
        )

    async def is_agent_claim_current(self, run_id: str, claim_token: str) -> bool:
        return self._agent_claim_is_current(run_id, claim_token)

    async def claim_agent_run_job(self, run_id: str) -> str | None:
        job = next((item for item in self.jobs.values() if item.agent_run_id == run_id), None)
        if not job or job.status != JobStatus.queued or job.available_at > now():
            return None
        token = str(uuid.uuid4())
        job.status = JobStatus.running
        job.attempts += 1
        job.claim_token = token
        job.claimed_at = now()
        job.updated_at = now()
        return token

    async def start_agent_run(self, run_id: str, claim_token: str) -> AgentRunRecord | None:
        run = self.agent_runs.get(run_id)
        if (
            not run
            or not self._agent_claim_is_current(run_id, claim_token)
            or run.cancel_requested
            or run.status == "cancelled"
        ):
            return None
        if run.status == "pending":
            run.status = "running"
            run.updated_at = now()
        assistant = self.chat_messages.get(run.assistant_message_id or "")
        if assistant and assistant.status == "pending":
            assistant.status = "streaming"
            assistant.updated_at = now()
        await self.append_agent_run_event(
            run_id,
            "run_started",
            {"status": "running"},
            event_key="run_started",
            claim_token=claim_token,
        )
        return run

    async def update_agent_context(
        self,
        run_id: str,
        claim_token: str,
        *,
        context_snapshot: dict,
        resolved_query: str,
        reference_confidence: float,
    ) -> AgentRunRecord | None:
        run = self.agent_runs.get(run_id)
        if not run or not self._agent_claim_is_current(run_id, claim_token):
            return None
        run.context_snapshot = dict(context_snapshot)
        run.context_version = int(context_snapshot.get("version", 1))
        run.resolved_query = resolved_query
        run.reference_confidence = reference_confidence
        run.updated_at = now()
        return run

    async def update_agent_skill(
        self,
        run_id: str,
        claim_token: str,
        *,
        selected_skill: str,
        skill_version: int,
        harness_trace: dict,
    ) -> AgentRunRecord | None:
        run = self.agent_runs.get(run_id)
        if not run or not self._agent_claim_is_current(run_id, claim_token):
            return None
        run.selected_skill = selected_skill
        run.skill_version = skill_version
        run.harness_trace = dict(harness_trace)
        run.updated_at = now()
        return run

    async def start_agent_tool_call(
        self,
        record: AgentToolCallRecord,
        claim_token: str,
    ) -> AgentToolCallRecord | None:
        if not self._agent_claim_is_current(record.run_id, claim_token):
            return None
        duplicate = next(
            (
                item
                for item in self.agent_tool_calls.values()
                if item.run_id == record.run_id and item.call_id == record.call_id
            ),
            None,
        )
        if duplicate:
            return duplicate
        self.agent_tool_calls[record.id] = record
        return record

    async def finish_agent_tool_call(
        self,
        tool_call_id: str,
        run_id: str,
        claim_token: str,
        *,
        status: str,
        attempt: int,
        duration_ms: int,
        result_preview: dict | None,
        error_code: str | None,
    ) -> AgentToolCallRecord | None:
        if not self._agent_claim_is_current(run_id, claim_token):
            return None
        record = self.agent_tool_calls.get(tool_call_id)
        if not record or record.run_id != run_id:
            return None
        record.status = status
        record.attempt = attempt
        record.duration_ms = duration_ms
        record.result_preview = result_preview
        record.error_code = error_code
        record.updated_at = now()
        return record

    async def create_agent_tool_artifact(
        self,
        record: AgentToolArtifactRecord,
        claim_token: str,
    ) -> AgentToolArtifactRecord | None:
        tool_call = self.agent_tool_calls.get(record.tool_call_id)
        if not tool_call or not self._agent_claim_is_current(tool_call.run_id, claim_token):
            return None
        self.agent_tool_artifacts[record.id] = record
        return record

    async def list_agent_tool_calls_for_observability(
        self, since: datetime, *, limit: int = 10000
    ) -> list[AgentToolCallRecord]:
        return sorted(
            (item for item in self.agent_tool_calls.values() if item.created_at >= since),
            key=lambda item: item.created_at,
            reverse=True,
        )[:limit]

    async def memory_observability_counts(self) -> dict[str, object]:
        records = list(self.memory_items.values())
        types: dict[str, int] = {}
        sources: dict[str, int] = {}
        for item in records:
            types[item.type] = types.get(item.type, 0) + 1
            sources[item.source_kind] = sources.get(item.source_kind, 0) + 1
        users = len({item.user_id for item in records})
        return {
            "total": len(records),
            "active": sum(1 for item in records if item.enabled),
            "disabled": sum(1 for item in records if not item.enabled),
            "pinned": sum(1 for item in records if item.pinned),
            "users_with_memory": users,
            "capacity": users * 200,
            "superseded_versions": sum(
                1 for item in self.memory_item_versions.values() if item.status == "superseded"
            ),
            "types": types,
            "sources": sources,
        }

    async def ensure_mcp_server_config(
        self, record: McpServerConfigRecord
    ) -> McpServerConfigRecord:
        existing = self.mcp_server_configs.get(record.id)
        if existing:
            return existing
        self.mcp_server_configs[record.id] = record
        return record

    async def list_mcp_server_configs(self) -> list[McpServerConfigRecord]:
        return sorted(self.mcp_server_configs.values(), key=lambda item: item.id)

    async def get_mcp_server_config(self, server_id: str) -> McpServerConfigRecord | None:
        return self.mcp_server_configs.get(server_id)

    async def update_mcp_server_config(
        self, server_id: str, **changes: object
    ) -> McpServerConfigRecord | None:
        record = self.mcp_server_configs.get(server_id)
        if not record:
            return None
        config_changed = any(
            key in changes and getattr(record, key, None) != changes[key]
            for key in ("enabled", "endpoint_url", "transport", "allowed_hosts")
        )
        for key in (
            "enabled",
            "endpoint_url",
            "transport",
            "allowed_hosts",
            "health_status",
            "consecutive_failures",
            "circuit_open_until",
            "last_checked_at",
            "last_error_code",
        ):
            if key in changes:
                setattr(record, key, changes[key])
        if config_changed:
            record.cache_revision += 1
        record.updated_at = now()
        return record

    async def replace_mcp_tool_snapshots(
        self, server_id: str, records: list[McpToolSnapshotRecord]
    ) -> list[McpToolSnapshotRecord]:
        self.mcp_tool_snapshots = {
            key: value
            for key, value in self.mcp_tool_snapshots.items()
            if value.server_id != server_id
        }
        for record in records:
            self.mcp_tool_snapshots[record.id] = record
        server = self.mcp_server_configs.get(server_id)
        if server:
            server.cache_revision += 1
            server.updated_at = now()
        return records

    async def list_mcp_tool_snapshots(self, server_id: str) -> list[McpToolSnapshotRecord]:
        return sorted(
            (item for item in self.mcp_tool_snapshots.values() if item.server_id == server_id),
            key=lambda item: item.normalized_name,
        )

    async def publish_agent_paragraph(
        self,
        run_id: str,
        paragraph_index: int,
        content: str,
        citations: list[dict],
        classification: str,
        claim_token: str,
    ) -> AgentRunEventRecord | None:
        run = self.agent_runs.get(run_id)
        if (
            not run
            or not self._agent_claim_is_current(run_id, claim_token)
            or run.cancel_requested
            or run.status != "running"
        ):
            return None
        key = f"paragraph:{paragraph_index}"
        existing = next(
            (
                item
                for item in self.agent_run_events.values()
                if item.run_id == run_id and item.event_key == key
            ),
            None,
        )
        if existing:
            return existing
        message = self.chat_messages.get(run.assistant_message_id or "")
        if not message:
            return None
        delta = content if not message.content else f"\n\n{content}"
        message.content = f"{message.content}{delta}"
        message.citations = list(
            {item["chunk_id"]: item for item in [*message.citations, *citations]}.values()
        )
        message.updated_at = now()
        return await self.append_agent_run_event(
            run_id,
            "message_delta",
            {
                "delta": delta,
                "message_id": message.id,
                "classification": classification,
                "citations": citations,
            },
            event_key=key,
            claim_token=claim_token,
        )

    async def finish_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        result_summary: dict,
        tool_steps: int = 0,
        duration_ms: int | None = None,
        error_code: str | None = None,
        pending_action: dict | None = None,
        claim_token: str | None = None,
        force: bool = False,
    ) -> AgentRunRecord | None:
        if status not in {"interrupted", "completed", "failed", "cancelled"}:
            raise ValueError("非法 Agent Run 终态")
        run = self.agent_runs.get(run_id)
        if not run:
            return None
        if not force and (
            claim_token is None or not self._agent_claim_is_current(run_id, claim_token)
        ):
            return None
        if run.status in {"completed", "failed", "cancelled"}:
            return run
        if run.cancel_requested or run.status == "cancelled":
            status = "cancelled"
            error_code = "AGENT_RUN_CANCELLED"
        run.status = status
        run.result_summary = dict(result_summary)
        run.tool_steps = tool_steps
        run.duration_ms = duration_ms
        run.error_code = error_code
        run.pending_action = pending_action
        run.updated_at = now()
        assistant = self.chat_messages.get(run.assistant_message_id or "")
        if assistant:
            assistant.status = (
                "pending"
                if status == "interrupted"
                else "completed"
                if status == "completed"
                else "cancelled"
                if status == "cancelled"
                else "failed"
            )
            assistant.updated_at = now()
        for job in self.jobs.values():
            if job.agent_run_id == run_id:
                job.status = (
                    JobStatus.completed
                    if status in {"completed", "interrupted", "cancelled"}
                    else JobStatus.failed
                )
                job.progress = 100
                job.claim_token = None
                job.claimed_at = None
                job.updated_at = now()
        await self.append_agent_run_event(
            run_id,
            "interrupt"
            if status == "interrupted"
            else "run_finished"
            if status != "failed"
            else "error",
            {
                "status": status,
                "duration_ms": duration_ms,
                **({"pending_action": pending_action or {}} if status == "interrupted" else {}),
            },
            event_key="terminal",
            # 当前方法已在变更任何状态前验证租约；作业终态会清除 token，
            # 因此终态事件在同一内存事务中不再重复验证已失效的 token。
            claim_token=None,
        )
        return run

    async def cancel_owned_agent_run(self, run_id: str, user_id: str) -> AgentRunRecord | None:
        run = await self.get_owned_agent_run(run_id, user_id)
        if not run:
            return None
        if run.status == "cancelled":
            return run
        if run.status in {"completed", "failed"}:
            raise ChatActiveRunError("运行已经结束")
        was_interrupted = run.status == "interrupted"
        if was_interrupted:
            for event in self.agent_run_events.values():
                if event.run_id == run_id and event.event_key == "terminal":
                    event.event_key = f"interrupt:{event.sequence}"
        run.cancel_requested = True
        return await self.finish_agent_run(
            run_id,
            status="cancelled",
            result_summary=run.result_summary or {},
            error_code="AGENT_RUN_CANCELLED",
            force=True,
        )

    async def resume_owned_agent_run(
        self,
        run_id: str,
        user_id: str,
        action_id: str,
        decision: str,
    ) -> AgentRunRecord | None:
        run = await self.get_owned_agent_run(run_id, user_id)
        if not run:
            return None
        if run.resume_action_id:
            if run.resume_action_id == action_id and run.resume_decision == decision:
                return run
            raise ChatIdempotencyConflictError("该待确认动作已使用不同决定处理")
        if run.status != "interrupted":
            raise ChatActiveRunError("运行未在等待确认")
        pending = run.pending_action or {}
        if pending.get("action_id") != action_id:
            raise ChatIdempotencyConflictError("待确认动作不匹配")
        run.resume_action_id = action_id
        run.resume_decision = decision
        run.scope_snapshot = {
            **run.scope_snapshot,
            "resume_decision": decision,
            "resumed_action": pending,
        }
        run.status = "pending"
        run.pending_action = None
        run.error_code = None
        run.updated_at = now()
        job = next(
            (item for item in self.jobs.values() if item.agent_run_id == run_id),
            None,
        )
        if not job:
            job = JobRecord(
                id=str(uuid.uuid4()),
                paper_id=None,
                agent_run_id=run_id,
                type="agent_run",
            )
            self.jobs[job.id] = job
        job.status = JobStatus.queued
        job.progress = 0
        job.attempts = 0
        job.available_at = now()
        job.claimed_at = None
        job.claim_token = None
        job.error_code = None
        job.error_message = None
        for event in self.agent_run_events.values():
            if event.run_id == run_id and event.event_key == "terminal":
                event.event_key = f"interrupt:{event.sequence}"
        await self.append_agent_run_event(
            run_id,
            "run_started",
            {"status": "pending", "resumed": True},
            event_key=f"resume:{action_id}:{decision}",
        )
        return run


class SQLAlchemyRepository:
    """生产仓库；每个方法都是独立短事务。"""

    def __init__(self, session_secret: str) -> None:
        self.session_secret = session_secret

    async def ensure_admin(self, email: str, password: str) -> User:
        existing = await self.find_user_by_email(email)
        if existing:
            return existing
        try:
            return await self.create_user(email, password, UserRole.admin, False)
        except ValueError:
            existing = await self.find_user_by_email(email)
            if not existing:
                raise
            return existing

    async def find_user_by_email(self, email: str) -> User | None:
        async with get_session_factory()() as session:
            return await session.scalar(select(User).where(User.email == email.strip().casefold()))

    async def get_user(self, user_id: str) -> User | None:
        async with get_session_factory()() as session:
            return await session.get(User, user_id)

    async def create_user(
        self,
        email: str,
        password: str,
        role: UserRole,
        must_change_password: bool = True,
    ) -> User:
        user = User(
            email=email.strip().casefold(),
            password_hash=hash_password(password),
            role=role,
            must_change_password=must_change_password,
        )
        async with get_session_factory()() as session:
            session.add(user)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("邮箱已存在") from exc
            await session.refresh(user)
            return user

    async def authenticate(self, email: str, password: str) -> User | None:
        user = await self.find_user_by_email(email)
        if not user or not user.active or not verify_password(user.password_hash, password):
            return None
        return user

    async def list_users(self) -> list[User]:
        async with get_session_factory()() as session:
            result = await session.scalars(select(User).order_by(User.created_at))
            return list(result)

    async def update_user(self, user_id: str, **changes: object) -> User | None:
        async with get_session_factory()() as session:
            user = await session.get(User, user_id)
            if not user:
                return None
            for key in (
                "active",
                "role",
                "must_change_password",
                "display_name",
                "preferences",
            ):
                if key in changes and (changes[key] is not None or key == "display_name"):
                    setattr(user, key, changes[key])
            if changes.get("active") is False:
                sessions = await session.scalars(
                    select(UserSession).where(UserSession.user_id == user_id)
                )
                for item in sessions:
                    await session.delete(item)
            await session.commit()
            await session.refresh(user)
            return user

    async def update_managed_user(
        self, user_id: str, acting_admin_id: str, **changes: object
    ) -> User:
        async with get_session_factory()() as session:
            # 统一顺序锁住所有活跃管理员，使“检查最后管理员 + 更新”处于同一事务，
            # 防止两个管理员被并发停用或降级造成零管理员状态。
            active_admins = list(
                await session.scalars(
                    select(User)
                    .where(User.active.is_(True), User.role == UserRole.admin)
                    .order_by(User.id)
                    .with_for_update()
                )
            )
            user = next((item for item in active_admins if item.id == user_id), None)
            if user is None:
                user = await session.get(User, user_id, with_for_update=True)
            if not user:
                raise ManagedUserNotFoundError("用户不存在")

            removes_active_admin = (
                user.active
                and user.role == UserRole.admin
                and (changes.get("active") is False or changes.get("role") == UserRole.user)
            )
            if removes_active_admin:
                # READ COMMITTED 下等待行锁后用新语句重新计数，不能复用等待前的查询快照。
                active_admin_count = await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.active.is_(True), User.role == UserRole.admin)
                )
                if int(active_admin_count or 0) <= 1:
                    raise LastAdminProtectionError("不能停用或降级最后一名管理员")
            if user.id == acting_admin_id and changes.get("active") is False:
                raise CurrentAdminProtectionError("不能停用当前管理员")

            for key in ("active", "role"):
                if key in changes and changes[key] is not None:
                    setattr(user, key, changes[key])
            if changes.get("active") is False:
                await session.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await session.commit()
            await session.refresh(user)
            return user

    async def create_session(self, user_id: str, token: str, ttl_seconds: int) -> None:
        digest = digest_session_token(token, self.session_secret)
        record = UserSession(
            id=digest,
            user_id=user_id,
            expires_at=now() + timedelta(seconds=ttl_seconds),
        )
        async with get_session_factory()() as session:
            session.add(record)
            await session.commit()

    async def user_for_session(self, token: str) -> User | None:
        digest = digest_session_token(token, self.session_secret)
        async with get_session_factory()() as session:
            result = await session.execute(
                select(UserSession, User)
                .join(User, User.id == UserSession.user_id)
                .where(
                    UserSession.id == digest,
                    UserSession.expires_at > now(),
                    User.active.is_(True),
                )
            )
            row = result.first()
            return row[1] if row else None

    async def delete_session(self, token: str) -> None:
        digest = digest_session_token(token, self.session_secret)
        async with get_session_factory()() as session:
            record = await session.get(UserSession, digest)
            if record:
                await session.delete(record)
                await session.commit()

    async def set_password(self, user_id: str, password: str) -> User:
        async with get_session_factory()() as session:
            user = await session.get(User, user_id)
            if not user:
                raise KeyError(user_id)
            user.password_hash = hash_password(password)
            user.must_change_password = False
            await session.commit()
            await session.refresh(user)
            return user

    async def create_paper(self, paper: PaperRecord) -> Paper:
        record = Paper(**paper.__dict__)
        async with get_session_factory()() as session:
            session.add(record)
            try:
                # Job 只持有 paper_id，并没有 ORM relationship 可供 unit-of-work
                # 推断插入顺序；先 flush Paper，避免 PostgreSQL 外键竞态。
                await session.flush()
                session.add(Job(paper_id=record.id, type="parse_pdf", status=JobStatus.queued))
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                duplicate = await session.scalar(
                    select(Paper).where(
                        Paper.owner_id == paper.owner_id, Paper.sha256 == paper.sha256
                    )
                )
                if duplicate:
                    raise ValueError(f"文献已存在:{duplicate.id}") from exc
                # 非重复键约束错误不能伪装成 409；交给全局错误处理记录为服务端故障。
                raise
            await session.refresh(record)
            return record

    async def list_papers(
        self,
        owner_id: str,
        collection_id: str | None = None,
        unfiled: bool = False,
    ) -> list[Paper]:
        allowed_ids: list[str] | None = None
        if collection_id is not None:
            allowed_ids = await self.resolve_collection_paper_ids(collection_id, owner_id)
            if allowed_ids is None:
                return []
        async with get_session_factory()() as session:
            statement = select(Paper).where(
                Paper.owner_id == owner_id,
                Paper.status != PaperStatus.deleting,
            )
            if allowed_ids is not None:
                if not allowed_ids:
                    return []
                statement = statement.where(Paper.id.in_(allowed_ids))
            if unfiled:
                statement = statement.where(~Paper.id.in_(select(paper_collections.c.paper_id)))
            result = await session.scalars(statement.order_by(Paper.created_at.desc()))
            return list(result)

    async def get_latest_discovery_batch(self, user_id: str) -> DiscoveryBatchPage | None:
        async with get_session_factory()() as session:
            batch = await session.scalar(
                select(DiscoveryBatch)
                .where(DiscoveryBatch.user_id == user_id)
                .order_by(DiscoveryBatch.batch_number.desc(), DiscoveryBatch.created_at.desc())
                .limit(1)
            )
            if batch is None:
                return None
            items = list(
                await session.scalars(
                    select(DiscoveryItem)
                    .where(
                        DiscoveryItem.batch_id == batch.id,
                        DiscoveryItem.user_id == user_id,
                    )
                    .order_by(DiscoveryItem.rank)
                )
            )
            return batch, items

    async def list_discovery_seen_arxiv_ids(self, user_id: str) -> set[str]:
        async with get_session_factory()() as session:
            return set(
                await session.scalars(
                    select(DiscoveryItem.arxiv_id).where(DiscoveryItem.user_id == user_id)
                )
            )

    async def get_discovery_feedback_signals(
        self, user_id: str, *, limit: int = 20
    ) -> tuple[list[str], list[str]]:
        async with get_session_factory()() as session:
            items = list(
                await session.scalars(
                    select(DiscoveryItem)
                    .where(
                        DiscoveryItem.user_id == user_id,
                        DiscoveryItem.feedback.in_(["interested", "not_interested"]),
                    )
                    .order_by(
                        DiscoveryItem.feedback_at.desc(),
                        DiscoveryItem.created_at.desc(),
                    )
                    .limit(limit)
                )
            )
        positive = [
            f"{item.title} {item.abstract}" for item in items if item.feedback == "interested"
        ]
        negative = [
            f"{item.title} {item.abstract}" for item in items if item.feedback == "not_interested"
        ]
        return positive, negative

    async def create_discovery_batch(
        self, batch: DiscoveryBatchRecord, items: list[DiscoveryItemRecord]
    ) -> DiscoveryBatchPage:
        record = DiscoveryBatch(
            id=batch.id,
            user_id=batch.user_id,
            batch_number=batch.batch_number,
            basis_paper_count=batch.basis_paper_count,
            seed_paper_title=batch.seed_paper_title,
            profile_terms=list(batch.profile_terms),
            strategy=batch.strategy,
            feedback_applied=batch.feedback_applied,
            created_at=batch.created_at,
        )
        records = [
            DiscoveryItem(
                id=item.id,
                batch_id=item.batch_id,
                user_id=item.user_id,
                arxiv_id=item.arxiv_id,
                title=item.title,
                authors=list(item.authors),
                abstract=item.abstract,
                published=item.published,
                pdf_url=item.pdf_url,
                journal_ref=item.journal_ref,
                matched_paper_title=item.matched_paper_title,
                matched_terms=list(item.matched_terms),
                match_type=item.match_type,
                score=item.score,
                rank=item.rank,
                created_at=item.created_at,
            )
            for item in items
        ]
        async with get_session_factory()() as session:
            session.add(record)
            session.add_all(records)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await self.get_latest_discovery_batch(batch.user_id)
                if existing is not None:
                    return existing
                raise
        return record, records

    async def record_discovery_item_action(
        self, item_id: str, user_id: str, action: str, *, arxiv_id: str | None = None
    ) -> DiscoveryItem | None:
        async with get_session_factory()() as session:
            item = await session.scalar(
                select(DiscoveryItem)
                .where(DiscoveryItem.id == item_id, DiscoveryItem.user_id == user_id)
                .with_for_update()
            )
            if item is None or (arxiv_id and item.arxiv_id != arxiv_id):
                return None
            timestamp = now()
            if action == "opened":
                item.opened_at = item.opened_at or timestamp
            elif action in {"interested", "not_interested"}:
                item.feedback = action
                item.feedback_at = timestamp
            elif action == "imported":
                item.imported_at = item.imported_at or timestamp
            else:
                raise ValueError("不支持的推荐反馈动作")
            await session.commit()
            await session.refresh(item)
            return item

    async def discovery_metrics(self, since: datetime) -> dict[str, int | float]:
        async with get_session_factory()() as session:
            batches = int(
                await session.scalar(
                    select(func.count(DiscoveryBatch.id)).where(DiscoveryBatch.created_at >= since)
                )
                or 0
            )
            row = (
                await session.execute(
                    select(
                        func.count(DiscoveryItem.id),
                        func.count(DiscoveryItem.id).filter(DiscoveryItem.opened_at.is_not(None)),
                        func.count(DiscoveryItem.id).filter(DiscoveryItem.feedback == "interested"),
                        func.count(DiscoveryItem.id).filter(
                            DiscoveryItem.feedback == "not_interested"
                        ),
                        func.count(DiscoveryItem.id).filter(DiscoveryItem.imported_at.is_not(None)),
                    ).where(DiscoveryItem.created_at >= since)
                )
            ).one()
        impressions, opened, interested, not_interested, imported = (
            int(value or 0) for value in row
        )
        feedback = interested + not_interested
        return _discovery_metric_report(
            batches, impressions, opened, interested, not_interested, imported, feedback
        )

    async def get_owned_paper(self, paper_id: str, owner_id: str) -> Paper | None:
        async with get_session_factory()() as session:
            return await session.scalar(
                select(Paper).where(Paper.id == paper_id, Paper.owner_id == owner_id)
            )

    async def get_owned_paper_page_text(
        self, paper_id: str, physical_page: int, owner_id: str
    ) -> str | None:
        async with get_session_factory()() as session:
            return await session.scalar(
                select(PaperPage.text)
                .join(Paper, Paper.id == PaperPage.paper_id)
                .where(
                    PaperPage.paper_id == paper_id,
                    PaperPage.physical_page == physical_page,
                    Paper.owner_id == owner_id,
                )
            )

    async def update_owned_paper(
        self, paper_id: str, owner_id: str, **changes: object
    ) -> Paper | None:
        async with get_session_factory()() as session:
            paper = await session.scalar(
                select(Paper).where(Paper.id == paper_id, Paper.owner_id == owner_id)
            )
            if not paper:
                return None
            for key in (
                "title",
                "authors",
                "year",
                "abstract",
                "doi",
                "publication",
                "status",
            ):
                if key in changes and changes[key] is not None:
                    setattr(paper, key, changes[key])
            paper.updated_at = now()
            await session.commit()
            await session.refresh(paper)
            return paper

    async def get_owned_paper_artifact(
        self, paper_id: str, owner_id: str, artifact_type: str
    ) -> PaperArtifact | None:
        async with get_session_factory()() as session:
            return await session.scalar(
                select(PaperArtifact).where(
                    PaperArtifact.paper_id == paper_id,
                    PaperArtifact.owner_id == owner_id,
                    PaperArtifact.type == artifact_type,
                )
            )

    async def upsert_paper_artifact(
        self,
        paper_id: str,
        owner_id: str,
        artifact_type: str,
        source_revision_value: str,
        status: str,
        fallback_reason: str | None,
        structured_payload: dict,
        markdown: str,
    ) -> PaperArtifact | None:
        async with get_session_factory()() as session:
            paper = await session.scalar(
                select(Paper)
                .where(Paper.id == paper_id, Paper.owner_id == owner_id)
                .with_for_update()
            )
            if paper is None:
                return None
            record = await session.scalar(
                select(PaperArtifact)
                .where(
                    PaperArtifact.paper_id == paper_id,
                    PaperArtifact.type == artifact_type,
                )
                .with_for_update()
            )
            if record is None:
                record = PaperArtifact(
                    paper_id=paper_id,
                    owner_id=owner_id,
                    type=artifact_type,
                    source_revision=source_revision_value,
                    status=status,
                    fallback_reason=fallback_reason,
                    structured_payload=dict(structured_payload),
                    markdown=markdown,
                )
                session.add(record)
            else:
                record.source_revision = source_revision_value
                record.status = status
                record.fallback_reason = fallback_reason
                record.structured_payload = dict(structured_payload)
                record.markdown = markdown
                record.updated_at = now()
            await session.commit()
            await session.refresh(record)
            return record

    async def get_active_paper_artifact_job(
        self, paper_id: str, owner_id: str, artifact_type: str
    ) -> Job | None:
        job_type = ARTIFACT_JOB_TYPES.get(artifact_type)
        if not job_type:
            return None
        async with get_session_factory()() as session:
            return await session.scalar(
                select(Job)
                .join(Paper, Paper.id == Job.paper_id)
                .where(
                    Job.paper_id == paper_id,
                    Paper.owner_id == owner_id,
                    Job.type == job_type,
                    Job.status.in_([JobStatus.queued, JobStatus.running]),
                )
                .order_by(Job.created_at.desc())
                .limit(1)
            )

    async def enqueue_paper_artifact(
        self,
        paper_id: str,
        owner_id: str,
        artifact_type: str,
        source_revision_value: str,
        *,
        preserve_existing: bool,
    ) -> Job | None:
        job_type = ARTIFACT_JOB_TYPES.get(artifact_type)
        if not job_type:
            return None
        async with get_session_factory()() as session:
            paper = await session.scalar(
                select(Paper)
                .where(Paper.id == paper_id, Paper.owner_id == owner_id)
                .with_for_update()
            )
            if not paper:
                return None
            active = await session.scalar(
                select(Job)
                .where(
                    Job.paper_id == paper_id,
                    Job.type == job_type,
                    Job.status.in_([JobStatus.queued, JobStatus.running]),
                )
                .order_by(Job.created_at.desc())
                .with_for_update()
                .limit(1)
            )
            if active:
                return active
            artifact = await session.scalar(
                select(PaperArtifact)
                .where(
                    PaperArtifact.paper_id == paper_id,
                    PaperArtifact.type == artifact_type,
                )
                .with_for_update()
            )
            if not preserve_existing:
                if artifact is None:
                    artifact = PaperArtifact(
                        paper_id=paper_id,
                        owner_id=owner_id,
                        type=artifact_type,
                        source_revision=source_revision_value,
                        status="processing",
                        fallback_reason=None,
                        structured_payload={},
                        markdown="",
                    )
                    session.add(artifact)
                else:
                    artifact.source_revision = source_revision_value
                    artifact.status = "processing"
                    artifact.fallback_reason = None
                    artifact.structured_payload = {}
                    artifact.markdown = ""
                    artifact.updated_at = now()
            job = Job(
                paper_id=paper_id,
                type=job_type,
                max_attempts=2,
                status=JobStatus.queued,
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job

    async def mark_paper_artifacts_stale(self, paper_id: str) -> None:
        async with get_session_factory()() as session:
            await session.execute(
                update(PaperArtifact)
                .where(PaperArtifact.paper_id == paper_id)
                .values(status="stale", updated_at=now())
            )
            await session.commit()

    async def requeue_owned_paper(self, paper_id: str, owner_id: str) -> Paper | None:
        """原子地创建新的解析任务；可用于失败重试和已完成论文的重新识别。"""

        async with get_session_factory()() as session:
            paper = await session.scalar(
                select(Paper)
                .where(Paper.id == paper_id, Paper.owner_id == owner_id)
                .with_for_update()
            )
            if not paper or paper.status in {
                PaperStatus.queued,
                PaperStatus.extracting,
                PaperStatus.deleting,
            }:
                return None
            active_job = await session.scalar(
                select(Job.id).where(
                    Job.paper_id == paper.id,
                    Job.type == "parse_pdf",
                    Job.status.in_([JobStatus.queued, JobStatus.running]),
                )
            )
            if active_job:
                return None
            paper.status = PaperStatus.queued
            paper.updated_at = now()
            translation_ids = list(
                await session.scalars(
                    select(PaperTranslation.id).where(PaperTranslation.paper_id == paper.id)
                )
            )
            if translation_ids:
                await session.execute(
                    update(PaperTranslation)
                    .where(PaperTranslation.id.in_(translation_ids))
                    .values(
                        status="failed",
                        error_code="SOURCE_CHANGED",
                        error_message="论文正在重新索引，既有译文已失效",
                        updated_at=now(),
                    )
                )
                await session.execute(
                    update(PaperTranslationPage)
                    .where(PaperTranslationPage.translation_id.in_(translation_ids))
                    .values(
                        status="failed",
                        translated_text=None,
                        error_code="SOURCE_CHANGED",
                        error_message="来源页面正在重新索引",
                        updated_at=now(),
                    )
                )
                await session.execute(
                    update(Job)
                    .where(
                        Job.translation_id.in_(translation_ids),
                        Job.status.in_([JobStatus.queued, JobStatus.running]),
                    )
                    .values(
                        status=JobStatus.completed,
                        error_code="SOURCE_CHANGED",
                        error_message="论文重新索引已终止旧翻译作业",
                        claimed_at=None,
                        claim_token=None,
                        updated_at=now(),
                    )
                )
            session.add(Job(paper_id=paper.id, type="parse_pdf", status=JobStatus.queued))
            await session.commit()
            await session.refresh(paper)
            return paper

    async def delete_owned_paper(self, paper_id: str, owner_id: str) -> Paper | None:
        async with get_session_factory()() as session:
            paper = await session.scalar(
                select(Paper)
                .where(Paper.id == paper_id, Paper.owner_id == owner_id)
                .with_for_update()
            )
            if not paper:
                return None
            paper.status = PaperStatus.deleting
            paper.updated_at = now()
            translation_ids = list(
                await session.scalars(
                    select(PaperTranslation.id).where(
                        PaperTranslation.paper_id == paper.id,
                        PaperTranslation.status != "completed",
                    )
                )
            )
            if translation_ids:
                await session.execute(
                    update(PaperTranslation)
                    .where(PaperTranslation.id.in_(translation_ids))
                    .values(
                        status="cancelled",
                        cancel_requested=True,
                        error_code="PAPER_DELETING",
                        error_message="文献正在删除，全文翻译已取消",
                        updated_at=now(),
                    )
                )
                await session.execute(
                    update(PaperTranslationPage)
                    .where(
                        PaperTranslationPage.translation_id.in_(translation_ids),
                        PaperTranslationPage.status.not_in(["completed", "no_text"]),
                    )
                    .values(
                        status="cancelled",
                        error_code="PAPER_DELETING",
                        error_message="文献正在删除，全文翻译已取消",
                        updated_at=now(),
                    )
                )
                await session.execute(
                    update(Job)
                    .where(
                        Job.translation_id.in_(translation_ids),
                        Job.status.in_([JobStatus.queued, JobStatus.running]),
                    )
                    .values(
                        status=JobStatus.completed,
                        error_code="PAPER_DELETING",
                        error_message="文献删除已取消全文翻译作业",
                        claimed_at=None,
                        claim_token=None,
                        updated_at=now(),
                    )
                )
            active_delete = await session.scalar(
                select(Job).where(
                    Job.paper_id == paper.id,
                    Job.type == "delete_paper",
                    Job.status.in_([JobStatus.queued, JobStatus.running]),
                )
            )
            if not active_delete:
                session.add(Job(paper_id=paper.id, type="delete_paper"))
            await session.commit()
            await session.refresh(paper)
            return paper

    async def touch_paper_opened(self, paper_id: str, owner_id: str) -> Paper | None:
        async with get_session_factory()() as session:
            paper = await session.scalar(
                select(Paper).where(Paper.id == paper_id, Paper.owner_id == owner_id)
            )
            if not paper:
                return None
            paper.last_opened_at = now()
            paper.updated_at = now()
            await session.commit()
            await session.refresh(paper)
            return paper

    async def set_papers_archived(
        self, paper_ids: list[str], owner_id: str, archived: bool
    ) -> list[str] | None:
        unique_ids = list(dict.fromkeys(paper_ids))
        async with get_session_factory()() as session:
            owned_ids = list(
                await session.scalars(
                    select(Paper.id).where(
                        Paper.owner_id == owner_id,
                        Paper.id.in_(unique_ids),
                        Paper.status != PaperStatus.deleting,
                    )
                )
            )
            if set(owned_ids) != set(unique_ids):
                return None
            timestamp = now()
            await session.execute(
                update(Paper)
                .where(Paper.owner_id == owner_id, Paper.id.in_(unique_ids))
                .values(
                    archived_at=timestamp if archived else None,
                    updated_at=timestamp,
                )
            )
            await session.commit()
            return unique_ids

    async def count_active_admins(self) -> int:
        async with get_session_factory()() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.active.is_(True), User.role == UserRole.admin)
            )
            return int(value or 0)

    async def create_collection(
        self,
        owner_id: str,
        name: str,
        description: str | None,
        parent_id: str | None = None,
    ) -> Collection:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("集合名称不能为空")
        async with get_session_factory()() as session:
            records = list(
                await session.scalars(select(Collection).where(Collection.owner_id == owner_id))
            )
            _validate_collection_change(
                records,
                owner_id=owner_id,
                name=normalized_name,
                parent_id=parent_id,
            )
            record = Collection(
                owner_id=owner_id,
                parent_id=parent_id,
                name=normalized_name,
                description=description,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("同级集合名称已存在") from exc
            await session.refresh(record)
            return record

    async def _append_agent_event_in_session(
        self,
        session: object,
        run_id: str,
        event: str,
        data: dict,
        event_key: str | None,
    ) -> AgentRunEvent:
        if event_key:
            existing = await session.scalar(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == run_id,
                    AgentRunEvent.event_key == event_key,
                )
            )
            if existing:
                return existing
        sequence = 1 + int(
            await session.scalar(
                select(func.max(AgentRunEvent.sequence)).where(AgentRunEvent.run_id == run_id)
            )
            or 0
        )
        record = AgentRunEvent(
            run_id=run_id,
            sequence=sequence,
            event=event,
            data=dict(data),
            event_key=event_key,
        )
        session.add(record)
        await session.flush()
        return record

    async def append_agent_run_event(
        self,
        run_id: str,
        event: str,
        data: dict,
        *,
        event_key: str | None = None,
        claim_token: str | None = None,
    ) -> AgentRunEvent | None:
        async with get_session_factory()() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == run_id).with_for_update()
            )
            if not run:
                return None
            if claim_token is not None and not await self._sql_agent_claim_current(
                session, run_id, claim_token
            ):
                return None
            record = await self._append_agent_event_in_session(
                session, run_id, event, data, event_key
            )
            await session.commit()
            await session.refresh(record)
            return record

    async def _sql_agent_claim_current(
        self, session: object, run_id: str, claim_token: str
    ) -> bool:
        job = await session.scalar(
            select(Job)
            .where(
                Job.agent_run_id == run_id,
                Job.type == "agent_run",
                Job.status == JobStatus.running,
                Job.claim_token == claim_token,
                Job.claimed_at.is_not(None),
                Job.claimed_at >= now() - AGENT_JOB_LEASE,
            )
            .with_for_update()
        )
        return bool(job)

    async def is_agent_claim_current(self, run_id: str, claim_token: str) -> bool:
        async with get_session_factory()() as session:
            return await self._sql_agent_claim_current(session, run_id, claim_token)

    async def claim_agent_run_job(self, run_id: str) -> str | None:
        async with get_session_factory()() as session:
            job = await session.scalar(
                select(Job)
                .where(
                    Job.agent_run_id == run_id,
                    Job.type == "agent_run",
                    Job.status == JobStatus.queued,
                    Job.available_at <= now(),
                )
                .with_for_update(skip_locked=True)
            )
            if not job:
                return None
            token = str(uuid.uuid4())
            job.status = JobStatus.running
            job.attempts += 1
            job.claim_token = token
            job.claimed_at = now()
            job.updated_at = now()
            await session.commit()
            return token

    async def list_owned_agent_run_events(
        self, run_id: str, user_id: str, after_sequence: int = 0
    ) -> list[AgentRunEvent] | None:
        async with get_session_factory()() as session:
            owned = await session.scalar(
                select(AgentRun.id).where(AgentRun.id == run_id, AgentRun.user_id == user_id)
            )
            if not owned:
                return None
            return list(
                await session.scalars(
                    select(AgentRunEvent)
                    .where(
                        AgentRunEvent.run_id == run_id,
                        AgentRunEvent.sequence > after_sequence,
                    )
                    .order_by(AgentRunEvent.sequence)
                )
            )

    async def get_agent_run(self, run_id: str) -> AgentRun | None:
        async with get_session_factory()() as session:
            return await session.get(AgentRun, run_id)

    async def list_agent_runs_for_observability(
        self, since: datetime, *, limit: int = 5000
    ) -> list[AgentRun]:
        async with get_session_factory()() as session:
            return list(
                await session.scalars(
                    select(AgentRun)
                    .where(AgentRun.created_at >= since)
                    .order_by(AgentRun.created_at.desc())
                    .limit(limit)
                )
            )

    async def get_agent_run_input(self, run_id: str) -> tuple[AgentRun, str] | None:
        async with get_session_factory()() as session:
            run = await session.get(AgentRun, run_id)
            if not run or not run.user_message_id:
                return None
            message = await session.get(ChatMessage, run.user_message_id)
            return (run, message.content) if message else None

    async def start_agent_run(self, run_id: str, claim_token: str) -> AgentRun | None:
        async with get_session_factory()() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == run_id).with_for_update()
            )
            if (
                not run
                or not await self._sql_agent_claim_current(session, run_id, claim_token)
                or run.cancel_requested
                or run.status == "cancelled"
            ):
                return None
            if run.status == "pending":
                run.status = "running"
                run.updated_at = now()
            assistant = (
                await session.scalar(
                    select(ChatMessage)
                    .where(ChatMessage.id == run.assistant_message_id)
                    .with_for_update()
                )
                if run.assistant_message_id
                else None
            )
            if assistant and assistant.status == "pending":
                assistant.status = "streaming"
                assistant.updated_at = now()
            await self._append_agent_event_in_session(
                session,
                run_id,
                "run_started",
                {"status": "running"},
                "run_started",
            )
            await session.commit()
            await session.refresh(run)
            return run

    async def update_agent_context(
        self,
        run_id: str,
        claim_token: str,
        *,
        context_snapshot: dict,
        resolved_query: str,
        reference_confidence: float,
    ) -> AgentRun | None:
        async with get_session_factory()() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == run_id).with_for_update()
            )
            if not run or not await self._sql_agent_claim_current(session, run_id, claim_token):
                return None
            run.context_snapshot = dict(context_snapshot)
            run.context_version = int(context_snapshot.get("version", 1))
            run.resolved_query = resolved_query
            run.reference_confidence = reference_confidence
            run.updated_at = now()
            await session.commit()
            await session.refresh(run)
            return run

    async def update_agent_skill(
        self,
        run_id: str,
        claim_token: str,
        *,
        selected_skill: str,
        skill_version: int,
        harness_trace: dict,
    ) -> AgentRun | None:
        async with get_session_factory()() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == run_id).with_for_update()
            )
            if not run or not await self._sql_agent_claim_current(session, run_id, claim_token):
                return None
            run.selected_skill = selected_skill
            run.skill_version = skill_version
            run.harness_trace = dict(harness_trace)
            run.updated_at = now()
            await session.commit()
            await session.refresh(run)
            return run

    async def start_agent_tool_call(
        self,
        record: AgentToolCallRecord,
        claim_token: str,
    ) -> AgentToolCall | None:
        async with get_session_factory()() as session:
            if not await self._sql_agent_claim_current(session, record.run_id, claim_token):
                return None
            existing = await session.scalar(
                select(AgentToolCall).where(
                    AgentToolCall.run_id == record.run_id,
                    AgentToolCall.call_id == record.call_id,
                )
            )
            if existing:
                return existing
            value = AgentToolCall(**record.__dict__)
            session.add(value)
            await session.commit()
            await session.refresh(value)
            return value

    async def list_agent_tool_calls_for_observability(
        self, since: datetime, *, limit: int = 10000
    ) -> list[AgentToolCall]:
        async with get_session_factory()() as session:
            return list(
                await session.scalars(
                    select(AgentToolCall)
                    .where(AgentToolCall.created_at >= since)
                    .order_by(AgentToolCall.created_at.desc())
                    .limit(limit)
                )
            )

    async def memory_observability_counts(self) -> dict[str, object]:
        async with get_session_factory()() as session:
            totals = (
                await session.execute(
                    select(
                        func.count(MemoryItem.id),
                        func.count(MemoryItem.id).filter(MemoryItem.enabled.is_(True)),
                        func.count(MemoryItem.id).filter(MemoryItem.enabled.is_(False)),
                        func.count(MemoryItem.id).filter(MemoryItem.pinned.is_(True)),
                        func.count(func.distinct(MemoryItem.user_id)),
                    )
                )
            ).one()
            type_rows = (
                await session.execute(
                    select(MemoryItem.type, func.count(MemoryItem.id)).group_by(MemoryItem.type)
                )
            ).all()
            source_rows = (
                await session.execute(
                    select(MemoryItem.source_kind, func.count(MemoryItem.id)).group_by(
                        MemoryItem.source_kind
                    )
                )
            ).all()
            superseded = await session.scalar(
                select(func.count(MemoryItemVersion.id)).where(
                    MemoryItemVersion.status == "superseded"
                )
            )
            users = int(totals[4] or 0)
            return {
                "total": int(totals[0] or 0),
                "active": int(totals[1] or 0),
                "disabled": int(totals[2] or 0),
                "pinned": int(totals[3] or 0),
                "users_with_memory": users,
                "capacity": users * 200,
                "superseded_versions": int(superseded or 0),
                "types": {str(key): int(count) for key, count in type_rows},
                "sources": {str(key): int(count) for key, count in source_rows},
            }

    async def ensure_mcp_server_config(self, record: McpServerConfigRecord) -> McpServerConfig:
        async with get_session_factory()() as session:
            value = await session.get(McpServerConfig, record.id)
            if value:
                return value
            value = McpServerConfig(**record.__dict__)
            session.add(value)
            await session.commit()
            await session.refresh(value)
            return value

    async def list_mcp_server_configs(self) -> list[McpServerConfig]:
        async with get_session_factory()() as session:
            return list(await session.scalars(select(McpServerConfig).order_by(McpServerConfig.id)))

    async def get_mcp_server_config(self, server_id: str) -> McpServerConfig | None:
        async with get_session_factory()() as session:
            return await session.get(McpServerConfig, server_id)

    async def update_mcp_server_config(
        self, server_id: str, **changes: object
    ) -> McpServerConfig | None:
        async with get_session_factory()() as session:
            value = await session.get(McpServerConfig, server_id)
            if not value:
                return None
            config_changed = any(
                key in changes and getattr(value, key, None) != changes[key]
                for key in ("enabled", "endpoint_url", "transport", "allowed_hosts")
            )
            for key in (
                "enabled",
                "endpoint_url",
                "transport",
                "allowed_hosts",
                "health_status",
                "consecutive_failures",
                "circuit_open_until",
                "last_checked_at",
                "last_error_code",
            ):
                if key in changes:
                    setattr(value, key, changes[key])
            if config_changed:
                value.cache_revision += 1
            value.updated_at = now()
            await session.commit()
            await session.refresh(value)
            return value

    async def replace_mcp_tool_snapshots(
        self, server_id: str, records: list[McpToolSnapshotRecord]
    ) -> list[McpToolSnapshot]:
        async with get_session_factory()() as session:
            await session.execute(
                delete(McpToolSnapshot).where(McpToolSnapshot.server_id == server_id)
            )
            values = [McpToolSnapshot(**record.__dict__) for record in records]
            session.add_all(values)
            server = await session.get(McpServerConfig, server_id)
            if server:
                server.cache_revision += 1
                server.updated_at = now()
            await session.commit()
            for value in values:
                await session.refresh(value)
            return values

    async def list_mcp_tool_snapshots(self, server_id: str) -> list[McpToolSnapshot]:
        async with get_session_factory()() as session:
            return list(
                await session.scalars(
                    select(McpToolSnapshot)
                    .where(McpToolSnapshot.server_id == server_id)
                    .order_by(McpToolSnapshot.normalized_name)
                )
            )

    async def finish_agent_tool_call(
        self,
        tool_call_id: str,
        run_id: str,
        claim_token: str,
        *,
        status: str,
        attempt: int,
        duration_ms: int,
        result_preview: dict | None,
        error_code: str | None,
    ) -> AgentToolCall | None:
        async with get_session_factory()() as session:
            if not await self._sql_agent_claim_current(session, run_id, claim_token):
                return None
            value = await session.scalar(
                select(AgentToolCall).where(
                    AgentToolCall.id == tool_call_id,
                    AgentToolCall.run_id == run_id,
                )
            )
            if not value:
                return None
            value.status = status
            value.attempt = attempt
            value.duration_ms = duration_ms
            value.result_preview = result_preview
            value.error_code = error_code
            value.updated_at = now()
            await session.commit()
            await session.refresh(value)
            return value

    async def create_agent_tool_artifact(
        self,
        record: AgentToolArtifactRecord,
        claim_token: str,
    ) -> AgentToolArtifact | None:
        async with get_session_factory()() as session:
            tool_call = await session.scalar(
                select(AgentToolCall).where(AgentToolCall.id == record.tool_call_id)
            )
            if not tool_call or not await self._sql_agent_claim_current(
                session, tool_call.run_id, claim_token
            ):
                return None
            value = AgentToolArtifact(**record.__dict__)
            session.add(value)
            await session.commit()
            await session.refresh(value)
            return value

    async def publish_agent_paragraph(
        self,
        run_id: str,
        paragraph_index: int,
        content: str,
        citations: list[dict],
        classification: str,
        claim_token: str,
    ) -> AgentRunEvent | None:
        async with get_session_factory()() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == run_id).with_for_update()
            )
            if (
                not run
                or not await self._sql_agent_claim_current(session, run_id, claim_token)
                or run.cancel_requested
                or run.status != "running"
                or not run.assistant_message_id
            ):
                return None
            event_key = f"paragraph:{paragraph_index}"
            existing = await session.scalar(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == run_id,
                    AgentRunEvent.event_key == event_key,
                )
            )
            if existing:
                return existing
            assistant = await session.scalar(
                select(ChatMessage)
                .where(ChatMessage.id == run.assistant_message_id)
                .with_for_update()
            )
            if not assistant:
                return None
            delta = content if not assistant.content else f"\n\n{content}"
            assistant.content = f"{assistant.content}{delta}"
            by_chunk = {
                str(item.get("chunk_id")): item
                for item in [*assistant.citations, *citations]
                if item.get("chunk_id")
            }
            assistant.citations = list(by_chunk.values())
            assistant.status = "streaming"
            assistant.updated_at = now()
            record = await self._append_agent_event_in_session(
                session,
                run_id,
                "message_delta",
                {
                    "delta": delta,
                    "message_id": assistant.id,
                    "classification": classification,
                    "citations": citations,
                },
                event_key,
            )
            await session.commit()
            await session.refresh(record)
            return record

    async def finish_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        result_summary: dict,
        tool_steps: int = 0,
        duration_ms: int | None = None,
        error_code: str | None = None,
        pending_action: dict | None = None,
        claim_token: str | None = None,
        force: bool = False,
    ) -> AgentRun | None:
        if status not in {"interrupted", "completed", "failed", "cancelled"}:
            raise ValueError("非法 Agent Run 终态")
        async with get_session_factory()() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == run_id).with_for_update()
            )
            if not run:
                return None
            if not force and (
                claim_token is None
                or not await self._sql_agent_claim_current(session, run_id, claim_token)
            ):
                return None
            if run.status in {"completed", "failed", "cancelled"}:
                return run
            if run.cancel_requested:
                status = "cancelled"
                error_code = "AGENT_RUN_CANCELLED"
            run.status = status
            run.result_summary = dict(result_summary)
            run.tool_steps = tool_steps
            run.duration_ms = duration_ms
            run.error_code = error_code
            run.pending_action = pending_action
            run.updated_at = now()
            assistant = (
                await session.scalar(
                    select(ChatMessage)
                    .where(ChatMessage.id == run.assistant_message_id)
                    .with_for_update()
                )
                if run.assistant_message_id
                else None
            )
            if assistant:
                assistant.status = (
                    "pending"
                    if status == "interrupted"
                    else "completed"
                    if status == "completed"
                    else "cancelled"
                    if status == "cancelled"
                    else "failed"
                )
                assistant.updated_at = now()
            job = await session.scalar(
                select(Job).where(Job.agent_run_id == run_id).with_for_update()
            )
            if job:
                job.status = (
                    JobStatus.completed
                    if status in {"completed", "interrupted", "cancelled"}
                    else JobStatus.failed
                )
                job.progress = 100
                job.claim_token = None
                job.claimed_at = None
                job.updated_at = now()
            await self._append_agent_event_in_session(
                session,
                run_id,
                "interrupt"
                if status == "interrupted"
                else "error"
                if status == "failed"
                else "run_finished",
                {
                    "status": status,
                    "duration_ms": duration_ms,
                    **({"pending_action": pending_action or {}} if status == "interrupted" else {}),
                },
                "terminal",
            )
            await session.commit()
            await session.refresh(run)
            return run

    async def cancel_owned_agent_run(self, run_id: str, user_id: str) -> AgentRun | None:
        async with get_session_factory()() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(AgentRun.id == run_id, AgentRun.user_id == user_id)
                .with_for_update()
            )
            if not run:
                return None
            if run.status == "cancelled":
                return run
            if run.status in {"completed", "failed"}:
                raise ChatActiveRunError("运行已经结束")
            was_interrupted = run.status == "interrupted"
            if was_interrupted:
                terminal = await session.scalar(
                    select(AgentRunEvent)
                    .where(
                        AgentRunEvent.run_id == run_id,
                        AgentRunEvent.event_key == "terminal",
                    )
                    .with_for_update()
                )
                if terminal:
                    terminal.event_key = f"interrupt:{terminal.sequence}"
            run.cancel_requested = True
            run.status = "cancelled"
            run.error_code = "AGENT_RUN_CANCELLED"
            run.pending_action = None
            run.updated_at = now()
            assistant = (
                await session.scalar(
                    select(ChatMessage)
                    .where(ChatMessage.id == run.assistant_message_id)
                    .with_for_update()
                )
                if run.assistant_message_id
                else None
            )
            if assistant:
                assistant.status = "cancelled"
                assistant.updated_at = now()
            job = await session.scalar(
                select(Job).where(Job.agent_run_id == run_id).with_for_update()
            )
            if job:
                job.status = JobStatus.completed
                job.error_code = "AGENT_RUN_CANCELLED"
                job.error_message = "用户已取消 Agent 运行"
                job.claim_token = None
                job.claimed_at = None
                job.updated_at = now()
            await self._append_agent_event_in_session(
                session,
                run_id,
                "run_finished",
                {"status": "cancelled"},
                "terminal",
            )
            await session.commit()
            await session.refresh(run)
            return run

    async def resume_owned_agent_run(
        self,
        run_id: str,
        user_id: str,
        action_id: str,
        decision: str,
    ) -> AgentRun | None:
        async with get_session_factory()() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(AgentRun.id == run_id, AgentRun.user_id == user_id)
                .with_for_update()
            )
            if not run:
                return None
            if run.resume_action_id:
                if run.resume_action_id == action_id and run.resume_decision == decision:
                    return run
                raise ChatIdempotencyConflictError("该待确认动作已使用不同决定处理")
            if run.status != "interrupted":
                raise ChatActiveRunError("运行未在等待确认")
            pending = run.pending_action or {}
            if pending.get("action_id") != action_id:
                raise ChatIdempotencyConflictError("待确认动作不匹配")
            run.resume_action_id = action_id
            run.resume_decision = decision
            run.scope_snapshot = {
                **(run.scope_snapshot or {}),
                "resume_decision": decision,
                "resumed_action": pending,
            }
            run.status = "pending"
            run.pending_action = None
            run.error_code = None
            run.updated_at = now()
            job = await session.scalar(
                select(Job).where(Job.agent_run_id == run_id).with_for_update()
            )
            if not job:
                job = Job(agent_run_id=run_id, type="agent_run")
                session.add(job)
            job.status = JobStatus.queued
            job.progress = 0
            job.attempts = 0
            job.available_at = now()
            job.claim_token = None
            job.claimed_at = None
            job.error_code = None
            job.error_message = None
            # interrupted 的 terminal 事件只代表暂停；resume 后允许新的最终事件键。
            terminal = await session.scalar(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == run_id,
                    AgentRunEvent.event_key == "terminal",
                )
            )
            if terminal:
                terminal.event_key = f"interrupt:{terminal.sequence}"
            await self._append_agent_event_in_session(
                session,
                run_id,
                "run_started",
                {"status": "pending", "resumed": True},
                f"resume:{action_id}:{decision}",
            )
            await session.commit()
            await session.refresh(run)
            return run

    async def list_collections(self, owner_id: str) -> list[Collection]:
        async with get_session_factory()() as session:
            result = await session.scalars(
                select(Collection).where(Collection.owner_id == owner_id).order_by(Collection.name)
            )
            return list(result)

    async def list_collection_memberships(self, owner_id: str) -> dict[str, list[str]]:
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(Collection.id, paper_collections.c.paper_id)
                    .join(
                        paper_collections,
                        paper_collections.c.collection_id == Collection.id,
                    )
                    .where(Collection.owner_id == owner_id)
                    .order_by(Collection.id, paper_collections.c.paper_id)
                )
            ).all()
            memberships: dict[str, list[str]] = {}
            for collection_id, paper_id in rows:
                memberships.setdefault(collection_id, []).append(paper_id)
            return memberships

    async def resolve_collection_paper_ids(
        self, collection_id: str, owner_id: str, *, ready_only: bool = False
    ) -> list[str] | None:
        async with get_session_factory()() as session:
            records = list(
                await session.scalars(select(Collection).where(Collection.owner_id == owner_id))
            )
            if collection_id not in {item.id for item in records}:
                return None
            descendants = {collection_id}
            pending = [collection_id]
            while pending:
                current = pending.pop()
                child_ids = [
                    item.id
                    for item in records
                    if item.parent_id == current and item.id not in descendants
                ]
                descendants.update(child_ids)
                pending.extend(child_ids)
            statement = (
                select(paper_collections.c.paper_id)
                .join(Paper, Paper.id == paper_collections.c.paper_id)
                .where(
                    paper_collections.c.collection_id.in_(descendants),
                    Paper.owner_id == owner_id,
                )
                .distinct()
                .order_by(paper_collections.c.paper_id)
            )
            if ready_only:
                statement = statement.where(Paper.status == PaperStatus.ready)
            return list(await session.scalars(statement))

    async def update_collection(
        self, collection_id: str, owner_id: str, **changes: object
    ) -> Collection | None:
        async with get_session_factory()() as session:
            records = list(
                await session.scalars(
                    select(Collection)
                    .where(Collection.owner_id == owner_id)
                    .order_by(Collection.id)
                    .with_for_update()
                )
            )
            record = next((item for item in records if item.id == collection_id), None)
            if not record:
                return None
            if "name" in changes:
                normalized_name = str(changes["name"]).strip()
                if not normalized_name:
                    raise ValueError("集合名称不能为空")
                changes["name"] = normalized_name
            proposed_name = str(changes.get("name", record.name))
            proposed_parent_id = (
                changes["parent_id"] if "parent_id" in changes else record.parent_id
            )
            if proposed_parent_id is not None and not isinstance(proposed_parent_id, str):
                raise ValueError("父集合无效")
            _validate_collection_change(
                records,
                owner_id=owner_id,
                name=proposed_name,
                parent_id=proposed_parent_id,
                collection_id=record.id,
            )
            for key in ("name", "description", "parent_id"):
                if key in changes:
                    setattr(record, key, changes[key])
            record.updated_at = now()
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("同级集合名称已存在") from exc
            await session.refresh(record)
            return record

    async def delete_collection(self, collection_id: str, owner_id: str) -> bool:
        async with get_session_factory()() as session:
            records = list(
                await session.scalars(
                    select(Collection)
                    .where(Collection.owner_id == owner_id)
                    .order_by(Collection.id)
                    .with_for_update()
                )
            )
            record = next((item for item in records if item.id == collection_id), None)
            if not record:
                return False
            children = [item for item in records if item.parent_id == collection_id]
            siblings = [
                item
                for item in records
                if item.parent_id == record.parent_id and item.id != record.id
            ]
            if any(
                child.name.casefold() == sibling.name.casefold()
                for child in children
                for sibling in siblings
            ):
                raise ValueError("子集合提升后会与同级集合重名，请先重命名")
            for child in children:
                child.parent_id = record.parent_id
                child.updated_at = now()
            await session.delete(record)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("子集合提升后会与同级集合重名，请先重命名") from exc
            return True

    async def set_paper_collection(
        self, collection_id: str, paper_id: str, owner_id: str, assigned: bool
    ) -> bool:
        async with get_session_factory()() as session:
            collection = await session.scalar(
                select(Collection.id).where(
                    Collection.id == collection_id, Collection.owner_id == owner_id
                )
            )
            paper = await session.scalar(
                select(Paper.id).where(Paper.id == paper_id, Paper.owner_id == owner_id)
            )
            if not collection or not paper:
                return False
            exists = await session.scalar(
                select(paper_collections.c.paper_id).where(
                    paper_collections.c.paper_id == paper_id,
                    paper_collections.c.collection_id == collection_id,
                )
            )
            if assigned and not exists:
                await session.execute(
                    insert(paper_collections).values(paper_id=paper_id, collection_id=collection_id)
                )
            elif not assigned and exists:
                await session.execute(
                    delete(paper_collections).where(
                        paper_collections.c.paper_id == paper_id,
                        paper_collections.c.collection_id == collection_id,
                    )
                )
            await session.commit()
            return True

    async def create_or_resume_translation(
        self,
        paper_id: str,
        owner_id: str,
        target_language: str,
        priority_page: int | None,
        *,
        model_available: bool,
        refresh: bool = False,
    ) -> PaperTranslation | None:
        async with get_session_factory()() as session:
            # 锁论文即可串行化同一篇论文的“翻译 + 作业”创建，唯一约束作为第二道门禁。
            paper = await session.scalar(
                select(Paper)
                .where(Paper.id == paper_id, Paper.owner_id == owner_id)
                .with_for_update()
            )
            if not paper:
                return None
            if paper.status not in {PaperStatus.ready, PaperStatus.partial}:
                raise TranslationSourceUnavailableError("文献尚未完成页面解析")
            source_pages = list(
                await session.scalars(
                    select(PaperPage)
                    .where(PaperPage.paper_id == paper_id)
                    .order_by(PaperPage.physical_page)
                )
            )
            if not source_pages:
                raise TranslationSourceUnavailableError("文献尚未完成页面解析")
            page_numbers = {item.physical_page for item in source_pages}
            if priority_page is not None and priority_page not in page_numbers:
                raise ValueError("优先翻译页不存在")
            revision = source_revision([(item.physical_page, item.text) for item in source_pages])
            translation = await session.scalar(
                select(PaperTranslation)
                .where(
                    PaperTranslation.paper_id == paper_id,
                    PaperTranslation.target_language == target_language,
                )
                .with_for_update()
            )
            if translation is None:
                translation_created = True
                restart_requested = False
                source_changed = False
                translation = PaperTranslation(
                    paper_id=paper_id,
                    owner_id=owner_id,
                    target_language=target_language,
                    source_revision=revision,
                    status="queued",
                    total_pages=len(source_pages),
                    priority_page=priority_page,
                )
                session.add(translation)
                await session.flush()
            else:
                translation_created = False
                source_changed = (
                    translation.error_code == "SOURCE_CHANGED"
                    or translation.source_revision != revision
                )
                restart_requested = (
                    refresh
                    or source_changed
                    or translation.cancel_requested
                    or (translation.status in {"cancelled", "failed", "partial"})
                )
                translation.source_revision = revision
                translation.priority_page = priority_page
                if restart_requested:
                    translation.cancel_requested = False
                    translation.error_code = None
                    translation.error_message = None
                    translation.updated_at = now()

            # 先取得唯一 Job 锁，再修改任何页状态。重启会清除旧 token，确保旧
            # Worker 即使仍持有旧来源文本，也无法通过最终写入门禁。
            translation_job = await session.scalar(
                select(Job).where(Job.translation_id == translation.id).with_for_update()
            )
            if translation_job is None:
                translation_job_created = True
                translation_job = Job(
                    paper_id=paper_id,
                    translation_id=translation.id,
                    type="translate_paper",
                    status=JobStatus.queued,
                )
                session.add(translation_job)
                await session.flush()
            elif restart_requested:
                translation_job_created = False
                translation_job.status = JobStatus.queued
                translation_job.progress = 0
                translation_job.attempts = 0
                translation_job.error_code = None
                translation_job.error_message = None
                translation_job.available_at = now()
                translation_job.claimed_at = None
                translation_job.claim_token = None
            else:
                translation_job_created = False

            existing_pages = {
                item.physical_page: item
                for item in await session.scalars(
                    select(PaperTranslationPage).where(
                        PaperTranslationPage.translation_id == translation.id
                    )
                )
            }
            for source_page in source_pages:
                text_hash = source_text_hash(source_page.text)
                page = existing_pages.pop(source_page.physical_page, None)
                initial_status = "queued" if source_page.text.strip() else "no_text"
                if page is None:
                    page = PaperTranslationPage(
                        translation_id=translation.id,
                        physical_page=source_page.physical_page,
                        status=initial_status,
                        source_text_hash=text_hash,
                    )
                    session.add(page)
                elif refresh or source_changed or page.source_text_hash != text_hash:
                    page.source_text_hash = text_hash
                    page.status = initial_status
                    page.translated_text = None
                    page.attempts = 0
                    page.error_code = None
                    page.error_message = None
                elif restart_requested and page.status not in {"completed", "no_text"}:
                    page.status = initial_status
                    page.translated_text = None
                    page.attempts = 0
                    page.error_code = None
                    page.error_message = None
                page.priority = (
                    0
                    if source_page.physical_page == priority_page
                    else 1000 + source_page.physical_page
                )
                page.updated_at = now()
            for stale_page in existing_pages.values():
                await session.delete(stale_page)
            await session.flush()

            pages = list(
                await session.scalars(
                    select(PaperTranslationPage).where(
                        PaperTranslationPage.translation_id == translation.id
                    )
                )
            )
            translation.total_pages = len(pages)
            translation.completed_pages = sum(item.status == "completed" for item in pages)
            translation.failed_pages = sum(item.status == "failed" for item in pages)
            queued_pages = [item for item in pages if item.status == "queued"]
            running_pages = [item for item in pages if item.status == "running"]
            preserve_active_schedule = (
                not translation_created
                and not translation_job_created
                and not restart_requested
                and translation.status in {"queued", "running"}
                and translation_job.status in {JobStatus.queued, JobStatus.running}
            )
            if preserve_active_schedule:
                # 幂等 POST 不得刷新退避时间、尝试次数或 fencing token。
                await session.commit()
                await session.refresh(translation)
                return translation
            if queued_pages and not model_available:
                for page in queued_pages:
                    page.status = "failed"
                    page.error_code = "MODEL_NOT_CONFIGURED"
                    page.error_message = "尚未配置可用于全文翻译的模型"
                translation.completed_pages = sum(item.status == "completed" for item in pages)
                translation.failed_pages = sum(item.status == "failed" for item in pages)
                translation.status = "partial" if translation.completed_pages else "failed"
                translation.error_code = "MODEL_NOT_CONFIGURED"
                translation.error_message = "尚未配置可用于全文翻译的模型"
                translation_job.status = JobStatus.failed
                translation_job.error_code = "MODEL_NOT_CONFIGURED"
                translation_job.error_message = "尚未配置可用于全文翻译的模型"
            elif queued_pages:
                translation.status = "running" if running_pages else "queued"
                if translation_job.status != JobStatus.running and (
                    translation_job_created or restart_requested
                ):
                    translation_job.status = JobStatus.queued
                    translation_job.progress = 0
                    translation_job.attempts = 0
                    translation_job.error_code = None
                    translation_job.error_message = None
                    translation_job.available_at = now()
                    translation_job.claimed_at = None
                    translation_job.claim_token = None
            elif running_pages:
                translation.status = "running"
            elif all(item.status == "no_text" for item in pages):
                translation.status = "completed"
                translation.error_code = "NO_TRANSLATABLE_TEXT"
                translation.error_message = "此文献暂无可翻译的页面文本"
                translation_job.status = JobStatus.completed
                translation_job.progress = 100
                translation_job.error_code = "NO_TRANSLATABLE_TEXT"
                translation_job.error_message = "此文献暂无可翻译的页面文本"
            elif translation.failed_pages:
                translation.status = "partial" if translation.completed_pages else "failed"
                translation_job.status = JobStatus.failed
            else:
                translation.status = "completed"
                translation_job.status = JobStatus.completed
                translation_job.progress = 100
                translation_job.error_code = None
                translation_job.error_message = None
            translation_job.updated_at = now()
            translation.updated_at = now()
            await session.commit()
            await session.refresh(translation)
            return translation

    async def get_owned_translation(
        self, paper_id: str, translation_id: str, owner_id: str
    ) -> PaperTranslation | None:
        async with get_session_factory()() as session:
            return await session.scalar(
                select(PaperTranslation)
                .join(Paper, Paper.id == PaperTranslation.paper_id)
                .where(
                    PaperTranslation.id == translation_id,
                    PaperTranslation.paper_id == paper_id,
                    Paper.owner_id == owner_id,
                )
            )

    async def list_translation_pages(
        self, translation_id: str, owner_id: str
    ) -> list[PaperTranslationPage]:
        async with get_session_factory()() as session:
            return list(
                await session.scalars(
                    select(PaperTranslationPage)
                    .join(
                        PaperTranslation,
                        PaperTranslation.id == PaperTranslationPage.translation_id,
                    )
                    .join(Paper, Paper.id == PaperTranslation.paper_id)
                    .where(
                        PaperTranslationPage.translation_id == translation_id,
                        Paper.owner_id == owner_id,
                    )
                    .order_by(PaperTranslationPage.physical_page)
                )
            )

    async def get_owned_translation_page(
        self, paper_id: str, translation_id: str, physical_page: int, owner_id: str
    ) -> PaperTranslationPage | None:
        async with get_session_factory()() as session:
            return await session.scalar(
                select(PaperTranslationPage)
                .join(
                    PaperTranslation,
                    PaperTranslation.id == PaperTranslationPage.translation_id,
                )
                .join(Paper, Paper.id == PaperTranslation.paper_id)
                .where(
                    PaperTranslation.id == translation_id,
                    PaperTranslation.paper_id == paper_id,
                    Paper.owner_id == owner_id,
                    PaperTranslationPage.physical_page == physical_page,
                )
            )

    async def cancel_owned_translation(
        self, paper_id: str, translation_id: str, owner_id: str
    ) -> PaperTranslation | None:
        async with get_session_factory()() as session:
            paper = await session.scalar(
                select(Paper)
                .where(Paper.id == paper_id, Paper.owner_id == owner_id)
                .with_for_update()
            )
            if not paper:
                return None
            translation = await session.scalar(
                select(PaperTranslation)
                .where(
                    PaperTranslation.id == translation_id,
                    PaperTranslation.paper_id == paper_id,
                )
                .with_for_update()
            )
            if not translation:
                return None
            translation_job = await session.scalar(
                select(Job).where(Job.translation_id == translation_id).with_for_update()
            )
            # 重复取消不会清空已成功页，也不会改变已完成翻译。
            if translation.status != "completed":
                translation.cancel_requested = True
                translation.status = "cancelled"
                translation.error_code = "TRANSLATION_CANCELLED"
                translation.error_message = "全文翻译已取消"
                translation.updated_at = now()
                await session.execute(
                    update(PaperTranslationPage)
                    .where(
                        PaperTranslationPage.translation_id == translation_id,
                        PaperTranslationPage.status.not_in(["completed", "no_text"]),
                    )
                    .values(
                        status="cancelled",
                        error_code="TRANSLATION_CANCELLED",
                        error_message="全文翻译已取消",
                        updated_at=now(),
                    )
                )
                if translation_job and translation_job.status in {
                    JobStatus.queued,
                    JobStatus.running,
                }:
                    translation_job.status = JobStatus.completed
                    translation_job.error_code = "TRANSLATION_CANCELLED"
                    translation_job.error_message = "用户已取消全文翻译"
                    translation_job.claim_token = None
                    translation_job.claimed_at = None
                    translation_job.updated_at = now()
            await session.commit()
            await session.refresh(translation)
            return translation

    async def list_jobs(self) -> list[Job]:
        async with get_session_factory()() as session:
            result = await session.scalars(select(Job).order_by(Job.created_at.desc()).limit(200))
            return list(result)

    async def retry_job(self, job_id: str) -> Job | None:
        async with get_session_factory()() as session:
            snapshot = await session.get(Job, job_id)
            if not snapshot or snapshot.status != JobStatus.failed:
                return None
            translation: PaperTranslation | None = None
            if snapshot.type == "translate_paper" and snapshot.translation_id:
                translation_snapshot = await session.get(PaperTranslation, snapshot.translation_id)
                if not translation_snapshot:
                    return None
                paper = await session.scalar(
                    select(Paper).where(Paper.id == translation_snapshot.paper_id).with_for_update()
                )
                translation = await session.scalar(
                    select(PaperTranslation)
                    .where(PaperTranslation.id == translation_snapshot.id)
                    .with_for_update()
                )
                job = await session.scalar(
                    select(Job)
                    .where(
                        Job.id == job_id,
                        Job.translation_id == translation_snapshot.id,
                        Job.status == JobStatus.failed,
                    )
                    .with_for_update()
                )
                if not paper or not translation or not job:
                    return None
                pages = list(
                    await session.scalars(
                        select(PaperPage)
                        .where(PaperPage.paper_id == paper.id)
                        .order_by(PaperPage.physical_page)
                    )
                )
                current_revision = (
                    source_revision([(page.physical_page, page.text) for page in pages])
                    if pages
                    else None
                )
                if (
                    paper.status not in {PaperStatus.ready, PaperStatus.partial}
                    or translation.cancel_requested
                    or translation.error_code == "SOURCE_CHANGED"
                    or current_revision != translation.source_revision
                ):
                    return None
            else:
                job = await session.scalar(
                    select(Job)
                    .where(Job.id == job_id, Job.status == JobStatus.failed)
                    .with_for_update()
                )
                if not job:
                    return None
            job.status = JobStatus.queued
            job.progress = 0
            job.attempts = 0
            job.error_code = None
            job.error_message = None
            job.available_at = now()
            job.claimed_at = None
            job.claim_token = None
            job.updated_at = now()
            if job.paper_id and job.type in ARTIFACT_JOB_TYPES.values():
                artifact_type = next(
                    key for key, value in ARTIFACT_JOB_TYPES.items() if value == job.type
                )
                artifact = await session.scalar(
                    select(PaperArtifact)
                    .where(
                        PaperArtifact.paper_id == job.paper_id,
                        PaperArtifact.type == artifact_type,
                    )
                    .with_for_update()
                )
                if artifact and artifact.status != "ready":
                    artifact.status = "processing"
                    artifact.fallback_reason = None
                    artifact.structured_payload = {}
                    artifact.markdown = ""
                    artifact.updated_at = now()
            if job.type == "translate_paper" and job.translation_id:
                if translation:
                    translation.status = "queued"
                    translation.error_code = None
                    translation.error_message = None
                    translation.updated_at = now()
                await session.execute(
                    update(PaperTranslationPage)
                    .where(
                        PaperTranslationPage.translation_id == job.translation_id,
                        PaperTranslationPage.status == "failed",
                    )
                    .values(
                        status="queued",
                        attempts=0,
                        error_code=None,
                        error_message=None,
                        updated_at=now(),
                    )
                )
                if translation:
                    translation.failed_pages = int(
                        await session.scalar(
                            select(func.count())
                            .select_from(PaperTranslationPage)
                            .where(
                                PaperTranslationPage.translation_id == job.translation_id,
                                PaperTranslationPage.status == "failed",
                            )
                        )
                        or 0
                    )
            await session.commit()
            await session.refresh(job)
            return job

    async def create_chat_session(
        self,
        user_id: str,
        title: str,
        session_type: str,
        paper_id: str | None,
        collection_id: str | None,
    ) -> ChatSession:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("会话标题不能为空")
        record = ChatSession(
            user_id=user_id,
            title=normalized_title,
            type=session_type,
            paper_id=paper_id,
            collection_id=collection_id,
        )
        async with get_session_factory()() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def _chat_session_with_current_run_sql(
        self, session: AsyncSession, record: ChatSession
    ) -> ChatSession:
        latest = await session.scalar(
            select(AgentRun)
            .where(
                AgentRun.session_id == record.id,
                AgentRun.user_id == record.user_id,
            )
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        record.current_run_id = latest.id if latest else None
        record.current_run_status = latest.status if latest else None
        return record

    async def list_chat_sessions(self, user_id: str) -> list[ChatSession]:
        async with get_session_factory()() as session:
            records = list(
                await session.scalars(
                    select(ChatSession)
                    .where(ChatSession.user_id == user_id)
                    .order_by(ChatSession.updated_at.desc())
                )
            )
            for record in records:
                await self._chat_session_with_current_run_sql(session, record)
            return records

    async def get_owned_chat_session(self, session_id: str, user_id: str) -> ChatSession | None:
        async with get_session_factory()() as session:
            record = await session.scalar(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.user_id == user_id,
                )
            )
            if record is None:
                return None
            return await self._chat_session_with_current_run_sql(session, record)

    async def update_owned_chat_session(
        self, session_id: str, user_id: str, title: str
    ) -> ChatSession | None:
        async with get_session_factory()() as session:
            record = await session.scalar(
                select(ChatSession)
                .where(
                    ChatSession.id == session_id,
                    ChatSession.user_id == user_id,
                )
                .with_for_update()
            )
            if record is None:
                return None
            normalized_title = title.strip()
            if not normalized_title:
                raise ValueError("会话标题不能为空")
            record.title = normalized_title
            record.updated_at = now()
            await session.commit()
            await session.refresh(record)
            return await self._chat_session_with_current_run_sql(session, record)

    async def list_session_thread_ids(self, session_id: str, user_id: str) -> list[str] | None:
        async with get_session_factory()() as session:
            owned = await session.scalar(
                select(ChatSession.id).where(
                    ChatSession.id == session_id,
                    ChatSession.user_id == user_id,
                )
            )
            if owned is None:
                return None
            return list(
                await session.scalars(
                    select(AgentRun.thread_id).where(
                        AgentRun.session_id == session_id,
                        AgentRun.user_id == user_id,
                    )
                )
            )

    async def delete_owned_chat_session(self, session_id: str, user_id: str) -> bool:
        async with get_session_factory()() as session:
            record = await session.scalar(
                select(ChatSession)
                .where(
                    ChatSession.id == session_id,
                    ChatSession.user_id == user_id,
                )
                .with_for_update()
            )
            if record is None:
                return False
            active = await session.scalar(
                select(AgentRun.id).where(
                    AgentRun.session_id == session_id,
                    AgentRun.status.in_(["pending", "running", "interrupted"]),
                )
            )
            if active is not None:
                raise ChatActiveRunError("会话仍有运行中或等待确认的任务，请先取消")
            run_ids = list(
                await session.scalars(select(AgentRun.id).where(AgentRun.session_id == session_id))
            )
            if run_ids:
                # jobs.agent_run_id 使用 SET NULL，以便管理员保留一般任务记录；
                # 删除会话时必须显式清理 Agent 作业，避免留下无归属历史。
                await session.execute(delete(Job).where(Job.agent_run_id.in_(run_ids)))
            await session.delete(record)
            await session.commit()
            return True

    async def list_chat_messages(self, session_id: str, user_id: str) -> list[ChatMessage] | None:
        async with get_session_factory()() as session:
            owned = await session.scalar(
                select(ChatSession.id).where(
                    ChatSession.id == session_id,
                    ChatSession.user_id == user_id,
                )
            )
            if owned is None:
                return None
            return list(
                await session.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.sequence)
                )
            )

    async def submit_chat_message(
        self,
        session_id: str,
        user_id: str,
        content: str,
        client_message_id: str,
        request_hash: str,
        scope_snapshot: dict,
    ) -> ChatSubmission | None:
        async with get_session_factory()() as session:
            chat_session = await session.scalar(
                select(ChatSession)
                .where(
                    ChatSession.id == session_id,
                    ChatSession.user_id == user_id,
                )
                .with_for_update()
            )
            if chat_session is None:
                return None
            existing = await session.scalar(
                select(ChatMessage).where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.client_message_id == client_message_id,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ChatIdempotencyConflictError("客户端消息 ID 已用于不同请求")
                run = await session.get(AgentRun, existing.run_id)
                if run is None:
                    raise RuntimeError("幂等消息关联的 Agent Run 不存在")
                return ChatSubmission(existing, run, True)
            active = await session.scalar(
                select(AgentRun.id).where(
                    AgentRun.session_id == session_id,
                    AgentRun.status.in_(["pending", "running", "interrupted"]),
                )
            )
            if active is not None:
                raise ChatActiveRunError("当前会话已有正在运行或等待确认的任务")
            next_sequence = 1 + int(
                await session.scalar(
                    select(func.max(ChatMessage.sequence)).where(
                        ChatMessage.session_id == session_id
                    )
                )
                or 0
            )
            run_id = str(uuid.uuid4())
            user_message = ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="user",
                sequence=next_sequence,
                status="completed",
                content=content,
                citations=[],
                run_id=run_id,
                client_message_id=client_message_id,
                request_hash=request_hash,
            )
            assistant_message = ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="assistant",
                sequence=next_sequence + 1,
                status="pending",
                content="",
                citations=[],
                run_id=run_id,
            )
            run = AgentRun(
                id=run_id,
                user_id=user_id,
                session_id=session_id,
                thread_id=f"{user_id}:{session_id}:{run_id}",
                status="pending",
                cancel_requested=False,
                scope_snapshot=dict(scope_snapshot),
                orchestration_version=str(
                    scope_snapshot.get("orchestration_version", "single_agent_v1")
                ),
                user_message_id=user_message.id,
                assistant_message_id=assistant_message.id,
                request_hash=request_hash,
            )
            job = Job(
                agent_run_id=run_id,
                type="agent_run",
                status=JobStatus.queued,
            )
            session.add_all([user_message, assistant_message, run, job])
            if chat_session.title == "新会话":
                chat_session.title = content.strip().replace("\n", " ")[:60]
            chat_session.updated_at = now()
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                async with get_session_factory()() as retry_session:
                    replay = await retry_session.scalar(
                        select(ChatMessage).where(
                            ChatMessage.session_id == session_id,
                            ChatMessage.client_message_id == client_message_id,
                        )
                    )
                    if replay is not None:
                        if replay.request_hash != request_hash:
                            raise ChatIdempotencyConflictError(
                                "客户端消息 ID 已用于不同请求"
                            ) from exc
                        replay_run = await retry_session.get(AgentRun, replay.run_id)
                        if replay_run is None:
                            raise RuntimeError("幂等消息关联的 Agent Run 不存在") from exc
                        return ChatSubmission(replay, replay_run, True)
                    retry_active = await retry_session.scalar(
                        select(AgentRun.id).where(
                            AgentRun.session_id == session_id,
                            AgentRun.status.in_(["pending", "running", "interrupted"]),
                        )
                    )
                    if retry_active is not None:
                        raise ChatActiveRunError("当前会话已有正在运行或等待确认的任务") from exc
                raise
            await session.refresh(user_message)
            await session.refresh(run)
            return ChatSubmission(user_message, run, False)

    async def update_session_compaction(
        self,
        session_id: str,
        user_id: str,
        *,
        compact_summary: dict,
        compacted_through_message_id: str | None,
        entity_state: dict,
    ) -> ChatSession | None:
        async with get_session_factory()() as session:
            record = await session.scalar(
                select(ChatSession)
                .where(ChatSession.id == session_id, ChatSession.user_id == user_id)
                .with_for_update()
            )
            if not record:
                return None
            record.compact_summary = dict(compact_summary)
            record.summary_version = 1
            record.compacted_through_message_id = compacted_through_message_id
            record.entity_state = dict(entity_state)
            record.updated_at = now()
            await session.commit()
            await session.refresh(record)
            return record

    async def mark_embedding_contract_stale(self, fingerprint: str | None) -> int:
        async with get_session_factory()() as session:
            conditions = [Paper.embedding_status == "ready"]
            if fingerprint:
                conditions.append(
                    or_(
                        Paper.embedding_fingerprint.is_(None),
                        Paper.embedding_fingerprint != fingerprint,
                    )
                )
            result = await session.execute(
                update(Paper).where(*conditions).values(embedding_status="stale")
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def embedding_contract_counts(self, fingerprint: str | None) -> dict[str, int]:
        async with get_session_factory()() as session:
            grouped = await session.execute(
                select(Paper.embedding_status, func.count(Paper.id)).group_by(
                    Paper.embedding_status
                )
            )
            statuses = {str(status): int(count) for status, count in grouped.all()}
            ready_current = 0
            if fingerprint:
                ready_current = int(
                    await session.scalar(
                        select(func.count(Paper.id)).where(
                            Paper.embedding_status == "ready",
                            Paper.embedding_fingerprint == fingerprint,
                        )
                    )
                    or 0
                )
            return {
                "total": sum(statuses.values()),
                "ready": statuses.get("ready", 0),
                "ready_current": ready_current,
                "stale": statuses.get("stale", 0),
                "unavailable": statuses.get("unavailable", 0),
                "failed": statuses.get("failed", 0),
            }

    async def list_memories(self, user_id: str, *, enabled_only: bool = False) -> list[MemoryItem]:
        async with get_session_factory()() as session:
            query = select(MemoryItem).where(MemoryItem.user_id == user_id)
            if enabled_only:
                query = query.where(MemoryItem.enabled.is_(True))
            query = query.order_by(MemoryItem.pinned.desc(), MemoryItem.updated_at.desc())
            return list((await session.scalars(query)).all())

    async def create_memory_item(self, record: MemoryItemRecord) -> MemoryItem:
        async with get_session_factory()() as session:
            existing = await session.scalar(
                select(MemoryItem)
                .where(
                    MemoryItem.user_id == record.user_id,
                    MemoryItem.normalized_hash == record.normalized_hash,
                )
                .with_for_update()
            )
            if existing:
                existing.enabled = True
                existing.pinned = existing.pinned or record.pinned
                existing.confidence = max(existing.confidence, record.confidence)
                existing.updated_at = now()
                await session.commit()
                await session.refresh(existing)
                return existing
            active_count = await session.scalar(
                select(func.count(MemoryItem.id)).where(
                    MemoryItem.user_id == record.user_id,
                    MemoryItem.enabled.is_(True),
                )
            )
            if int(active_count or 0) >= 200:
                raise ValueError("长期记忆已达到 200 条上限")
            item = MemoryItem(
                id=record.id,
                user_id=record.user_id,
                type=record.type,
                value=record.value,
                normalized_hash=record.normalized_hash,
                confidence=record.confidence,
                source_kind=record.source_kind,
                source_session_id=record.source_session_id,
                source_message_id=record.source_message_id,
                source_excerpt=record.source_excerpt,
                pinned=record.pinned,
                enabled=record.enabled,
                embedding=record.embedding,
                embedding_fingerprint=record.embedding_fingerprint,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            session.add(item)
            session.add(
                MemoryItemVersion(
                    memory_item_id=item.id,
                    version=1,
                    value=item.value,
                    confidence=item.confidence,
                    status="active",
                    source_kind=item.source_kind,
                    source_excerpt=item.source_excerpt,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                duplicate = await session.scalar(
                    select(MemoryItem).where(
                        MemoryItem.user_id == record.user_id,
                        MemoryItem.normalized_hash == record.normalized_hash,
                    )
                )
                if duplicate:
                    return duplicate
                raise ValueError("长期记忆保存失败") from exc
            await session.refresh(item)
            return item

    async def update_owned_memory(
        self, memory_id: str, user_id: str, **changes: object
    ) -> MemoryItem | None:
        async with get_session_factory()() as session:
            item = await session.scalar(
                select(MemoryItem)
                .where(MemoryItem.id == memory_id, MemoryItem.user_id == user_id)
                .with_for_update()
            )
            if not item:
                return None
            if "normalized_hash" in changes:
                collision = await session.scalar(
                    select(MemoryItem.id).where(
                        MemoryItem.user_id == user_id,
                        MemoryItem.id != memory_id,
                        MemoryItem.normalized_hash == changes["normalized_hash"],
                    )
                )
                if collision:
                    raise ValueError("相同记忆已经存在")
            if any(key in changes for key in ("value", "confidence", "type")):
                await session.execute(
                    update(MemoryItemVersion)
                    .where(
                        MemoryItemVersion.memory_item_id == memory_id,
                        MemoryItemVersion.status == "active",
                    )
                    .values(status="superseded")
                )
                latest_version = await session.scalar(
                    select(func.max(MemoryItemVersion.version)).where(
                        MemoryItemVersion.memory_item_id == memory_id
                    )
                )
                session.add(
                    MemoryItemVersion(
                        memory_item_id=memory_id,
                        version=int(latest_version or 0) + 1,
                        value=str(changes.get("value", item.value)),
                        confidence=float(changes.get("confidence", item.confidence)),
                        status="active",
                        source_kind="user_edit",
                        source_excerpt=item.source_excerpt,
                    )
                )
            if any(key in changes for key in ("value", "type")):
                item.embedding = changes.get("embedding")
                item.embedding_fingerprint = changes.get("embedding_fingerprint")
            for key in (
                "type",
                "value",
                "normalized_hash",
                "confidence",
                "pinned",
                "enabled",
                "embedding",
                "embedding_fingerprint",
            ):
                if key in changes:
                    setattr(item, key, changes[key])
            item.updated_at = now()
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("相同记忆已经存在") from exc
            await session.refresh(item)
            return item

    async def delete_owned_memory(self, memory_id: str, user_id: str) -> bool:
        async with get_session_factory()() as session:
            result = await session.execute(
                delete(MemoryItem).where(MemoryItem.id == memory_id, MemoryItem.user_id == user_id)
            )
            await session.commit()
            return bool(result.rowcount)

    async def clear_memories(self, user_id: str) -> int:
        async with get_session_factory()() as session:
            result = await session.execute(delete(MemoryItem).where(MemoryItem.user_id == user_id))
            await session.commit()
            return int(result.rowcount or 0)

    async def create_agent_run(
        self, run_id: str, user_id: str, session_id: str, thread_id: str
    ) -> AgentRun:
        record = AgentRun(
            id=run_id,
            user_id=user_id,
            session_id=session_id,
            thread_id=thread_id,
            status="pending",
        )
        async with get_session_factory()() as session:
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("Agent Run 已存在") from exc
            await session.refresh(record)
            return record

    async def get_owned_agent_run(self, run_id: str, user_id: str) -> AgentRun | None:
        async with get_session_factory()() as session:
            return await session.scalar(
                select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user_id)
            )

    async def update_owned_agent_run(
        self, run_id: str, user_id: str, **changes: object
    ) -> AgentRun | None:
        async with get_session_factory()() as session:
            record = await session.scalar(
                select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user_id)
            )
            if not record:
                return None
            for key in (
                "status",
                "tool_steps",
                "duration_ms",
                "token_usage",
                "result_summary",
                "pending_action",
                "error_code",
            ):
                if key in changes:
                    setattr(record, key, changes[key])
            record.updated_at = now()
            await session.commit()
            await session.refresh(record)
            return record
