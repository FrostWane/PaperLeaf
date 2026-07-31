"""PaperLeaf FastAPI åº”ç”¨å…¥å£ã€‚"""

import asyncio
import hashlib
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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

from .agent.graph import (
    build_agent_graph,
    build_configured_answerer,
    build_configured_evidence_support_grader,
)
from .agent.tools import DemoLibrarySearch, SQLLibrarySearch
from .artifacts import load_paper_evidence, structure_graph, summarize_evidence
from .arxiv_service import fetch_arxiv_pdf, search_arxiv
from .config import Settings, settings
from .model_runtime import build_model_router, collect_model_attempts
from .models import PaperStatus, UserRole
from .rag.citations import Evidence
from .rag.retrieval_quality import EvidenceQualityPolicy
from .repository import MemoryRepository, PaperRecord, SQLAlchemyRepository, UserRecord
from .schemas import (
    AgentResumeRequest,
    AgentRunRead,
    ArtifactCitation,
    ArxivImportRequest,
    ArxivSearchResponse,
    ChangePasswordRequest,
    ChatMessageRequest,
    CollectionCreate,
    CollectionRead,
    CollectionUpdate,
    JobRead,
    LoginRequest,
    PaperBulkActionRequest,
    PaperBulkActionResponse,
    PaperRead,
    PaperUpdate,
    SSEEvent,
    StructureGraphResponse,
    SummaryResponse,
    TagCreate,
    TagRead,
    TagUpdate,
    UserCreate,
    UserRead,
    UserUpdate,
)
from .security import csrf_matches, new_csrf_token, new_session_token, verify_password
from .storage import ObjectStorage, create_storage, parse_byte_range, validate_pdf

_PUBLIC_AGENT_NODES = {
    "validate_request",
    "retrieve_library",
    "grade_evidence",
    "generate_answer",
    "validate_citations",
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
    ) -> None:
        self.config = config
        self.repository = repository or (
            MemoryRepository(config.session_secret)
            if config.is_demo
            else SQLAlchemyRepository(config.session_secret)
        )
        self.storage = storage or create_storage(config)
        self.model_router = build_model_router(config)
        self.retriever = (
            DemoLibrarySearch()
            if config.is_demo
            else SQLLibrarySearch(config, self.model_router)
        )
        self.agent_graph = self.build_agent_graph()
        self._agent_tasks: dict[str, asyncio.Task[Any]] = {}
        self._agent_tasks_lock = asyncio.Lock()

    def build_agent_graph(self, checkpointer: Any | None = None) -> Any:
        """ç”Ÿäº§é‡å»º Graph æ—¶ä¿æŒä¸Ž App ç›¸åŒçš„æ¨¡åž‹å’Œè´¨é‡ç­–ç•¥ã€‚"""

        return build_agent_graph(
            retriever=self.retriever,
            answerer=build_configured_answerer(self.config, self.model_router),
            checkpointer=checkpointer,
            quality_policy=EvidenceQualityPolicy(
                min_confidence=self.config.evidence_min_confidence,
                min_vector_score=self.config.evidence_min_vector_score,
                min_lexical_coverage=self.config.evidence_min_lexical_coverage,
            ),
            support_grader=build_configured_evidence_support_grader(
                self.config, self.model_router
            ),
        )

    async def register_agent_task(self, run_id: str, task: asyncio.Task[Any]) -> None:
        async with self._agent_tasks_lock:
            self._agent_tasks[run_id] = task

    async def unregister_agent_task(self, run_id: str, task: asyncio.Task[Any]) -> None:
        async with self._agent_tasks_lock:
            if self._agent_tasks.get(run_id) is task:
                self._agent_tasks.pop(run_id, None)

    async def cancel_agent_task(self, run_id: str) -> bool:
        async with self._agent_tasks_lock:
            task = self._agent_tasks.get(run_id)
            if not task or task.done():
                return False
            task.cancel()
            return True


def _paper_read(paper: PaperRecord) -> PaperRead:
    return PaperRead.model_validate(paper)


def _user_read(user: UserRecord) -> UserRead:
    return UserRead.model_validate(user)


def _citation_dicts(
    items: list[Any], evidence: list[Evidence] | None = None
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
    return AgentRunRead(
        run_id=record.id,
        session_id=record.session_id,
        status=record.status,
        answer=summary.get("answer", ""),
        citations=summary.get("citations", []),
        evidence_quality=summary.get("evidence_quality", {}),
        node_trace=summary.get("node_trace", []),
        model_attempts=summary.get("model_attempts", []),
        duration_ms=getattr(record, "duration_ms", None),
        error=record.error_code,
    )


def create_app(
    config: Settings = settings,
    *,
    repository: Optional[MemoryRepository] = None,
    storage: Optional[ObjectStorage] = None,
) -> FastAPI:
    config.validate_production()
    services = AppServices(config, repository, storage)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await services.repository.ensure_admin(
            config.bootstrap_admin_email, config.bootstrap_admin_password
        )
        if config.is_demo:
            yield
            return
        # LangGraph ä½¿ç”¨ç‹¬ç«‹çš„ PostgreSQL Checkpointerï¼Œä¸šåŠ¡è¡¨ä¸å­˜æ”¾éšè—æŽ¨ç†å†…å®¹ã€‚
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpoint_url = config.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        async with AsyncPostgresSaver.from_conn_string(checkpoint_url) as checkpointer:
            await checkpointer.setup()
            services.agent_graph = services.build_agent_graph(checkpointer)
            yield

    app = FastAPI(
        title="PaperLeaf API",
        version="0.5.0",
        description="ä¸ªäººç§‘ç ”æ–‡çŒ®åº“ã€é¡µçº§ RAG ä¸Žå—æŽ§ç ”ç©¶ Agent",
        lifespan=lifespan,
    )
    app.state.services = services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Range"],
        expose_headers=["Accept-Ranges", "Content-Range", "Content-Length"],
    )

    def service(request: Request) -> AppServices:
        return request.app.state.services

    async def current_user(
        request: Request,
        session_token: Annotated[Optional[str], Cookie(alias=config.session_cookie)] = None,
    ) -> UserRecord:
        if not session_token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "æœªç™»å½•")
        user = await service(request).repository.user_for_session(session_token)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ä¼šè¯æ— æ•ˆæˆ–å·²è¿‡æœŸ")
        if user.must_change_password and request.url.path not in {
            "/api/v1/auth/me",
            "/api/v1/auth/change-password",
            "/api/v1/auth/logout",
        }:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={"code": "PASSWORD_CHANGE_REQUIRED", "message": "è¯·å…ˆä¿®æ”¹ä¸´æ—¶å¯†ç "},
            )
        return user

    async def csrf_protected(
        csrf_cookie: Annotated[Optional[str], Cookie(alias=config.csrf_cookie)] = None,
        csrf_header: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
    ) -> None:
        if not csrf_matches(csrf_cookie, csrf_header):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF æ ¡éªŒå¤±è´¥")

    async def admin_user(user: Annotated[UserRecord, Depends(current_user)]) -> UserRecord:
        if user.role != UserRole.admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "éœ€è¦ç®¡ç†å‘˜æƒé™")
        return user

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": config.mode}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/api/v1/auth/login", response_model=UserRead)
    async def login(payload: LoginRequest, response: Response) -> UserRead:
        user = await services.repository.authenticate(payload.email, payload.password)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "é‚®ç®±æˆ–å¯†ç é”™è¯¯")
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
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "å½“å‰å¯†ç é”™è¯¯")
        await services.repository.set_password(user.id, payload.new_password)
        return _user_read(user)

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
        target = await services.repository.get_user(user_id)
        if not target:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "ç”¨æˆ·ä¸å­˜åœ¨")
        removes_admin = target.role == UserRole.admin and (
            payload.active is False or payload.role == UserRole.user
        )
        if removes_admin and await services.repository.count_active_admins() <= 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "ä¸èƒ½åœç”¨æˆ–é™çº§æœ€åŽä¸€åç®¡ç†å‘˜")
        if target.id == admin.id and payload.active is False:
            raise HTTPException(status.HTTP_409_CONFLICT, "ä¸èƒ½åœç”¨å½“å‰ç®¡ç†å‘˜")
        updated = await services.repository.update_user(
            user_id, **payload.model_dump(exclude_none=True)
        )
        return _user_read(updated)  # type: ignore[arg-type]

    @app.get("/api/v1/admin/jobs", response_model=list[JobRead])
    async def list_admin_jobs(
        _: Annotated[UserRecord, Depends(admin_user)],
    ) -> list[JobRead]:
        # åªè¿”å›žä½œä¸šå…ƒæ•°æ®ï¼Œä¸è¯»å–è®ºæ–‡æ­£æ–‡ã€æ–‡æœ¬å—æˆ–èŠå¤©å†…å®¹ã€‚
        return [JobRead.model_validate(job) for job in await services.repository.list_jobs()]

    @app.post("/api/v1/admin/jobs/{job_id}/retry", response_model=JobRead)
    async def retry_admin_job(
        job_id: str,
        _: Annotated[UserRecord, Depends(admin_user)],
        __: Annotated[None, Depends(csrf_protected)],
    ) -> JobRead:
        job = await services.repository.retry_job(job_id)
        if not job:
            raise HTTPException(status.HTTP_409_CONFLICT, "ä½œä¸šä¸å­˜åœ¨æˆ–å½“å‰çŠ¶æ€ä¸å¯é‡è¯•")
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

    @app.geï]ô¶‰žËkºwµçt€ômt(€€€€€€€€€€€…ÑÑ•µÁÑ}‰Õ™™•Èè±¥ÍÑm¹åt€ômt(€€€€€€€€€€€É•ÍÕ±Ðè‘¥ÑmÍÑÈ°¹åt€ô‘¥Ð¡¥¹¥Ñ¥…°¤(€€€€€€€€€€€¥˜ÕÉÉ•¹Ñ}Ñ…Í¬è(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•¥ÍÑ•É}…•¹Ñ}Ñ…Í¬¡ÉÕ¹}¥°ÕÉÉ•¹Ñ}Ñ…Í¬¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹ÕÁ‘…Ñ•}½Ý¹•‘}…•¹Ñ}ÉÕ¸ (€€€€€€€€€€€€€€€ÉÕ¹}¥°ÕÍ•È¹¥°ÍÑ…ÑÕÌô‰ÉÕ¹¹¥¹œˆ(€€€€€€€€€€€€¤(€€€€€€€€€€€å¥•±MMÙ•¹Ð¡•Ù•¹Ðô‰ÉÕ¹}ÍÑ…ÉÑ•ˆ°ÉÕ¹}¥õÉÕ¹}¥¤¹•¹½‘” ¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€É…Á¡}½¹™¥œ€ôì(€€€€€€€€€€€€€€€€€€€€‰É•ÕÉÍ¥½¹}±¥µ¥Ðˆè€à°(€€€€€€€€€€€€€€€€€€€€‰½¹™¥ÕÉ…‰±”ˆèì‰Ñ¡É•…‘}¥ˆèÑ¡É•…‘}¥‘ô°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€€€€Ý¥Ñ ½±±•Ñ}µ½‘•±}…ÑÑ•µÁÑÌ ¤…Ì…ÑÑ•µÁÑ}‰Õ™™•Èè(€€€€€€€€€€€€€€€€€€€¥˜¡…Í…ÑÑÈ¡Í•ÉÙ¥•Ì¹…•¹Ñ}É…Á °€‰…ÍÑÉ•…´ˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€…Íå¹Œ™½ÈÉ…Á¡}•Ù•¹Ð¥¸Í•ÉÙ¥•Ì¹…•¹Ñ}É…Á ¹…ÍÑÉ•…´ (€€€€€€€€€€€€€€€€€€€€€€€€€€€¥¹¥Ñ¥…°°É…Á¡}½¹™¥œ°ÍÑÉ•…µ}µ½‘”ô‰‘•‰Õœˆ(€€€€€€€€€€€€€€€€€€€€€€€€¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€•Ù•¹Ñ}ÑåÁ”€ôÍÑÈ¡É…Á¡}•Ù•¹Ð¹•Ð ‰ÑåÁ”ˆ°€ˆˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Á…å±½…‘}‘…Ñ„€ôÉ…Á¡}•Ù•¹Ð¹•Ð ‰Á…å±½…ˆ°íô¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡Á…å±½…‘}‘…Ñ„°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€É…Ý}¹½‘”€ôÍÑÈ¡Á…å±½…‘}‘…Ñ„¹•Ð ‰¹…µ”ˆ°€ˆˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Ñ…Í­}¥€ôÍÑÈ¡Á…å±½…‘}‘…Ñ„¹•Ð ‰¥ˆ°€ˆˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ•À€ô¥¹Ð¡É…Á¡}•Ù•¹Ð¹•Ð ‰ÍÑ•Àˆ°€À¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜•Ù•¹Ñ}ÑåÁ”€ôô€‰Ñ…Í¬ˆè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜É…Ý}¹½‘”¹½Ð¥¸}AU	1%}9Q}9=Lè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¹½‘•}ÍÑ…ÉÑ•‘}…ÑmÑ…Í­}¥‘t€ôÑ¥µ”¹Á•É™}½Õ¹Ñ•È ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¹½‘•}…ÑÑ•µÁÑ}½™™Í•ÑÍmÑ…Í­}¥‘t€ô±•¸¡…ÑÑ•µÁÑ}‰Õ™™•È¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰¹½‘•}ÍÑ…ÉÑ•ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘…Ñ„õì‰¹½‘”ˆèÉ…Ý}¹½‘”°€‰ÍÑ•ÀˆèÍÑ•Áô°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜É…Ý}¹½‘”€ôô€‰É•ÑÉ¥•Ù•}±¥‰É…Éäˆè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰Ñ½½±}ÍÑ…ÉÑ•ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘…Ñ„õì‰Ñ½½°ˆè€‰Í•…É¡}±¥‰É…Éä‰ô°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•±¥˜É…Ý}¹½‘”€ôô€‰Í•…É¡}…Éá¥Øˆè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰Ñ½½±}ÍÑ…ÉÑ•ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘…Ñ„õì‰Ñ½½°ˆè€‰Í•…É¡}…Éá¥Ø‰ô°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜•Ù•¹Ñ}ÑåÁ”€„ô€‰Ñ…Í­}É•ÍÕ±Ðˆè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘•±Ñ„€ôÁ…å±½…‘}‘…Ñ„¹•Ð ‰É•ÍÕ±Ðˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡‘•±Ñ„°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð¹ÕÁ‘…Ñ”¡‘•±Ñ„¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥¹Ñ•ÉÉÕÁÑÌ€ôÁ…å±½…‘}‘…Ñ„¹•Ð ‰¥¹Ñ•ÉÉÕÁÑÌˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¥¹Ñ•ÉÉÕÁÑÌè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ñl‰}}¥¹Ñ•ÉÉÕÁÑ}|‰t€ô¥¹Ñ•ÉÉÕÁÑÌ(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜É…Ý}¹½‘”¹½Ð¥¸}AU	1%}9Q}9=Lè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌ€ôÉ½Õ¹ (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¡Ñ¥µ”¹Á•É™}½Õ¹Ñ•È ¤€´¹½‘•}ÍÑ…ÉÑ•‘}…Ð¹Á½À¡Ñ…Í­}¥°ÉÕ¹}ÍÑ…ÉÑ•‘}…Ð¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¨€ÄÀÀÀ(€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€½™™Í•Ð€ô¹½‘•}…ÑÑ•µÁÑ}½™™Í•ÑÌ¹Á½À¡Ñ…Í­}¥°±•¸¡…ÑÑ•µÁÑ}‰Õ™™•È¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¹½‘•}µ½‘•±}…ÑÑ•µÁÑÌ€ôl(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…ÑÑ•µÁÐ¹…Í}‘¥Ð ¤™½È…ÑÑ•µÁÐ¥¸…ÑÑ•µÁÑ}‰Õ™™•Ém½™™Í•Ðét(€€€€€€€€€€€€€€€€€€€€€€€€€€€t(€€€€€€€€€€€€€€€€€€€€€€€€€€€™…¥±•€ô‰½½°¡Á…å±½…‘}‘…Ñ„¹•Ð ‰•ÉÉ½Èˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉ…•}¥Ñ•´€ôì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¹½‘”ˆèÉ…Ý}¹½‘”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑ•ÀˆèÍÑ•À°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰™…¥±•ˆ¥˜™…¥±••±Í”€‰½µÁ±•Ñ•ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰‘ÕÉ…Ñ¥½¹}µÌˆè‘ÕÉ…Ñ¥½¹}µÌ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½É}½‘”ˆè€‰9=}aUQ%=9}%1ˆ¥˜™…¥±••±Í”9½¹”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰µ½‘•±}…ÑÑ•µÁÑÌˆè¹½‘•}µ½‘•±}…ÑÑ•µÁÑÌ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€€€€€€€€€€€€€€€€¹½‘•}ÑÉ…”¹…ÁÁ•¹¡ÑÉ…•}¥Ñ•´¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰¹½‘•}™¥¹¥Í¡•ˆ°ÉÕ¹}¥õÉÕ¹}¥°‘…Ñ„õÑÉ…•}¥Ñ•´(€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜É…Ý}¹½‘”€ôô€‰É…‘•}•Ù¥‘•¹”ˆè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰Ñ½½±}™¥¹¥Í¡•ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘…Ñ„õì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ½½°ˆè€‰Í•…É¡}±¥‰É…Éäˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}ÅÕ…±¥Ñäˆè‘¥Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð¹•Ð ‰•Ù¥‘•¹•}ÅÕ…±¥Ñäˆ°íô¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€•±¥˜É…Ý}¹½‘”€ôô€‰Í•…É¡}…Éá¥Øˆè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰Ñ½½±}™¥¹¥Í¡•ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘…Ñ„õì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ½½°ˆè€‰Í•…É¡}…Éá¥Øˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…¹‘¥‘…Ñ•}½Õ¹Ðˆè±•¸ (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð¹•Ð ‰…Éá¥Ù}…¹‘¥‘…Ñ•Ìˆ°mt¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô…Ý…¥ÐÍ•ÉÙ¥•Ì¹…•¹Ñ}É…Á ¹…¥¹Ù½­”¡¥¹¥Ñ¥…°°É…Á¡}½¹™¥œ¤(€€€€€€€€€€€€€€€µ½‘•±}…ÑÑ•µÁÑÌ€ôm…ÑÑ•µÁÐ¹…Í}‘¥Ð ¤™½È…ÑÑ•µÁÐ¥¸…ÑÑ•µÁÑ}‰Õ™™•Ét(€€€€€€€€€€€€€€€ÅÕ…±¥Ñä€ô‘¥Ð¡É•ÍÕ±Ð¹•Ð ‰•Ù¥‘•¹•}ÅÕ…±¥Ñäˆ°íô¤¤(€€€€€€€€€€€€€€€¥¹Ñ•ÉÉÕÁÑÌ€ôÉ•ÍÕ±Ð¹•Ð ‰}}¥¹Ñ•ÉÉÕÁÑ}|ˆ°mt¤(€€€€€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌ€ôÉ½Õ¹ ¡Ñ¥µ”¹Á•É™}½Õ¹Ñ•È ¤€´ÉÕ¹}ÍÑ…ÉÑ•‘}…Ð¤€¨€ÄÀÀÀ¤(€€€€€€€€€€€€€€€¥˜¥¹Ñ•ÉÉÕÁÑÌè(€€€€€€€€€€€€€€€€€€€Á•¹‘¥¹œ€ô•Ñ…ÑÑÈ¡¥¹Ñ•ÉÉÕÁÑÍlÁt°€‰Ù…±Õ”ˆ°íô¤(€€€€€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹ÕÁ‘…Ñ•}½Ý¹•‘}…•¹Ñ}ÉÕ¸ (€€€€€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€€€€ÕÍ•È¹¥°(€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÕÌô‰¥¹Ñ•ÉÉÕÁÑ•ˆ°(€€€€€€€€€€€€€€€€€€€€€€€Ñ½½±}ÍÑ•ÁÌõ¥¹Ð¡É•ÍÕ±Ð¹•Ð ‰Ñ½½±}ÍÑ•ÁÌˆ°€À¤¤°(€€€€€€€€€€€€€€€€€€€€€€€Á•¹‘¥¹}…Ñ¥½¸õÁ•¹‘¥¹œ°(€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ñ}ÍÕµµ…Éäõì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…¹ÍÝ•Èˆè€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¥Ñ…Ñ¥½¹Ìˆèmt°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}ÅÕ…±¥ÑäˆèÅÕ…±¥Ñä°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¹½‘•}ÑÉ…”ˆè¹½‘•}ÑÉ…”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰µ½‘•±}…ÑÑ•µÁÑÌˆèµ½‘•±}…ÑÑ•µÁÑÌ°(€€€€€€€€€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌõ‘ÕÉ…Ñ¥½¹}µÌ°(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰¥¹Ñ•ÉÉÕÁÐˆ°ÉÕ¹}¥õÉÕ¹}¥°‘…Ñ„õì‰Á•¹‘¥¹}…Ñ¥½¸ˆèÁ•¹‘¥¹ô(€€€€€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€€€€€€€€€¥Ñ…Ñ¥½¹}Ù…±Õ•Ì€ô}¥Ñ…Ñ¥½¹}‘¥ÑÌ (€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð¹•Ð ‰¥Ñ…Ñ¥½¹Ìˆ°mt¤°É•ÍÕ±Ð¹•Ð ‰É•ÑÉ¥•Ù•‘}•Ù¥‘•¹”ˆ°mt¤(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹ÕÁ‘…Ñ•}½Ý¹•‘}…•¹Ñ}ÉÕ¸ (€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€ÕÍ•È¹¥°(€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÕÌõÉ•ÍÕ±Ð¹•Ð ‰ÍÑ…ÑÕÌˆ°€‰½µÁ±•Ñ•ˆ¤°(€€€€€€€€€€€€€€€€€€€Ñ½½±}ÍÑ•ÁÌõ¥¹Ð¡É•ÍÕ±Ð¹•Ð ‰Ñ½½±}ÍÑ•ÁÌˆ°€À¤¤°(€€€€€€€€€€€€€€€€€€€Á•¹‘¥¹}…Ñ¥½¸õ9½¹”°(€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ñ}ÍÕµµ…Éäõì(€€€€€€€€€€€€€€€€€€€€€€€€‰…¹ÍÝ•ÈˆèÍÑÈ¡É•ÍÕ±Ð¹•Ð ‰…¹ÍÝ•Èˆ°€ˆˆ¤¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰¥Ñ…Ñ¥½¹Ìˆè¥Ñ…Ñ¥½¹}Ù…±Õ•Ì°(€€€€€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}ÅÕ…±¥ÑäˆèÅÕ…±¥Ñä°(€€€€€€€€€€€€€€€€€€€€€€€€‰¹½‘•}ÑÉ…”ˆè¹½‘•}ÑÉ…”°(€€€€€€€€€€€€€€€€€€€€€€€€‰µ½‘•±}…ÑÑ•µÁÑÌˆèµ½‘•±}…ÑÑ•µÁÑÌ°(€€€€€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌõ‘ÕÉ…Ñ¥½¹}µÌ°(€€€€€€€€€€€€€€€€€€€•ÉÉ½É}½‘”õÉ•ÍÕ±Ð¹•Ð ‰•ÉÉ½Èˆ¤°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€…¹ÍÝ•È€ôÍÑÈ¡É•ÍÕ±Ð¹•Ð ‰…¹ÍÝ•Èˆ°€ˆˆ¤¤(€€€€€€€€€€€€€€€™½ÈÁ¥•”¥¸É”¹™¥¹‘…±°¡Èˆ¹ìÄ°Ðáôˆ°…¹ÍÝ•È°™±…ÌõÉ”¹L¤è(€€€€€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰µ•ÍÍ…•}‘•±Ñ„ˆ°ÉÕ¹}¥õÉÕ¹}¥°‘…Ñ„õì‰‘•±Ñ„ˆèÁ¥••ô(€€€€€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€€€€€€€€€…Ý…¥Ð…Íå¹¥¼¹Í±••À À¤(€€€€€€€€€€€€€€€™½È‘…Ñ„¥¸¥Ñ…Ñ¥½¹}Ù…±Õ•Ìè(€€€€€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð¡•Ù•¹Ðô‰¥Ñ…Ñ¥½¸ˆ°ÉÕ¹}¥õÉÕ¹}¥°‘…Ñ„õ‘…Ñ„¤¹•¹½‘” ¤(€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰ÉÕ¹}™¥¹¥Í¡•ˆ°(€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€‘…Ñ„õì(€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆèÉ•ÍÕ±Ð¹•Ð ‰ÍÑ…ÑÕÌˆ°€‰½µÁ±•Ñ•ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰‘ÕÉ…Ñ¥½¹}µÌˆè‘ÕÉ…Ñ¥½¹}µÌ°(€€€€€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€•á•ÁÐ…Íå¹¥¼¹…¹•±±•‘ÉÉ½Èè(€€€€€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌ€ôÉ½Õ¹ ¡Ñ¥µ”¹Á•É™}½Õ¹Ñ•È ¤€´ÉÕ¹}ÍÑ…ÉÑ•‘}…Ð¤€¨€ÄÀÀÀ¤(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹ÕÁ‘…Ñ•}½Ý¹•‘}…•¹Ñ}ÉÕ¸ (€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€ÕÍ•È¹¥°(€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÕÌô‰…¹•±±•ˆ°(€€€€€€€€€€€€€€€€€€€Á•¹‘¥¹}…Ñ¥½¸õ9½¹”°(€€€€€€€€€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌõ‘ÕÉ…Ñ¥½¹}µÌ°(€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ñ}ÍÕµµ…Éäõì(€€€€€€€€€€€€€€€€€€€€€€€€‰…¹ÍÝ•Èˆè€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰¥Ñ…Ñ¥½¹Ìˆèmt°(€€€€€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}ÅÕ…±¥Ñäˆè‘¥Ð¡É•ÍÕ±Ð¹•Ð ‰•Ù¥‘•¹•}ÅÕ…±¥Ñäˆ°íô¤¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰¹½‘•}ÑÉ…”ˆè¹½‘•}ÑÉ…”°(€€€€€€€€€€€€€€€€€€€€€€€€‰µ½‘•±}…ÑÑ•µÁÑÌˆèl(€€€€€€€€€€€€€€€€€€€€€€€€€€€…ÑÑ•µÁÐ¹…Í}‘¥Ð ¤™½È…ÑÑ•µÁÐ¥¸…ÑÑ•µÁÑ}‰Õ™™•È(€€€€€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€€€€•ÉÉ½É}½‘”ô‰9Q}IU9}911ˆ°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰ÉÕ¹}™¥¹¥Í¡•ˆ°(€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€‘…Ñ„õì‰ÍÑ…ÑÕÌˆè€‰…¹•±±•ˆ°€‰‘ÕÉ…Ñ¥½¹}µÌˆè‘ÕÉ…Ñ¥½¹}µÍô°(€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌ€ôÉ½Õ¹ ¡Ñ¥µ”¹Á•É™}½Õ¹Ñ•È ¤€´ÉÕ¹}ÍÑ…ÉÑ•‘}…Ð¤€¨€ÄÀÀÀ¤(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹ÕÁ‘…Ñ•}½Ý¹•‘}…•¹Ñ}ÉÕ¸ (€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€ÕÍ•È¹¥°(€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÕÌô‰™…¥±•ˆ°(€€€€€€€€€€€€€€€€€€€•ÉÉ½É}½‘”ô‰9Q}IU9}%1ˆ°(€€€€€€€€€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌõ‘ÕÉ…Ñ¥½¹}µÌ°(€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ñ}ÍÕµµ…Éäõì(€€€€€€€€€€€€€€€€€€€€€€€€‰…¹ÍÝ•Èˆè€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰¥Ñ…Ñ¥½¹Ìˆèmt°(€€€€€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}ÅÕ…±¥Ñäˆèíô°(€€€€€€€€€€€€€€€€€€€€€€€€‰¹½‘•}ÑÉ…”ˆè¹½‘•}ÑÉ…”°(€€€€€€€€€€€€€€€€€€€€€€€€‰µ½‘•±}…ÑÑ•µÁÑÌˆèl(€€€€€€€€€€€€€€€€€€€€€€€€€€€…ÑÑ•µÁÐ¹…Í}‘¥Ð ¤™½È…ÑÑ•µÁÐ¥¸…ÑÑ•µÁÑ}‰Õ™™•È(€€€€€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€å¥•±MMÙ•¹Ð (€€€€€€€€€€€€€€€€€€€•Ù•¹Ðô‰•ÉÉ½Èˆ°ÉÕ¹}¥õÉÕ¹}¥°‘…Ñ„õì‰µ•ÍÍ…”ˆè€‰•¹Ðƒ¢þC¢†3–’Ç¢Ò”‰ô(€€€€€€€€€€€€€€€€¤¹•¹½‘” ¤(€€€€€€€€€€€™¥¹…±±äè(€€€€€€€€€€€€€€€¥˜ÕÉÉ•¹Ñ}Ñ…Í¬è(€€€€€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹Õ¹É•¥ÍÑ•É}…•¹Ñ}Ñ…Í¬¡ÉÕ¹}¥°ÕÉÉ•¹Ñ}Ñ…Í¬¤((€€€€€€€É•ÑÕÉ¸MÑÉ•…µ¥¹I•ÍÁ½¹Í”¡•Ù•¹ÑÌ ¤°µ•‘¥…}ÑåÁ”ô‰Ñ•áÐ½•Ù•¹ÐµÍÑÉ•…´ˆ¤((€€€…ÁÀ¹•Ð ˆ½…Á¤½ØÄ½…•¹Ð½ÉÕ¹Ì½íÉÕ¹}¥‘ôˆ°É•ÍÁ½¹Í•}µ½‘•°õ•¹ÑIÕ¹I•…¤(€€€…Íå¹Œ‘•˜•Ñ}…•¹Ñ}ÉÕ¸ (€€€€€€€ÉÕ¹}¥èÍÑÈ°ÕÍ•Èè¹¹½Ñ…Ñ•‘mUÍ•ÉI•½É°•Á•¹‘Ì¡ÕÉÉ•¹Ñ}ÕÍ•È¥t(€€€€¤€´ø•¹ÑIÕ¹I•…è(€€€€€€€ÉÕ¸€ô…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹•Ñ}½Ý¹•‘}…•¹Ñ}ÉÕ¸¡ÉÕ¹}¥°ÕÍ•È¹¥¤(€€€€€€€¥˜¹½ÐÉÕ¸è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÌ¹!QQA|ÐÀÑ}9=Q}=U9°€‹¢þC¢†3’â7–¶c–r ˆ¤(€€€€€€€É•ÑÕÉ¸}…•¹Ñ}ÉÕ¹}É•…¡ÉÕ¸¤((€€€…ÁÀ¹Á½ÍÐ ˆ½…Á¤½ØÄ½…•¹Ð½ÉÕ¹Ì½íÉÕ¹}¥‘ô½É•ÍÕµ”ˆ°É•ÍÁ½¹Í•}µ½‘•°õ•¹ÑIÕ¹I•…¤(€€€…Íå¹Œ‘•˜É•ÍÕµ•}…•¹Ñ}ÉÕ¸ (€€€€€€€ÉÕ¹}¥èÍÑÈ°(€€€€€€€Á…å±½…è•¹ÑI•ÍÕµ•I•ÅÕ•ÍÐ°(€€€€€€€ÕÍ•Èè¹¹½Ñ…Ñ•‘mUÍ•ÉI•½É°•Á•¹‘Ì¡ÕÉÉ•¹Ñ}ÕÍ•È¥t°(€€€€€€€|è¹¹½Ñ…Ñ•‘m9½¹”°•Á•¹‘Ì¡ÍÉ™}ÁÉ½Ñ•Ñ•¥t°(€€€€¤€´ø•¹ÑIÕ¹I•…è(€€€€€€€ÉÕ¸€ô…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹•Ñ}½Ý¹•‘}…•¹Ñ}ÉÕ¸¡ÉÕ¹}¥°ÕÍ•È¹¥¤(€€€€€€€¥˜¹½ÐÉÕ¸è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÌ¹!QQA|ÐÀÑ}9=Q}=U9°€‹¢þC¢†3’â7–¶c–r ˆ¤(€€€€€€€¥˜ÉÕ¸¹ÍÑ…ÑÕÌ€„ô€‰¥¹Ñ•ÉÉÕÁÑ•ˆè(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÌ¹!QQA|ÐÀå}=91%P°€‹¢þC¢†3šr«–r£ž¶'–úž†»¢ºˆ¤(€€€€€€€Á•¹‘¥¹œ€ôÉÕ¸¹Á•¹‘¥¹}…Ñ¥½¸½Èíô(€€€€€€€¥˜Á•¹‘¥¹œ¹•Ð ‰…Ñ¥½¹}¥ˆ¤€„ôÁ…å±½…¹…Ñ¥½¹}¥è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÌ¹!QQA|ÐÀå}=91%P°€‹–úž†»¢º“–*£’ös’â7–2ç¦4ˆ¤(€€€€€€€¥˜¹½Ð½¹™¥œ¹¥Í}‘•µ¼è(€€€€€€€€€€€™É½´±…¹É…Á ¹ÑåÁ•Ì¥µÁ½ÉÐ½µµ…¹((€€€€€€€€€€€É•ÍÕ±Ð€ô…Ý…¥ÐÍ•ÉÙ¥•Ì¹…•¹Ñ}É…Á ¹…¥¹Ù½­” (€€€€€€€€€€€€€€€½µµ…¹¡É•ÍÕµ”õÁ…å±½…¹‘•¥Í¥½¸¤°(€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€‰É•ÕÉÍ¥½¹}±¥µ¥Ðˆè€à°(€€€€€€€€€€€€€€€€€€€€‰½¹™¥ÕÉ…‰±”ˆèì‰Ñ¡É•…‘}¥ˆèÉÕ¸¹Ñ¡É•…‘}¥‘ô°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹ÕÁ‘…Ñ•}½Ý¹•‘}…•¹Ñ}ÉÕ¸ (€€€€€€€€€€€€€€€ÉÕ¹}¥°(€€€€€€€€€€€€€€€ÕÍ•È¹¥°(€€€€€€€€€€€€€€€ÍÑ…ÑÕÌõÉ•ÍÕ±Ð¹•Ð ‰ÍÑ…ÑÕÌˆ°€‰½µÁ±•Ñ•ˆ¤°(€€€€€€€€€€€€€€€Ñ½½±}ÍÑ•ÁÌõ¥¹Ð¡É•ÍÕ±Ð¹•Ð ‰Ñ½½±}ÍÑ•ÁÌˆ°ÉÕ¸¹Ñ½½±}ÍÑ•ÁÌ¤¤°(€€€€€€€€€€€€€€€Á•¹‘¥¹}…Ñ¥½¸õ9½¹”°(€€€€€€€€€€€€€€€É•ÍÕ±Ñ}ÍÕµµ…Éäõì(€€€€€€€€€€€€€€€€€€€€‰…¹ÍÝ•ÈˆèÍÑÈ¡É•ÍÕ±Ð¹•Ð ‰…¹ÍÝ•Èˆ°€ˆˆ¤¤°(€€€€€€€€€€€€€€€€€€€€‰¥Ñ…Ñ¥½¹Ìˆè}¥Ñ…Ñ¥½¹}‘¥ÑÌ (€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð¹•Ð ‰¥Ñ…Ñ¥½¹Ìˆ°mt¤°É•ÍÕ±Ð¹•Ð ‰É•ÑÉ¥•Ù•‘}•Ù¥‘•¹”ˆ°mt¤(€€€€€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}ÅÕ…±¥Ñäˆè‘¥Ð¡É•ÍÕ±Ð¹•Ð ‰•Ù¥‘•¹•}ÅÕ…±¥Ñäˆ°íô¤¤°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€•ÉÉ½É}½‘”õÉ•ÍÕ±Ð¹•Ð ‰•ÉÉ½Èˆ¤°(€€€€€€€€€€€€¤(€€€€€€€•±Í”è(€€€€€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹ÕÁ‘…Ñ•}½Ý¹•‘}…•¹Ñ}ÉÕ¸ (€€€€€€€€€€€€€€€ÉÕ¹}¥°(€€€€€€€€€€€€€€€ÕÍ•È¹¥°(€€€€€€€€€€€€€€€ÍÑ…ÑÕÌô‰½µÁ±•Ñ•ˆ°(€€€€€€€€€€€€€€€Á•¹‘¥¹}…Ñ¥½¸õ9½¹”°(€€€€€€€€€€€€€€€É•ÍÕ±Ñ}ÍÕµµ…Éäõì(€€€€€€€€€€€€€€€€€€€€‰…¹ÍÝ•Èˆè€ (€€€€€€€€€€€€€€€€€€€€€€€€‹–ÞËš&ç––¾ó–—¾ò3¢¾ß¦'š.§–g¦'šZž2»–æÛ¢ÂžR£–>_š:Ÿ–¾ó–—š:—–>Žˆ(€€€€€€€€€€€€€€€€€€€€€€€¥˜Á…å±½…¹‘•¥Í¥½¸€ôô€‰…ÁÁÉ½Ù”ˆ(€€€€€€€€€€€€€€€€€€€€€€€•±Í”€‹–ÞË–>[šÚ#–¾ó–—Žˆ(€€€€€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€€€€€€‰¥Ñ…Ñ¥½¹Ìˆèmt°(€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}ÅÕ…±¥ÑäˆèÉÕ¸¹É•ÍÕ±Ñ}ÍÕµµ…Éä¹•Ð ‰•Ù¥‘•¹•}ÅÕ…±¥Ñäˆ°íô¤(€€€€€€€€€€€€€€€€€€€¥˜ÉÕ¸¹É•ÍÕ±Ñ}ÍÕµµ…Éä(€€€€€€€€€€€€€€€€€€€•±Í”íô°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸…Ý…¥Ð•Ñ}…•¹Ñ}ÉÕ¸¡ÉÕ¹}¥°ÕÍ•È¤((€€€…ÁÀ¹Á½ÍÐ ˆ½…Á¤½ØÄ½…•¹Ð½ÉÕ¹Ì½íÉÕ¹}¥‘ô½…¹•°ˆ°É•ÍÁ½¹Í•}µ½‘•°õ•¹ÑIÕ¹I•…¤(€€€…Íå¹Œ‘•˜…¹•±}…•¹Ñ}ÉÕ¸ (€€€€€€€ÉÕ¹}¥èÍÑÈ°(€€€€€€€ÕÍ•Èè¹¹½Ñ…Ñ•‘mUÍ•ÉI•½É°•Á•¹‘Ì¡ÕÉÉ•¹Ñ}ÕÍ•È¥t°(€€€€€€€|è¹¹½Ñ…Ñ•‘m9½¹”°•Á•¹‘Ì¡ÍÉ™}ÁÉ½Ñ•Ñ•¥t°(€€€€¤€´ø•¹ÑIÕ¹I•…è(€€€€€€€ÉÕ¸€ô…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹•Ñ}½Ý¹•‘}…•¹Ñ}ÉÕ¸¡ÉÕ¹}¥°ÕÍ•È¹¥¤(€€€€€€€¥˜¹½ÐÉÕ¸è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÌ¹!QQA|ÐÀÑ}9=Q}=U9°€‹¢þC¢†3’â7–¶c–r ˆ¤(€€€€€€€¥˜ÉÕ¸¹ÍÑ…ÑÕÌ€ôô€‰…¹•±±•ˆè(€€€€€€€€€€€É•ÑÕÉ¸}…•¹Ñ}ÉÕ¹}É•…¡ÉÕ¸¤(€€€€€€€¥˜ÉÕ¸¹ÍÑ…ÑÕÌ¥¸ì‰½µÁ±•Ñ•ˆ°€‰™…¥±•‰ôè(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÌ¹!QQA|ÐÀå}=91%P°€‹¢þC¢†3–ÞËžî?žîOšv|ˆ¤(€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹É•Á½Í¥Ñ½Éä¹ÕÁ‘…Ñ•}½Ý¹•‘}…•¹Ñ}ÉÕ¸ (€€€€€€€€€€€ÉÕ¹}¥°(€€€€€€€€€€€ÕÍ•È¹¥°(€€€€€€€€€€€ÍÑ…ÑÕÌô‰…¹•±±•ˆ°(€€€€€€€€€€€Á•¹‘¥¹}…Ñ¥½¸õ9½¹”°(€€€€€€€€€€€•ÉÉ½É}½‘”ô‰9Q}IU9}911ˆ°(€€€€€€€€¤(€€€€€€€…Ý…¥ÐÍ•ÉÙ¥•Ì¹…¹•±}…•¹Ñ}Ñ…Í¬¡ÉÕ¹}¥¤(€€€€€€€É•ÑÕÉ¸…Ý…¥Ð•Ñ}…•¹Ñ}ÉÕ¸¡ÉÕ¹}¥°ÕÍ•È¤((€€€É•ÑÕÉ¸…ÁÀ(()…ÁÀ€ôÉ•…Ñ•}…ÁÀ ¤