"""PaperLeaf FastAPI 应用入口。"""

import asyncio
import hashlib
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from prometheus_client import make_asgi_app

from .agent.function_tools import FunctionToolHarness
from .agent.graph import (
    build_agent_graph,
    build_configured_answerer,
    build_configured_evidence_support_grader,
)
from .agent.memory import MEMORY_TYPES, memory_hash, normalize_memory_value
from .agent.skills import SkillRegistry
from .agent.tools import DemoLibrarySearch, SQLLibrarySearch
from .agent_execution import execute_agent_run
from .artifacts import (
    load_paper_evidence,
    load_paper_source_revision,
    validate_structure_payload,
    validate_summary_payload,
)
from .arxiv_import import import_arxiv_paper, import_public_paper
from .arxiv_service import (
    search_arxiv,
    search_related_arxiv,
)
from .config import Settings, settings
from .discovery import (
    build_discovery_profile,
    collect_recommendations,
    embed_discovery_texts,
    with_indexed_text,
)
from .embedding_contract import configured_embedding_contract, vector_matches_contract
from .harness_observability import aggregate_harness_metrics
from .mcp_gateway import McpGateway, McpGatewayError
from .model_runtime import build_model_router
from .models import PaperStatus, UserRole
from .rag.answer_quality import AnswerQualityPolicy
from .rag.citations import Evidence
from .rag.retrieval_quality import EvidenceQualityPolicy
from .rag_observability import aggregate_rag_runs, classify_intent
from .repository import (
    ChatActiveRunError,
    ChatIdempotencyConflictError,
    CurrentAdminProtectionError,
    DiscoveryBatchRecord,
    DiscoveryItemRecord,
    LastAdminProtectionError,
    ManagedUserNotFoundError,
    MemoryItemRecord,
    MemoryRepository,
    PaperRecord,
    SQLAlchemyRepository,
    TranslationSourceUnavailableError,
    UserRecord,
)
from .runtime_store import RuntimeStore, create_runtime_store
from .schemas import (
    AgentResumeRequest,
    AgentRunEventRead,
    AgentRunRead,
    ArxivImportRequest,
    ArxivSearchResponse,
    ChangePasswordRequest,
    ChatMessageRead,
    ChatMessageRequest,
    ChatSessionCreate,
    ChatSessionRead,
    ChatSessionUpdate,
    ChatSubmissionRead,
    CollectionCreate,
    CollectionRead,
    CollectionUpdate,
    DiscoveryFeedbackRequest,
    DiscoveryFeedbackResponse,
    DiscoveryMetricsResponse,
    DiscoveryRecommendation,
    DiscoveryRecommendationResponse,
    JobRead,
    LoginRequest,
    McpServerUpdate,
    MemoryClearRead,
    MemoryCreate,
    MemoryListRead,
    MemoryRead,
    MemoryUpdate,
    PaperBulkActionRequest,
    PaperBulkActionResponse,
    PaperRead,
    PaperTranslationRead,
    PaperUpdate,
    StructureGraphResponse,
    SummaryResponse,
    TranslationCreate,
    TranslationPageRead,
    UserCreate,
    UserPreferences,
    UserPreferencesRead,
    UserPreferencesUpdate,
    UserRead,
    UserUpdate,
)
from .security import csrf_matches, new_csrf_token, new_session_token, verify_password
from .selection_context import match_selection_to_page
from .storage import ObjectStorage, create_storage, parse_byte_range, validate_pdf

_PUBLIC_AGENT_NODES = {
    "resolve_context",
    "select_skill",
    "validate_request",
    "retrieve_library",
    "grade_evidence",
    "generate_answer",
    "validate_citations",
    "grade_answer_support",
    "suppress_unsupported_answer",
    "finalize",
    "abstain",
    "search_arxiv",
    "propose_import",
}


class AppServices:
    def __init__(
        self,
        config: Settings,
        repository: Optional[MemoryRepository] = None,
        storage: Optional[ObjectStorage] = None,
        runtime_store: Optional[RuntimeStore] = None,
    ) -> None:
        self.config = config
        self.repository = repository or (
            MemoryRepository(config.session_secret)
            if config.is_demo
            else SQLAlchemyRepository(config.session_secret)
        )
        self.storage = storage or create_storage(config)
        self.runtime_store = runtime_store or create_runtime_store(config)
        self.model_router = build_model_router(config)
        self.retriever = (
            DemoLibrarySearch() if config.is_demo else SQLLibrarySearch(config, self.model_router)
        )
        self.agent_graph = self.build_agent_graph()
        self.skill_registry = SkillRegistry.default()
        self.mcp_gateway = McpGateway(self.repository, self.runtime_store, config)

        async def confirmed_importer(user_id: str, candidate: dict[str, Any]) -> Any:
            return await import_public_paper(
                candidate,
                user_id,
                config=self.config,
                repository=self.repository,
                storage=self.storage,
            )

        self.function_tool_harness = FunctionToolHarness(
            self.repository,
            self.retriever,
            self.model_router,
            mcp_gateway=self.mcp_gateway if config.mcp_enabled else None,
            confirmed_importer=confirmed_importer,
        )
        self.checkpointer: Optional[Any] = None
        self._agent_tasks: dict[str, asyncio.Task[Any]] = {}
        self._agent_tasks_lock = threading.RLock()

    def build_agent_graph(self, checkpointer: Optional[Any] = None) -> Any:
        """生产重建 Graph 时保持与 App 相同的模型和质量策略。"""

        return build_agent_graph(
            retriever=self.retriever,
            answerer=build_configured_answerer(self.config, self.model_router),
            checkpointer=checkpointer,
            quality_policy=EvidenceQualityPolicy(
                min_confidence=self.config.evidence_min_confidence,
                min_vector_score=self.config.evidence_min_vector_score,
                min_lexical_coverage=self.config.evidence_min_lexical_coverage,
            ),
            answer_quality_policy=AnswerQualityPolicy(
                min_citation_coverage=self.config.answer_min_citation_coverage,
                min_claim_lexical_support=self.config.answer_min_claim_lexical_support,
                min_model_support_confidence=self.config.answer_min_support_confidence,
            ),
            support_grader=build_configured_evidence_support_grader(self.config, self.model_router),
        )

    async def register_agent_task(self, run_id: str, task: asyncio.Task[Any]) -> None:
        with self._agent_tasks_lock:
            self._agent_tasks[run_id] = task

    async def unregister_agent_task(self, run_id: str, task: asyncio.Task[Any]) -> None:
        with self._agent_tasks_lock:
            if self._agent_tasks.get(run_id) is task:
                self._agent_tasks.pop(run_id, None)

    async def cancel_agent_task(self, run_id: str) -> bool:
        with self._agent_tasks_lock:
            task = self._agent_tasks.get(run_id)
            if not task or task.done():
                return False
            task.cancel()
            return True

    async def launch_local_agent_run(self, run_id: str) -> None:
        claim_token = await self.repository.claim_agent_run_job(run_id)
        if not claim_token:
            return

        async def runner() -> None:
            try:
                await execute_agent_run(
                    self.repository,
                    self.agent_graph,
                    run_id,
                    claim_token,
                    answer_quality_policy=AnswerQualityPolicy(
                        min_citation_coverage=self.config.answer_min_citation_coverage,
                        min_claim_lexical_support=(self.config.answer_min_claim_lexical_support),
                        min_model_support_confidence=(self.config.answer_min_support_confidence),
                    ),
                    harness_config=self.config,
                    skill_registry=self.skill_registry,
                    function_tool_harness=self.function_tool_harness,
                )
            finally:
                task = asyncio.current_task()
                if task:
                    await self.unregister_agent_task(run_id, task)

        task = asyncio.create_task(runner())
        await self.register_agent_task(run_id, task)

    async def delete_checkpoints(self, thread_ids: list[str]) -> None:
        if not thread_ids or self.config.is_demo:
            return
        delete_thread = getattr(self.checkpointer, "adelete_thread", None)
        if not delete_thread:
            raise RuntimeError("CHECKPOINT_DELETE_UNAVAILABLE")
        for thread_id in thread_ids:
            await delete_thread(thread_id)


def _paper_read(paper: PaperRecord) -> PaperRead:
    return PaperRead.model_validate(paper)


def _user_read(user: UserRecord) -> UserRead:
    return UserRead.model_validate(user)


def _user_preferences_read(user: UserRecord) -> UserPreferencesRead:
    preferences = UserPreferences.model_validate(user.preferences or {})
    return UserPreferencesRead(display_name=user.display_name, **preferences.model_dump())


def _discovery_response(
    batch: Any,
    items: list[Any],
    *,
    restored: bool,
) -> DiscoveryRecommendationResponse:
    return DiscoveryRecommendationResponse(
        items=[
            DiscoveryRecommendation(
                item_id=item.id,
                arxiv_id=item.arxiv_id,
                title=item.title,
                authors=list(item.authors or []),
                abstract=item.abstract,
                published=item.published,
                pdf_url=item.pdf_url,
                journal_ref=item.journal_ref,
                matched_paper_title=item.matched_paper_title,
                matched_terms=list(item.matched_terms or []),
                match_type=item.match_type,
                feedback=item.feedback,
                opened=item.opened_at is not None,
                imported=item.imported_at is not None,
            )
            for item in items
        ],
        batch_id=batch.id,
        batch=batch.batch_number,
        basis_paper_count=batch.basis_paper_count,
        seed_paper_title=batch.seed_paper_title,
        profile_terms=list(batch.profile_terms or []),
        strategy=batch.strategy,
        restored=restored,
        feedback_applied=batch.feedback_applied,
        generated_at=batch.created_at,
    )


def _collection_tree(records: list[Any], memberships: dict[str, list[str]]) -> list[CollectionRead]:
    """把用户集合构造成树，并对后代论文去重计数。"""

    owned_ids = {item.id for item in records}
    children_by_parent: dict[str | None, list[Any]] = {}
    for item in records:
        parent_id = item.parent_id if item.parent_id in owned_ids else None
        children_by_parent.setdefault(parent_id, []).append(item)
    for children in children_by_parent.values():
        children.sort(key=lambda item: (item.name.casefold(), item.id))

    def build(record: Any, ancestors: set[str]) -> tuple[CollectionRead, set[str]]:
        if record.id in ancestors:  # 数据库约束之外的防御，避免损坏数据导致无限递归。
            raise RuntimeError("集合层级存在循环")
        next_ancestors = ancestors | {record.id}
        child_nodes: list[CollectionRead] = []
        recursive_paper_ids = set(memberships.get(record.id, []))
        for child in children_by_parent.get(record.id, []):
            child_node, child_paper_ids = build(child, next_ancestors)
            child_nodes.append(child_node)
            recursive_paper_ids.update(child_paper_ids)
        node = CollectionRead.model_validate(record).model_copy(
            update={
                "paper_ids": memberships.get(record.id, []),
                "recursive_paper_count": len(recursive_paper_ids),
                "children": child_nodes,
            }
        )
        return node, recursive_paper_ids

    return [build(record, set())[0] for record in children_by_parent.get(None, [])]


def _citation_dicts(
    items: list[Any], evidence: Optional[list[Evidence]] = None
) -> list[dict[str, Any]]:
    evidence_by_chunk = {item.chunk_id: item for item in evidence or []}
    result: list[dict[str, Any]] = []
    for item in items:
        value = item.__dict__ if hasattr(item, "__dict__") else dict(item)
        citation = dict(value)
        source = evidence_by_chunk.get(str(citation.get("chunk_id", "")))
        if source:
            citation["paper_title"] = source.paper_title
            citation["excerpt"] = citation.get("excerpt") or source.text[:320]
            citation["viewer_url"] = (
                f"/api/v1/papers/{source.paper_id}/file#page={source.physical_page}"
            )
        result.append(citation)
    return result


def _agent_run_read(record: Any) -> AgentRunRead:
    summary = record.result_summary or {}
    error_code = getattr(record, "error_code", None)
    safe_errors = {
        "UNVERIFIED_ANSWER": "系统已自动修复一次，但回答仍未通过证据核验，请稍后重试",
        "CONTEXT_BUDGET_EXCEEDED": (
            "本轮上下文过长，系统压缩后仍超出模型容量，请新建会话或缩小问题范围"
        ),
        "EVIDENCE_SCOPE_VIOLATION": "检索证据超出当前会话范围，运行已安全停止",
        "AGENT_RUN_FAILED": "问答运行失败，请稍后重试",
        "AGENT_RUN_CANCELLED": "问答运行已取消",
        "MODEL_NOT_CONFIGURED": "尚未配置可用的回答模型",
        "MODEL_TIMEOUT": "回答模型响应超时，请稍后重试",
        "MODEL_RATE_LIMITED": "回答模型请求过于频繁，请稍后重试",
        "MODEL_UNREACHABLE": "暂时无法连接回答模型，请检查模型服务配置",
        "MODEL_AUTHENTICATION_FAILED": "回答模型鉴权失败，请检查 API 配置",
        "MODEL_PROVIDER_ERROR": "回答模型暂时不可用，请稍后重试",
        "MODEL_CIRCUIT_OPEN": "回答模型连续失败，正在短暂恢复，请稍后重试",
    }
    error_message = safe_errors.get(error_code, "问答运行失败，请稍后重试") if error_code else None
    raw_action = getattr(record, "pending_action", None)
    pending_action = None
    if isinstance(raw_action, dict):
        candidate_keys = {
            "arxiv_id",
            "title",
            "authors",
            "abstract",
            "published",
            "pdf_url",
            "journal_ref",
        }
        candidates = [
            {key: value for key, value in item.items() if key in candidate_keys}
            for item in raw_action.get("candidates", [])
            if isinstance(item, dict)
        ]
        pending_action = {
            key: raw_action[key]
            for key in (
                "action_id",
                "type",
                "risk_message",
                "allowed_decisions",
            )
            if key in raw_action
        }
        pending_action["candidates"] = candidates
    return AgentRunRead(
        run_id=record.id,
        session_id=record.session_id,
        status=record.status,
        cancel_requested=bool(getattr(record, "cancel_requested", False)),
        orchestration_version=str(getattr(record, "orchestration_version", "single_agent_v1")),
        scope_snapshot=dict(getattr(record, "scope_snapshot", {}) or {}),
        context_snapshot=dict(getattr(record, "context_snapshot", {}) or {}),
        pending_action=pending_action,
        answer=summary.get("answer", ""),
        citations=summary.get("citations", []),
        evidence_quality=summary.get("evidence_quality", {}),
        node_trace=summary.get("node_trace", []),
        model_attempts=summary.get("model_attempts", []),
        duration_ms=getattr(record, "duration_ms", None),
        error_code=error_code,
        error_message=error_message,
        error=error_message,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def _embed_memory_value(
    config: Settings, model_router: Any, value: str
) -> tuple[Optional[list[float]], Optional[str]]:
    contract = configured_embedding_contract(config, model_router)
    if contract is None:
        return None, None
    try:
        values = await embed_discovery_texts(config, model_router, [value])
    except Exception:
        return None, None
    vector = values[0] if values else None
    if not vector or not vector_matches_contract(vector, contract):
        return None, None
    return vector, contract.fingerprint


def create_app(
    config: Settings = settings,
    *,
    repository: Optional[MemoryRepository] = None,
    storage: Optional[ObjectStorage] = None,
    runtime_store: Optional[RuntimeStore] = None,
) -> FastAPI:
    config.validate_production()
    services = AppServices(config, repository, storage, runtime_store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await services.repository.ensure_admin(
            config.bootstrap_admin_email, config.bootstrap_admin_password
        )
        contract = configured_embedding_contract(config, services.model_router)
        await services.repository.mark_embedding_contract_stale(
            contract.fingerprint if contract else None
        )
        if config.is_demo:
            try:
                yield
            finally:
                await services.mcp_gateway.close()
                await services.runtime_store.close()
            return
        # LangGraph 使用独立的 PostgreSQL Checkpointer，业务表不存放隐藏推理内容。
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpoint_url = config.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        async with AsyncPostgresSaver.from_conn_string(checkpoint_url) as checkpointer:
            await checkpointer.setup()
            services.checkpointer = checkpointer
            services.agent_graph = services.build_agent_graph(checkpointer)
            try:
                yield
            finally:
                services.checkpointer = None
                await services.mcp_gateway.close()
                await services.runtime_store.close()

    app = FastAPI(
        title="PaperLeaf API",
        version="0.8.0",
        description="个人科研文献库、页级 RAG 与受控研究 Agent",
        lifespan=lifespan,
    )
    app.state.services = services
    app.mount("/metrics", make_asgi_app())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "X-CSRF-Token",
            "Range",
            "Idempotency-Key",
            "Last-Event-ID",
        ],
        expose_headers=["Accept-Ranges", "Content-Range", "Content-Length"],
    )

    def service(request: Request) -> AppServices:
        return request.app.state.services

    async def current_user(
        request: Request,
        session_token: Annotated[Optional[str], Cookie(alias=config.session_cookie)] = None,
    ) -> UserRecord:
        if not session_token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
        user = await service(request).repository.user_for_session(session_token)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话无效或已过期")
        if user.must_change_password and request.url.path not in {
            "/api/v1/auth/me",
            "/api/v1/auth/change-password",
            "/api/v1/auth/logout",
            "/api/v1/users/me/preferences",
        }:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={"code": "PASSWORD_CHANGE_REQUIRED", "message": "请先修改临时密码"},
            )
        return user

    async def csrf_protected(
        csrf_cookie: Annotated[Optional[str], Cookie(alias=config.csrf_cookie)] = None,
        csrf_header: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
    ) -> None:
        if not csrf_matches(csrf_cookie, csrf_header):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF 校验失败")

    async def admin_user(user: Annotated[UserRecord, Depends(current_user)]) -> UserRecord:
        if user.role != UserRole.admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
        return user

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": config.mode}

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        runtime_available = await services.runtime_store.ping()
        return {
            "status": "ready",
            "runtime_store": {
                "backend": services.runtime_store.backend,
                "status": "available" if runtime_available else "degraded",
            },
        }

    @app.post("/api/v1/auth/login", response_model=UserRead)
    async def login(payload: LoginRequest, response: Response) -> UserRead:
        user = await services.repository.authenticate(payload.email, payload.password)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码错误")
        session_token = new_session_token()
        csrf_token = new_csrf_token()
        await services.repository.create_session(user.id, session_token, config.session_ttl_seconds)
        cookie_options = {
            "secure": config.secure_cookies,
            "samesite": "lax",
            "max_age": config.session_ttl_seconds,
            "path": "/",
        }
        response.set_cookie(config.session_cookie, session_token, httponly=True, **cookie_options)
        response.set_cookie(config.csrf_cookie, csrf_token, httponly=False, **cookie_options)
        return _user_read(user)

    @app.get("/api/v1/auth/me", response_model=UserRead)
    async def me(user: Annotated[UserRecord, Depends(current_user)]) -> UserRead:
        return _user_read(user)

    @app.get("/api/v1/users/me/preferences", response_model=UserPreferencesRead)
    async def get_preferences(
        user: Annotated[UserRecord, Depends(current_user)],
    ) -> UserPreferencesRead:
        return _user_preferences_read(user)

    @app.patch("/api/v1/users/me/preferences", response_model=UserPreferencesRead)
    async def update_preferences(
        payload: UserPreferencesUpdate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> UserPreferencesRead:
        changes = payload.model_dump(exclude_unset=True)
        display_name_marker = object()
        display_name = changes.pop("display_name", display_name_marker)
        preferences = UserPreferences.model_validate(user.preferences or {}).model_dump()
        preferences.update(changes)
        update_values: dict[str, object] = {"preferences": preferences}
        if display_name is not display_name_marker:
            update_values["display_name"] = display_name
        updated = await services.repository.update_user(user.id, **update_values)
        if not updated:  # pragma: no cover - 当前用户已由会话保证存在
            raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
        return _user_preferences_read(updated)

    @app.get("/api/v1/memories", response_model=MemoryListRead)
    async def list_memories(
        user: Annotated[UserRecord, Depends(current_user)],
    ) -> MemoryListRead:
        records = await services.repository.list_memories(user.id)
        return MemoryListRead(
            items=[MemoryRead.model_validate(item) for item in records],
            total=len(records),
            active=sum(bool(item.enabled) for item in records),
        )

    @app.post("/api/v1/memories", response_model=MemoryRead, status_code=status.HTTP_201_CREATED)
    async def create_memory(
        payload: MemoryCreate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> MemoryRead:
        value = normalize_memory_value(payload.value)
        embedding, embedding_fingerprint = await _embed_memory_value(
            config, services.model_router, value
        )
        record = MemoryItemRecord(
            id=str(uuid.uuid4()),
            user_id=user.id,
            type=payload.type,
            value=value,
            normalized_hash=memory_hash(payload.type, value),
            confidence=1.0,
            source_kind="manual",
            source_excerpt="由用户在设置页手动创建",
            pinned=payload.pinned,
            embedding=embedding,
            embedding_fingerprint=embedding_fingerprint,
        )
        try:
            created = await services.repository.create_memory_item(record)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return MemoryRead.model_validate(created)

    @app.patch("/api/v1/memories/{memory_id}", response_model=MemoryRead)
    async def update_memory(
        memory_id: str,
        payload: MemoryUpdate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> MemoryRead:
        changes = payload.model_dump(exclude_unset=True)
        memory_type = changes.get("type")
        value = changes.get("value")
        if memory_type is not None and memory_type not in MEMORY_TYPES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "不支持的记忆类型")
        if value is not None:
            changes["value"] = normalize_memory_value(str(value))
        if value is not None or memory_type is not None:
            existing = next(
                (
                    item
                    for item in await services.repository.list_memories(user.id)
                    if item.id == memory_id
                ),
                None,
            )
            if not existing:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
            next_type = str(memory_type or existing.type)
            next_value = str(changes.get("value", existing.value))
            changes["normalized_hash"] = memory_hash(next_type, next_value)
            embedding, embedding_fingerprint = await _embed_memory_value(
                config, services.model_router, next_value
            )
            changes["embedding"] = embedding
            changes["embedding_fingerprint"] = embedding_fingerprint
        try:
            updated = await services.repository.update_owned_memory(memory_id, user.id, **changes)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not updated:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
        return MemoryRead.model_validate(updated)

    @app.delete("/api/v1/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_memory(
        memory_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> Response:
        if not await services.repository.delete_owned_memory(memory_id, user.id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/memories/clear", response_model=MemoryClearRead)
    async def clear_memories(
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> MemoryClearRead:
        return MemoryClearRead(deleted=await services.repository.clear_memories(user.id))

    @app.post(
        "/api/v1/auth/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        response_model=None,
    )
    async def logout(
        response: Response,
        request: Request,
        _: Annotated[None, Depends(csrf_protected)],
        session_token: Annotated[Optional[str], Cookie(alias=config.session_cookie)] = None,
    ) -> None:
        if session_token:
            await service(request).repository.delete_session(session_token)
        response.delete_cookie(config.session_cookie, path="/")
        response.delete_cookie(config.csrf_cookie, path="/")

    @app.post("/api/v1/auth/change-password", response_model=UserRead)
    async def change_password(
        payload: ChangePasswordRequest,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> UserRead:
        if not verify_password(user.password_hash, payload.current_password):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前密码错误")
        updated = await services.repository.set_password(user.id, payload.new_password)
        return _user_read(updated)

    @app.get("/api/v1/admin/users", response_model=list[UserRead])
    async def list_users(_: Annotated[UserRecord, Depends(admin_user)]) -> list[UserRead]:
        return [_user_read(user) for user in await services.repository.list_users()]

    @app.post("/api/v1/admin/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
    async def create_user(
        payload: UserCreate,
        _: Annotated[UserRecord, Depends(admin_user)],
        __: Annotated[None, Depends(csrf_protected)],
    ) -> UserRead:
        try:
            user = await services.repository.create_user(
                payload.email, payload.temporary_password, payload.role
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return _user_read(user)

    @app.patch("/api/v1/admin/users/{user_id}", response_model=UserRead)
    async def update_user(
        user_id: str,
        payload: UserUpdate,
        admin: Annotated[UserRecord, Depends(admin_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> UserRead:
        try:
            updated = await services.repository.update_managed_user(
                user_id,
                admin.id,
                **payload.model_dump(exclude_none=True),
            )
        except ManagedUserNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except (CurrentAdminProtectionError, LastAdminProtectionError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return _user_read(updated)

    @app.get("/api/v1/admin/jobs", response_model=list[JobRead])
    async def list_admin_jobs(
        _: Annotated[UserRecord, Depends(admin_user)],
    ) -> list[JobRead]:
        # 只返回作业元数据，不读取论文正文、文本块或聊天内容。
        return [JobRead.model_validate(job) for job in await services.repository.list_jobs()]

    @app.post("/api/v1/admin/jobs/{job_id}/retry", response_model=JobRead)
    async def retry_admin_job(
        job_id: str,
        _: Annotated[UserRecord, Depends(admin_user)],
        __: Annotated[None, Depends(csrf_protected)],
    ) -> JobRead:
        job = await services.repository.retry_job(job_id)
        if not job:
            raise HTTPException(status.HTTP_409_CONFLICT, "作业不存在或当前状态不可重试")
        return JobRead.model_validate(job)

    @app.get("/api/v1/admin/model-health")
    async def model_health(
        _: Annotated[UserRecord, Depends(admin_user)],
    ) -> dict[str, Any]:
        providers = services.model_router.health()
        return {
            "configured": bool(providers),
            "providers": providers,
            "policy": {
                "timeout_seconds": config.model_timeout_seconds,
                "attempts_per_provider": config.model_attempts_per_provider,
                "failure_threshold": config.model_circuit_failure_threshold,
                "cooldown_seconds": config.model_circuit_cooldown_seconds,
            },
        }

    @app.get("/api/v1/admin/observability")
    async def admin_observability(
        _: Annotated[UserRecord, Depends(admin_user)],
        window: str = "24h",
    ) -> dict[str, Any]:
        windows = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}
        if window not in windows:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "统计窗口仅支持 24h、7d 或 30d"
            )
        hours = windows[window]
        limit = 5000
        runs = await services.repository.list_agent_runs_for_observability(
            datetime.now(timezone.utc) - timedelta(hours=hours),
            limit=limit,
        )
        report = aggregate_rag_runs(
            runs,
            window_hours=hours,
            limit_reached=len(runs) >= limit,
        )
        runtime_available = await services.runtime_store.ping()
        runtime_stats = await services.runtime_store.stats()
        report["runtime_store"] = {
            **runtime_stats,
            "status": "available" if runtime_available else "degraded",
        }
        report["privacy"] = {
            "content_collected": False,
            "identifiers_collected": False,
        }
        return report

    @app.get("/api/v1/admin/mcp/servers")
    async def list_admin_mcp_servers(
        _: Annotated[UserRecord, Depends(admin_user)],
    ) -> dict[str, Any]:
        servers = await services.mcp_gateway.list_servers()
        payload = []
        for server in servers:
            tools = await services.repository.list_mcp_tool_snapshots(server.id)
            payload.append(
                {
                    "id": server.id,
                    "display_name": server.display_name,
                    "transport": server.transport,
                    "enabled": server.enabled,
                    "health_status": server.health_status,
                    "consecutive_failures": server.consecutive_failures,
                    "circuit_open_until": server.circuit_open_until,
                    "last_checked_at": server.last_checked_at,
                    "last_error_code": server.last_error_code,
                    "tool_count": len(tools),
                    "tools": [
                        {
                            "name": item.normalized_name,
                            "description": item.description,
                            "annotations": item.annotations,
                            "discovered_at": item.discovered_at,
                        }
                        for item in tools
                    ],
                }
            )
        return {"feature_enabled": config.mcp_enabled, "servers": payload}

    @app.get("/api/v1/admin/harness/metrics")
    async def admin_harness_metrics(
        _: Annotated[UserRecord, Depends(admin_user)],
        window: str = "24h",
    ) -> dict[str, Any]:
        windows = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}
        if window not in windows:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "统计窗口仅支持 24h、7d 或 30d"
            )
        hours = windows[window]
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        run_limit = 5000
        tool_limit = 10000
        contract = configured_embedding_contract(config, services.model_router)
        runs, calls, memory, servers, embedding_counts = await asyncio.gather(
            services.repository.list_agent_runs_for_observability(since, limit=run_limit),
            services.repository.list_agent_tool_calls_for_observability(since, limit=tool_limit),
            services.repository.memory_observability_counts(),
            services.mcp_gateway.list_servers(),
            services.repository.embedding_contract_counts(
                contract.fingerprint if contract else None
            ),
        )
        return aggregate_harness_metrics(
            runs,
            calls,
            memory,
            servers,
            embedding={
                "configured": contract is not None,
                "provider": contract.provider if contract else None,
                "model": contract.model if contract else None,
                "dimensions": contract.dimensions if contract else None,
                "revision": contract.revision if contract else None,
                **embedding_counts,
            },
            window_hours=hours,
            limit_reached=len(runs) >= run_limit or len(calls) >= tool_limit,
        )

    @app.patch("/api/v1/admin/mcp/servers/{server_id}")
    async def update_admin_mcp_server(
        server_id: str,
        payload: McpServerUpdate,
        _: Annotated[UserRecord, Depends(admin_user)],
        __: Annotated[None, Depends(csrf_protected)],
    ) -> dict[str, Any]:
        if server_id != "academic":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP 服务不存在")
        if payload.enabled and not config.mcp_enabled:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "服务端尚未启用 PAPERLEAF_MCP_ENABLED，修改配置后需重启服务",
            )
        updated = await services.mcp_gateway.set_enabled(payload.enabled)
        return {
            "id": updated.id,
            "enabled": updated.enabled,
            "health_status": updated.health_status,
        }

    @app.post("/api/v1/admin/mcp/servers/{server_id}/test")
    async def test_admin_mcp_server(
        server_id: str,
        _: Annotated[UserRecord, Depends(admin_user)],
        __: Annotated[None, Depends(csrf_protected)],
    ) -> dict[str, Any]:
        if server_id != "academic":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP 服务不存在")
        try:
            return await services.mcp_gateway.test()
        except McpGatewayError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"学术 MCP 检测失败：{exc.code}",
            ) from exc

    @app.post("/api/v1/admin/mcp/servers/{server_id}/refresh")
    async def refresh_admin_mcp_server(
        server_id: str,
        _: Annotated[UserRecord, Depends(admin_user)],
        __: Annotated[None, Depends(csrf_protected)],
    ) -> dict[str, Any]:
        if server_id != "academic":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP 服务不存在")
        try:
            tools = await services.mcp_gateway.refresh()
        except McpGatewayError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"MCP 工具刷新失败：{exc.code}",
            ) from exc
        return {"server_id": server_id, "tool_count": len(tools)}

    @app.get(
        "/api/v1/admin/discovery-metrics",
        response_model=DiscoveryMetricsResponse,
    )
    async def admin_discovery_metrics(
        _: Annotated[UserRecord, Depends(admin_user)],
        window: str = "30d",
    ) -> DiscoveryMetricsResponse:
        windows = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}
        if window not in windows:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "统计窗口仅支持 24h、7d 或 30d"
            )
        hours = windows[window]
        metrics = await services.repository.discovery_metrics(
            datetime.now(timezone.utc) - timedelta(hours=hours)
        )
        return DiscoveryMetricsResponse(
            window_hours=hours,
            generated_at=datetime.now(timezone.utc),
            **metrics,
        )

    @app.get("/api/v1/collections", response_model=list[CollectionRead])
    async def list_collections(
        user: Annotated[UserRecord, Depends(current_user)],
    ) -> list[CollectionRead]:
        records = await services.repository.list_collections(user.id)
        memberships = await services.repository.list_collection_memberships(user.id)
        return _collection_tree(records, memberships)

    @app.post(
        "/api/v1/collections", response_model=CollectionRead, status_code=status.HTTP_201_CREATED
    )
    async def create_collection(
        payload: CollectionCreate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> CollectionRead:
        try:
            record = await services.repository.create_collection(
                user.id, payload.name, payload.description, payload.parent_id
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return CollectionRead.model_validate(record)

    @app.patch("/api/v1/collections/{collection_id}", response_model=CollectionRead)
    async def update_collection(
        collection_id: str,
        payload: CollectionUpdate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> CollectionRead:
        try:
            record = await services.repository.update_collection(
                collection_id, user.id, **payload.model_dump(exclude_unset=True)
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not record:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "集合不存在")
        return CollectionRead.model_validate(record)

    @app.delete("/api/v1/collections/{collection_id}")
    async def delete_collection(
        collection_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> dict[str, str]:
        try:
            deleted = await services.repository.delete_collection(collection_id, user.id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "集合不存在")
        return {"status": "deleted"}

    @app.post("/api/v1/collections/{collection_id}/papers/{paper_id}")
    async def add_paper_to_collection(
        collection_id: str,
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> dict[str, bool]:
        assigned = await services.repository.set_paper_collection(
            collection_id, paper_id, user.id, True
        )
        if not assigned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "集合或文献不存在")
        return {"assigned": True}

    @app.delete("/api/v1/collections/{collection_id}/papers/{paper_id}")
    async def remove_paper_from_collection(
        collection_id: str,
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> dict[str, bool]:
        assigned = await services.repository.set_paper_collection(
            collection_id, paper_id, user.id, False
        )
        if not assigned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "集合或文献不存在")
        return {"assigned": False}

    @app.get("/api/v1/papers", response_model=list[PaperRead])
    async def list_papers(
        user: Annotated[UserRecord, Depends(current_user)],
        collection_id: Optional[str] = None,
        unfiled: bool = False,
    ) -> list[PaperRead]:
        if collection_id is not None and unfiled:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "collection_id 与 unfiled 不能同时使用",
            )
        records = await services.repository.list_papers(
            user.id,
            collection_id=collection_id,
            unfiled=unfiled,
        )
        return [_paper_read(item) for item in records]

    @app.post("/api/v1/papers/bulk", response_model=PaperBulkActionResponse)
    async def bulk_paper_action(
        payload: PaperBulkActionRequest,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> PaperBulkActionResponse:
        paper_ids = list(dict.fromkeys(payload.paper_ids))
        for paper_id in paper_ids:
            if not await services.repository.get_owned_paper(paper_id, user.id):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不存在")

        if payload.action in {"archive", "unarchive"}:
            affected_ids = await services.repository.set_papers_archived(
                paper_ids, user.id, payload.action == "archive"
            )
            if affected_ids is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不存在")
        elif payload.action == "reindex":
            affected_ids = []
            for paper_id in paper_ids:
                if await services.repository.requeue_owned_paper(paper_id, user.id):
                    affected_ids.append(paper_id)
            if not affected_ids:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "所选文献正在处理或当前状态不能重新识别并索引",
                )
        else:
            if not payload.target_id:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "整理操作缺少目标")
            assigned = payload.action.startswith("add_")
            for paper_id in paper_ids:
                ok = await services.repository.set_paper_collection(
                    payload.target_id, paper_id, user.id, assigned
                )
                if not ok:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "集合或文献不存在")
            affected_ids = paper_ids

        return PaperBulkActionResponse(
            action=payload.action,
            affected=len(affected_ids),
            paper_ids=affected_ids,
        )

    @app.post("/api/v1/papers", response_model=PaperRead, status_code=status.HTTP_201_CREATED)
    async def upload_paper(
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
        file: Annotated[UploadFile, File()],
        title: Annotated[Optional[str], Form()] = None,
        doi: Annotated[Optional[str], Form()] = None,
    ) -> PaperRead:
        content = await file.read(config.max_pdf_bytes + 1)
        filename = Path(file.filename or "paper.pdf").name
        try:
            validate_pdf(content, filename, config.max_pdf_bytes)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        sha256 = hashlib.sha256(content).hexdigest()
        paper_id = str(uuid.uuid4())
        storage_key = f"{user.id}/{paper_id}/{sha256}.pdf"
        await services.storage.put(storage_key, content, "application/pdf")
        page_count: Optional[int] = None
        try:
            import fitz

            with fitz.open(stream=content, filetype="pdf") as document:
                page_count = document.page_count
                if page_count > config.max_pdf_pages:
                    await services.storage.delete(storage_key)
                    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "PDF 超过页数限制")
        except ImportError:
            pass
        except HTTPException:
            raise
        except Exception as exc:
            await services.storage.delete(storage_key)
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "PDF 已损坏或无法解析"
            ) from exc
        record = PaperRecord(
            id=paper_id,
            owner_id=user.id,
            title=(title or Path(filename).stem).strip(),
            authors=[],
            year=None,
            abstract=None,
            doi=doi,
            arxiv_id=None,
            filename=filename,
            storage_key=storage_key,
            mime_type="application/pdf",
            size_bytes=len(content),
            sha256=sha256,
            page_count=page_count,
            status=PaperStatus.queued,
        )
        try:
            await services.repository.create_paper(record)
        except ValueError as exc:
            await services.storage.delete(storage_key)
            duplicate_id = str(exc).partition(":")[2]
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"message": "文献已存在", "paper_id": duplicate_id},
            ) from exc
        return _paper_read(record)

    async def owned_paper(paper_id: str, user: UserRecord) -> PaperRecord:
        paper = await services.repository.get_owned_paper(paper_id, user.id)
        if not paper:
            # 对无权资源与不存在资源返回相同结果，避免枚举 ID。
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不存在")
        return paper

    def translation_read(translation: Any) -> PaperTranslationRead:
        return PaperTranslationRead.model_validate(translation)

    @app.get("/api/v1/papers/{paper_id}", response_model=PaperRead)
    async def get_paper(
        paper_id: str, user: Annotated[UserRecord, Depends(current_user)]
    ) -> PaperRead:
        return _paper_read(await owned_paper(paper_id, user))

    @app.post("/api/v1/papers/{paper_id}/opened", response_model=PaperRead)
    async def record_paper_opened(
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> PaperRead:
        paper = await services.repository.touch_paper_opened(paper_id, user.id)
        if not paper:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不存在")
        return _paper_read(paper)

    @app.post(
        "/api/v1/papers/{paper_id}/translations",
        response_model=PaperTranslationRead,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_paper_translation(
        paper_id: str,
        payload: TranslationCreate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> PaperTranslationRead:
        try:
            translation = await services.repository.create_or_resume_translation(
                paper_id,
                user.id,
                payload.target_language,
                payload.priority_page,
                model_available=services.model_router.has_provider("translation"),
                refresh=payload.refresh,
            )
        except TranslationSourceUnavailableError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        if not translation:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不存在")
        return translation_read(translation)

    @app.get(
        "/api/v1/papers/{paper_id}/translations/{translation_id}",
        response_model=PaperTranslationRead,
    )
    async def get_paper_translation(
        paper_id: str,
        translation_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        response: Response,
    ) -> PaperTranslationRead:
        translation = await services.repository.get_owned_translation(
            paper_id, translation_id, user.id
        )
        if not translation:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "全文翻译不存在")
        response.headers["Cache-Control"] = "private, no-store"
        return translation_read(translation)

    @app.get(
        "/api/v1/papers/{paper_id}/translations/{translation_id}/pages/{physical_page}",
        response_model=TranslationPageRead,
    )
    async def get_paper_translation_page(
        paper_id: str,
        translation_id: str,
        physical_page: int,
        user: Annotated[UserRecord, Depends(current_user)],
        response: Response,
    ) -> TranslationPageRead:
        page = await services.repository.get_owned_translation_page(
            paper_id, translation_id, physical_page, user.id
        )
        if not page:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "页译文不存在")
        response.headers["Cache-Control"] = "private, no-store"
        return TranslationPageRead.model_validate(page)

    @app.post(
        "/api/v1/papers/{paper_id}/translations/{translation_id}/cancel",
        response_model=PaperTranslationRead,
    )
    async def cancel_paper_translation(
        paper_id: str,
        translation_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> PaperTranslationRead:
        translation = await services.repository.cancel_owned_translation(
            paper_id, translation_id, user.id
        )
        if not translation:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "全文翻译不存在")
        return translation_read(translation)

    @app.patch("/api/v1/papers/{paper_id}", response_model=PaperRead)
    async def update_paper(
        paper_id: str,
        payload: PaperUpdate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> PaperRead:
        paper = await services.repository.update_owned_paper(
            paper_id, user.id, **payload.model_dump(exclude_none=True)
        )
        if not paper:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不存在")
        return _paper_read(paper)

    @app.delete("/api/v1/papers/{paper_id}", status_code=status.HTTP_202_ACCEPTED)
    async def delete_paper(
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> dict[str, str]:
        paper = await services.repository.delete_owned_paper(paper_id, user.id)
        if not paper:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不存在")
        return {"id": paper.id, "status": PaperStatus.deleting.value}

    @app.get("/api/v1/papers/{paper_id}/file")
    async def read_paper_file(
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        range_header: Annotated[Optional[str], Header(alias="Range")] = None,
    ) -> Response:
        paper = await owned_paper(paper_id, user)
        total = await services.storage.size(paper.storage_key)
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": "inline",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }

        try:
            byte_range = parse_byte_range(range_header, total)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                str(exc),
                headers={**headers, "Content-Range": f"bytes */{total}"},
            ) from exc
        if byte_range:
            body = await services.storage.read(paper.storage_key, byte_range.start, byte_range.end)
            headers.update(
                {
                    "Content-Range": byte_range.content_range,
                    "Content-Length": str(byte_range.length),
                }
            )
            return Response(body, status_code=206, media_type="application/pdf", headers=headers)
        body = await services.storage.read(paper.storage_key)
        headers["Content-Length"] = str(total)
        return Response(body, media_type="application/pdf", headers=headers)

    @app.post("/api/v1/papers/{paper_id}/retry", response_model=PaperRead)
    async def retry_paper(
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> PaperRead:
        await owned_paper(paper_id, user)
        updated = await services.repository.requeue_owned_paper(paper_id, user.id)
        if not updated:
            raise HTTPException(status.HTTP_409_CONFLICT, "文献正在处理或当前状态不能重新处理")
        return _paper_read(updated)

    @app.get("/api/v1/discover/arxiv/search", response_model=list[ArxivSearchResponse])
    async def discover_arxiv(
        q: str,
        user: Annotated[UserRecord, Depends(current_user)],
        limit: int = 10,
    ) -> list[ArxivSearchResponse]:
        if not q.strip() or len(q) > 500 or not 1 <= limit <= 20:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "检索参数无效")
        try:
            results = await search_arxiv(q.strip(), limit)
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "arXiv 暂时不可用") from exc
        return [ArxivSearchResponse(**item.__dict__) for item in results]

    @app.get(
        "/api/v1/discover/recommendations",
        response_model=DiscoveryRecommendationResponse,
    )
    async def discover_recommendations(
        response: Response,
        user: Annotated[UserRecord, Depends(current_user)],
        limit: int = 6,
        refresh: bool = False,
    ) -> DiscoveryRecommendationResponse:
        response.headers["Cache-Control"] = "private, no-store"
        if not UserPreferences.model_validate(user.preferences or {}).arxiv_search_enabled:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "ARXIV_SEARCH_DISABLED",
                    "message": "请先在个人设置中允许联网发现，再生成相关论文推荐",
                },
            )
        if not 3 <= limit <= 10:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "推荐参数无效")

        papers = await services.repository.list_papers(user.id)
        if not papers:
            return DiscoveryRecommendationResponse(
                items=[],
                batch=0,
                basis_paper_count=0,
                strategy="empty_library",
            )
        current = await services.repository.get_latest_discovery_batch(user.id)
        if current is not None and not refresh:
            return _discovery_response(current[0], current[1], restored=True)
        batch = int(current[0].batch_number) + 1 if current is not None else 0
        recommendation_papers: list[Any] = list(papers)
        if not config.is_demo and papers:
            try:
                evidence_groups = await asyncio.gather(
                    *(
                        load_paper_evidence(
                            user.id,
                            paper.id,
                            limit=3,
                        )
                        for paper in papers
                    )
                )
                recommendation_papers = with_indexed_text(
                    papers,
                    {
                        paper.id: " ".join(item.text for item in evidence)
                        for paper, evidence in zip(papers, evidence_groups)
                    },
                )
            except Exception:
                # 索引读取异常不阻断发现页，仍可使用已有元数据做确定性降级。
                recommendation_papers = list(papers)
        profile = build_discovery_profile(recommendation_papers, batch)
        if not profile:  # pragma: no cover - papers 已保证至少有一篇带标题记录
            raise HTTPException(status.HTTP_409_CONFLICT, "文献库暂无可用于推荐的标题")
        try:
            candidates = await search_related_arxiv(
                list(profile.search_phrases),
                20,
                start=profile.search_start,
            )
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "arXiv 暂时不可用") from exc

        excluded_ids = await services.repository.list_discovery_seen_arxiv_ids(user.id)
        (
            positive_feedback,
            negative_feedback,
        ) = await services.repository.get_discovery_feedback_signals(user.id)
        ranked, strategy = await collect_recommendations(
            profile,
            candidates,
            config=config,
            model_router=services.model_router,
            excluded_arxiv_ids=excluded_ids,
            positive_feedback_texts=positive_feedback,
            negative_feedback_texts=negative_feedback,
            limit=limit,
        )
        batch_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)
        saved = await services.repository.create_discovery_batch(
            DiscoveryBatchRecord(
                id=batch_id,
                user_id=user.id,
                batch_number=batch,
                basis_paper_count=profile.basis_paper_count,
                seed_paper_title=str(getattr(profile.seed, "title", "")),
                profile_terms=list(profile.topics[:6]),
                strategy=strategy,
                feedback_applied=bool(positive_feedback or negative_feedback),
                created_at=created_at,
            ),
            [
                DiscoveryItemRecord(
                    id=str(uuid.uuid4()),
                    batch_id=batch_id,
                    user_id=user.id,
                    arxiv_id=item.paper.arxiv_id,
                    title=item.paper.title,
                    authors=list(item.paper.authors),
                    abstract=item.paper.abstract,
                    published=item.paper.published,
                    pdf_url=item.paper.pdf_url,
                    journal_ref=item.paper.journal_ref,
                    matched_paper_title=item.matched_paper_title,
                    matched_terms=list(item.matched_terms),
                    match_type=item.match_type,
                    score=item.score,
                    rank=index,
                    created_at=created_at,
                )
                for index, item in enumerate(ranked, start=1)
            ],
        )
        return _discovery_response(saved[0], saved[1], restored=False)

    @app.post(
        "/api/v1/discover/recommendations/items/{item_id}/feedback",
        response_model=DiscoveryFeedbackResponse,
    )
    async def record_discovery_feedback(
        item_id: str,
        payload: DiscoveryFeedbackRequest,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> DiscoveryFeedbackResponse:
        item = await services.repository.record_discovery_item_action(
            item_id, user.id, payload.action
        )
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "推荐论文不存在")
        return DiscoveryFeedbackResponse(
            item_id=item.id,
            feedback=item.feedback,
            opened=item.opened_at is not None,
            imported=item.imported_at is not None,
        )

    @app.post(
        "/api/v1/discover/arxiv/import",
        response_model=PaperRead,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_arxiv(
        payload: ArxivImportRequest,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> PaperRead:
        try:
            created = await import_arxiv_paper(
                payload.arxiv_id,
                user.id,
                config=config,
                repository=services.repository,
                storage=services.storage,
            )
        except ValueError as exc:
            if "已存在" in str(exc) or "重复" in str(exc):
                raise HTTPException(status.HTTP_409_CONFLICT, "文献已导入") from exc
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "arXiv PDF 下载失败") from exc
        if payload.recommendation_item_id:
            await services.repository.record_discovery_item_action(
                payload.recommendation_item_id,
                user.id,
                "imported",
                arxiv_id=payload.arxiv_id,
            )
        return _paper_read(created)

    @app.post(
        "/api/v1/papers/{paper_id}/summary",
        response_model=SummaryResponse,
        responses={202: {"description": "概括后台任务已创建或仍在运行"}},
    )
    async def summarize_paper(
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
        response: Response,
        refresh: bool = False,
    ) -> SummaryResponse:
        await owned_paper(paper_id, user)
        if config.is_demo:
            raise HTTPException(status.HTTP_409_CONFLICT, "演示模式不处理真实 PDF")
        evidence = await load_paper_evidence(
            user.id,
            paper_id,
            limit=config.max_pdf_pages,
            first_chunk_per_page=True,
        )
        if not evidence:
            raise HTTPException(status.HTTP_409_CONFLICT, "文献尚未完成解析")
        revision = await load_paper_source_revision(user.id, paper_id)
        cached = await services.repository.get_owned_paper_artifact(paper_id, user.id, "summary")
        cached_payload = dict(cached.structured_payload or {}) if cached else {}
        validated_cached, _ = validate_summary_payload(cached_payload, evidence)
        cached_is_current = bool(
            cached
            and cached.status == "ready"
            and cached.source_revision == revision
            and validated_cached is not None
        )
        active_job = await services.repository.get_active_paper_artifact_job(
            paper_id, user.id, "summary"
        )
        if active_job:
            response.status_code = status.HTTP_202_ACCEPTED
            payload = (
                validated_cached
                if cached_is_current
                else {
                    "sections": [],
                    "citations": [],
                    "mode": "model",
                }
            )
            return SummaryResponse(
                paper_id=paper_id,
                status="processing",
                stale=False,
                fallback_reason=None,
                sections=payload.get("sections", []),
                content=cached.markdown if cached_is_current and cached else "",
                citations=payload.get("citations", []),
                mode="model",
            )
        if not refresh and cached_is_current:
            return SummaryResponse(
                paper_id=paper_id,
                status="ready",
                stale=False,
                fallback_reason=None,
                sections=validated_cached.get("sections", []),
                content=cached.markdown if cached else "",
                citations=validated_cached.get("citations", []),
                mode="model",
            )
        if (
            not refresh
            and cached
            and cached.status == "failed"
            and cached.source_revision == revision
        ):
            return SummaryResponse(
                paper_id=paper_id,
                status="failed",
                stale=False,
                fallback_reason=cached.fallback_reason,
                sections=[],
                content="",
                citations=[],
                mode="model",
            )
        queued = await services.repository.enqueue_paper_artifact(
            paper_id,
            user.id,
            "summary",
            revision,
            preserve_existing=cached_is_current,
        )
        if not queued:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不存在")
        response.status_code = status.HTTP_202_ACCEPTED
        payload = (
            validated_cached
            if cached_is_current
            else {
                "sections": [],
                "citations": [],
                "mode": "model",
            }
        )
        return SummaryResponse(
            paper_id=paper_id,
            status="processing",
            stale=False,
            fallback_reason=None,
            sections=payload.get("sections", []),
            content=cached.markdown if cached_is_current and cached else "",
            citations=payload.get("citations", []),
            mode="model",
        )

    @app.post(
        "/api/v1/papers/{paper_id}/structure-graph",
        response_model=StructureGraphResponse,
        responses={202: {"description": "研究脑图后台任务已创建或仍在运行"}},
    )
    async def build_paper_structure_graph(
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
        response: Response,
        refresh: bool = False,
    ) -> StructureGraphResponse:
        await owned_paper(paper_id, user)
        if config.is_demo:
            raise HTTPException(status.HTTP_409_CONFLICT, "演示模式不处理真实 PDF")
        evidence = await load_paper_evidence(
            user.id,
            paper_id,
            limit=config.max_pdf_pages,
            first_chunk_per_page=True,
        )
        if not evidence:
            raise HTTPException(status.HTTP_409_CONFLICT, "文献尚未完成解析")
        revision = await load_paper_source_revision(user.id, paper_id)
        cached = await services.repository.get_owned_paper_artifact(paper_id, user.id, "structure")
        cached_payload = dict(cached.structured_payload or {}) if cached else {}
        validated_cached, _ = validate_structure_payload(cached_payload, evidence)
        cached_is_current = bool(
            cached
            and cached.status == "ready"
            and cached.source_revision == revision
            and validated_cached is not None
        )
        active_job = await services.repository.get_active_paper_artifact_job(
            paper_id, user.id, "structure"
        )
        if active_job:
            response.status_code = status.HTTP_202_ACCEPTED
            payload = (
                validated_cached
                if cached_is_current
                else {
                    "nodes": [],
                    "edges": [],
                    "mermaid": "",
                }
            )
            return StructureGraphResponse(
                paper_id=paper_id,
                status="processing",
                stale=False,
                fallback_reason=None,
                nodes=payload.get("nodes", []),
                edges=payload.get("edges", []),
                mermaid=payload.get("mermaid", ""),
                evidence_excerpt="",
            )
        if not refresh and cached_is_current:
            return StructureGraphResponse(
                paper_id=paper_id,
                status="ready",
                stale=False,
                fallback_reason=None,
                nodes=validated_cached.get("nodes", []),
                edges=validated_cached.get("edges", []),
                mermaid=validated_cached.get("mermaid", ""),
                evidence_excerpt="",
            )
        if (
            not refresh
            and cached
            and cached.status == "failed"
            and cached.source_revision == revision
        ):
            return StructureGraphResponse(
                paper_id=paper_id,
                status="failed",
                stale=False,
                fallback_reason=cached.fallback_reason,
                nodes=[],
                edges=[],
                mermaid="",
                evidence_excerpt="",
            )
        queued = await services.repository.enqueue_paper_artifact(
            paper_id,
            user.id,
            "structure",
            revision,
            preserve_existing=cached_is_current,
        )
        if not queued:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文献不存在")
        response.status_code = status.HTTP_202_ACCEPTED
        payload = (
            validated_cached
            if cached_is_current
            else {
                "nodes": [],
                "edges": [],
                "mermaid": "",
            }
        )
        return StructureGraphResponse(
            paper_id=paper_id,
            status="processing",
            stale=False,
            fallback_reason=None,
            nodes=payload.get("nodes", []),
            edges=payload.get("edges", []),
            mermaid=payload.get("mermaid", ""),
            evidence_excerpt="",
        )

    @app.get("/api/v1/chat/sessions", response_model=list[ChatSessionRead])
    async def list_chat_sessions(
        user: Annotated[UserRecord, Depends(current_user)],
    ) -> list[ChatSessionRead]:
        return [
            ChatSessionRead.model_validate(item)
            for item in await services.repository.list_chat_sessions(user.id)
        ]

    @app.post(
        "/api/v1/chat/sessions",
        response_model=ChatSessionRead,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_chat_session(
        payload: ChatSessionCreate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> ChatSessionRead:
        if payload.type == "paper":
            if not payload.paper_id or payload.collection_id:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "单篇会话必须且只能绑定 paper_id",
                )
            await owned_paper(payload.paper_id, user)
        elif payload.type == "collection":
            if not payload.collection_id or payload.paper_id:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "集合会话必须且只能绑定 collection_id",
                )
            if (
                await services.repository.resolve_collection_paper_ids(
                    payload.collection_id, user.id
                )
                is None
            ):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "集合不存在")
        elif payload.paper_id or payload.collection_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "全库会话不能绑定论文或集合",
            )
        record = await services.repository.create_chat_session(
            user.id,
            payload.title,
            payload.type,
            payload.paper_id,
            payload.collection_id,
        )
        return ChatSessionRead.model_validate(record)

    @app.get("/api/v1/chat/sessions/{session_id}", response_model=ChatSessionRead)
    async def get_chat_session(
        session_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
    ) -> ChatSessionRead:
        record = await services.repository.get_owned_chat_session(session_id, user.id)
        if not record:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        return ChatSessionRead.model_validate(record)

    @app.patch("/api/v1/chat/sessions/{session_id}", response_model=ChatSessionRead)
    async def update_chat_session(
        session_id: str,
        payload: ChatSessionUpdate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> ChatSessionRead:
        record = await services.repository.update_owned_chat_session(
            session_id, user.id, payload.title
        )
        if not record:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        return ChatSessionRead.model_validate(record)

    @app.delete("/api/v1/chat/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_chat_session(
        session_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> Response:
        chat_session = await services.repository.get_owned_chat_session(session_id, user.id)
        if not chat_session:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        if chat_session.current_run_status in {"pending", "running", "interrupted"}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "会话仍有运行中或等待确认的任务，请先取消",
            )
        thread_ids = await services.repository.list_session_thread_ids(session_id, user.id)
        if thread_ids is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        try:
            await services.delete_checkpoints(thread_ids)
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "会话 Checkpoint 清理失败，请稍后重试",
            ) from exc
        try:
            deleted = await services.repository.delete_owned_chat_session(session_id, user.id)
        except ChatActiveRunError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/api/v1/chat/sessions/{session_id}/messages",
        response_model=list[ChatMessageRead],
    )
    async def list_chat_messages(
        session_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
    ) -> list[ChatMessageRead]:
        records = await services.repository.list_chat_messages(session_id, user.id)
        if records is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        return [ChatMessageRead.model_validate(item) for item in records]

    @app.post(
        "/api/v1/chat/sessions/{session_id}/messages",
        response_model=ChatSubmissionRead,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_chat_message(
        session_id: str,
        payload: ChatMessageRequest,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
        client_message_id: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=100)
        ],
    ) -> ChatSubmissionRead:
        chat_session = await services.repository.get_owned_chat_session(session_id, user.id)
        if not chat_session:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        context_paper = None
        if chat_session.type == "paper":
            paper = await services.repository.get_owned_paper(chat_session.paper_id or "", user.id)
            if not paper or paper.status not in {PaperStatus.ready, PaperStatus.partial}:
                raise HTTPException(status.HTTP_409_CONFLICT, "绑定论文尚未完成索引")
            paper_ids = [paper.id]
            context_paper = paper
        elif chat_session.type == "collection":
            resolved = await services.repository.resolve_collection_paper_ids(
                chat_session.collection_id or "", user.id, ready_only=True
            )
            if resolved is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "绑定集合不存在")
            if not resolved:
                raise HTTPException(status.HTTP_409_CONFLICT, "集合暂无可提问文献")
            paper_ids = resolved
        else:
            paper_ids = [
                item.id
                for item in await services.repository.list_papers(user.id)
                if item.status in {PaperStatus.ready, PaperStatus.partial}
            ]
        client_context = (
            payload.client_context.model_dump(exclude_none=True)
            if payload.client_context is not None
            else {}
        )
        context_paper_id = str(client_context.get("paper_id", "")).strip()
        if context_paper_id:
            if context_paper_id not in paper_ids:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "当前会话不能访问该上下文论文")
            context_paper = await services.repository.get_owned_paper(context_paper_id, user.id)
            if not context_paper:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "上下文论文不存在")
        elif chat_session.type == "paper" and context_paper is not None:
            client_context["paper_id"] = context_paper.id
        context_collection_id = str(client_context.get("collection_id", "")).strip()
        if context_collection_id and context_collection_id != (chat_session.collection_id or ""):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前会话不能访问该上下文集合")
        physical_page = client_context.get("physical_page")
        selected_text = str(client_context.get("selected_text", "")).strip()
        if physical_page is not None and context_paper is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "页码上下文必须指定论文")
        if context_paper is not None:
            client_context["paper_title"] = context_paper.title
            if physical_page is not None and context_paper.page_count:
                if int(physical_page) > int(context_paper.page_count):
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY, "上下文页码超出论文范围"
                    )
        if selected_text:
            if context_paper is None or physical_page is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "选中文字必须同时指定论文和物理页",
                )
            normalized_selection = " ".join(selected_text.split())
            supplied_hash = str(client_context.get("selected_text_hash", "")).lower()
            page_text = await services.repository.get_owned_paper_page_text(
                context_paper.id, int(physical_page), user.id
            )
            selection_match = match_selection_to_page(normalized_selection, page_text or "")
            legacy_hash = hashlib.sha256(normalized_selection.encode("utf-8")).hexdigest()
            if supplied_hash and supplied_hash not in {
                legacy_hash,
                selection_match.canonical_hash,
            }:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "选中文字校验失败")
            if not selection_match.accepted:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "无法在当前 PDF 页核对这段文字，请缩小选择范围后重试",
                )
            client_context["selected_text"] = selection_match.canonical_text
            client_context["selected_text_hash"] = selection_match.canonical_hash
            client_context["selection_match"] = {
                "mode": selection_match.mode,
                "score": selection_match.score,
            }
        scope_snapshot = {
            "type": chat_session.type,
            "paper_id": chat_session.paper_id,
            "collection_id": chat_session.collection_id,
            "paper_ids": paper_ids,
            "web_enabled": bool(
                payload.web_enabled
                and UserPreferences.model_validate(
                    getattr(user, "preferences", {}) or {}
                ).arxiv_search_enabled
            ),
            "client_context": client_context,
            "harness": {
                "context_engine_enabled": config.context_engine_enabled,
                "memory_enabled": config.memory_enabled,
                "skills_enabled": config.skills_enabled,
                "function_tools_enabled": config.function_tools_enabled,
                "mcp_enabled": config.mcp_enabled,
                "multi_agent_enabled": config.multi_agent_enabled,
                "multi_agent_max_branches": config.multi_agent_max_branches,
                "multi_agent_branch_timeout_seconds": (config.multi_agent_branch_timeout_seconds),
                "multi_agent_total_timeout_seconds": (config.multi_agent_total_timeout_seconds),
                "multi_agent_token_budget": config.multi_agent_token_budget,
            },
        }
        frozen_intent = classify_intent(
            payload.content,
            scope=chat_session.type,
            selected_paper_count=len(paper_ids),
            web_enabled=bool(scope_snapshot["web_enabled"]),
        )
        frozen_skill = services.skill_registry.route(
            payload.content,
            intent=frozen_intent,
            scope=chat_session.type,
            web_enabled=bool(scope_snapshot["web_enabled"]),
        ).manifest.name
        scope_snapshot["orchestration_version"] = (
            "compare_map_reduce_v2"
            if config.multi_agent_enabled
            and config.skills_enabled
            and frozen_skill == "compare_papers"
            and chat_session.type in {"collection", "library"}
            and 3 <= len(paper_ids) <= 10
            else "single_agent_v1"
        )
        rate_limit = await services.runtime_store.acquire_rate_limit(
            "agent-submit",
            user.id,
            limit=config.agent_rate_limit_requests,
            window_seconds=config.agent_rate_limit_window_seconds,
            idempotency_key=f"{session_id}:{client_message_id}",
        )
        if not rate_limit.allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "AGENT_RATE_LIMITED",
                    "message": "提问过于频繁，请稍后再试",
                    "retry_after_seconds": rate_limit.retry_after_seconds,
                },
                headers={"Retry-After": str(rate_limit.retry_after_seconds)},
            )
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "session_id": session_id,
                    "content": payload.content,
                    "scope_snapshot": scope_snapshot,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            submission = await services.repository.submit_chat_message(
                session_id,
                user.id,
                payload.content,
                client_message_id,
                request_hash,
                scope_snapshot,
            )
        except ChatIdempotencyConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except ChatActiveRunError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not submission:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        if config.is_demo and not submission.replayed:
            await services.launch_local_agent_run(submission.run.id)
        return ChatSubmissionRead(
            session_id=session_id,
            message_id=submission.message.id,
            run_id=submission.run.id,
            status="pending",
            replayed=submission.replayed,
        )

    @app.get("/api/v1/agent/runs/{run_id}", response_model=AgentRunRead)
    async def get_agent_run(
        run_id: str, user: Annotated[UserRecord, Depends(current_user)]
    ) -> AgentRunRead:
        run = await services.repository.get_owned_agent_run(run_id, user.id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "运行不存在")
        return _agent_run_read(run)

    @app.get("/api/v1/agent/runs/{run_id}/events")
    async def get_agent_run_events(
        run_id: str,
        request: Request,
        user: Annotated[UserRecord, Depends(current_user)],
        last_event_id: Annotated[Optional[str], Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        run = await services.repository.get_owned_agent_run(run_id, user.id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "运行不存在")
        try:
            after_sequence = int(last_event_id or "0")
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "Last-Event-ID 必须是事件序号"
            ) from exc
        if after_sequence < 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Last-Event-ID 不能为负数")

        async def persisted_events() -> AsyncIterator[str]:
            cursor = after_sequence
            last_heartbeat = time.monotonic()
            while True:
                records = await services.repository.list_owned_agent_run_events(
                    run_id, user.id, cursor
                )
                if records is None:
                    return
                for record in records:
                    cursor = record.sequence
                    body = AgentRunEventRead(
                        id=record.sequence,
                        sequence=record.sequence,
                        event=record.event,
                        run_id=record.run_id,
                        data=record.data or {},
                        created_at=record.created_at,
                    )
                    yield (
                        f"id: {record.sequence}\n"
                        f"event: {record.event}\n"
                        f"data: {body.model_dump_json()}\n\n"
                    )
                    last_heartbeat = time.monotonic()
                current = await services.repository.get_owned_agent_run(run_id, user.id)
                if not current or current.status in {
                    "interrupted",
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    return
                if await request.is_disconnected():
                    return
                if time.monotonic() - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = time.monotonic()
                await asyncio.sleep(0.25)

        return StreamingResponse(
            persisted_events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "private, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/agent/runs/{run_id}/resume", response_model=AgentRunRead)
    async def resume_agent_run(
        run_id: str,
        payload: AgentResumeRequest,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> AgentRunRead:
        try:
            run = await services.repository.resume_owned_agent_run(
                run_id, user.id, payload.action_id, payload.decision
            )
        except (ChatIdempotencyConflictError, ChatActiveRunError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "运行不存在")
        if config.is_demo and run.status == "pending":
            await services.launch_local_agent_run(run_id)
        return await get_agent_run(run_id, user)

    @app.post("/api/v1/agent/runs/{run_id}/cancel", response_model=AgentRunRead)
    async def cancel_agent_run(
        run_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> AgentRunRead:
        try:
            run = await services.repository.cancel_owned_agent_run(run_id, user.id)
        except ChatActiveRunError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "运行不存在")
        await services.cancel_agent_task(run_id)
        return _agent_run_read(run)

    return app


app = create_app()
