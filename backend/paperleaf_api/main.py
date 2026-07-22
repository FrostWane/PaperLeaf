"""PaperLeaf FastAPI 应用入口。"""

import asyncio
import hashlib
import re
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

from .agent.graph import build_agent_graph
from .agent.tools import DemoLibrarySearch, SQLLibrarySearch
from .artifacts import load_paper_evidence, structure_graph, summarize_evidence
from .arxiv_service import fetch_arxiv_pdf, search_arxiv
from .config import Settings, settings
from .models import PaperStatus, UserRole
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
        self.retriever = DemoLibrarySearch() if config.is_demo else SQLLibrarySearch()
        self.agent_graph = build_agent_graph(retriever=self.retriever)


def _paper_read(paper: PaperRecord) -> PaperRead:
    return PaperRead.model_validate(paper)


def _user_read(user: UserRecord) -> UserRead:
    return UserRead.model_validate(user)


def _citation_dicts(items: list[Any]) -> list[dict[str, Any]]:
    return [
        item.__dict__ if hasattr(item, "__dict__") else dict(item)
        for item in items
    ]


def _agent_run_read(record: Any) -> AgentRunRead:
    summary = record.result_summary or {}
    return AgentRunRead(
        run_id=record.id,
        session_id=record.session_id,
        status=record.status,
        answer=summary.get("answer", ""),
        citations=summary.get("citations", []),
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
        # LangGraph 使用独立的 PostgreSQL Checkpointer，业务表不存放隐藏推理内容。
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpoint_url = config.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        async with AsyncPostgresSaver.from_conn_string(checkpoint_url) as checkpointer:
            await checkpointer.setup()
            services.agent_graph = build_agent_graph(
                retriever=services.retriever, checkpointer=checkpointer
            )
            yield

    app = FastAPI(
        title="PaperLeaf API",
        version="0.1.0",
        description="个人科研文献库、页级 RAG 与受控研究 Agent",
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
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
        user = await service(request).repository.user_for_session(session_token)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话无效或已过期")
        if user.must_change_password and request.url.path not in {
            "/api/v1/auth/me",
            "/api/v1/auth/change-password",
            "/api/v1/auth/logout",
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
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

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
        response.set_cookie(
            config.session_cookie, session_token, httponly=True, **cookie_options
        )
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
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前密码错误")
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
            raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
        removes_admin = target.role == UserRole.admin and (
            payload.active is False or payload.role == UserRole.user
        )
        if removes_admin and await services.repository.count_active_admins() <= 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "不能停用或降级最后一名管理员")
        if target.id == admin.id and payload.active is False:
            raise HTTPException(status.HTTP_409_CONFLICT, "不能停用当前管理员")
        updated = await services.repository.update_user(
            user_id, **payload.model_dump(exclude_none=True)
        )
        return _user_read(updated)  # type: ignore[arg-type]

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

    @app.get("/api/v1/collections", response_model=list[CollectionRead])
    async def list_collections(
        user: Annotated[UserRecord, Depends(current_user)],
    ) -> list[CollectionRead]:
        records = await services.repository.list_collections(user.id)
        return [CollectionRead.model_validate(item) for item in records]

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
                user.id, payload.name, payload.description
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
        if not await services.repository.delete_collection(collection_id, user.id):
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

    @app.get("/api/v1/tags", response_model=list[TagRead])
    async def list_tags(
        user: Annotated[UserRecord, Depends(current_user)],
    ) -> list[TagRead]:
        records = await services.repository.list_tags(user.id)
        return [TagRead.model_validate(item) for item in records]

    @app.post("/api/v1/tags", response_model=TagRead, status_code=status.HTTP_201_CREATED)
    async def create_tag(
        payload: TagCreate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> TagRead:
        try:
            record = await services.repository.create_tag(user.id, payload.name, payload.color)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return TagRead.model_validate(record)

    @app.patch("/api/v1/tags/{tag_id}", response_model=TagRead)
    async def update_tag(
        tag_id: str,
        payload: TagUpdate,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> TagRead:
        try:
            record = await services.repository.update_tag(
                tag_id, user.id, **payload.model_dump(exclude_unset=True)
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not record:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "标签不存在")
        return TagRead.model_validate(record)

    @app.delete("/api/v1/tags/{tag_id}")
    async def delete_tag(
        tag_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> dict[str, str]:
        if not await services.repository.delete_tag(tag_id, user.id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "标签不存在")
        return {"status": "deleted"}

    @app.post("/api/v1/tags/{tag_id}/papers/{paper_id}")
    async def add_paper_tag(
        tag_id: str,
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> dict[str, bool]:
        assigned = await services.repository.set_paper_tag(tag_id, paper_id, user.id, True)
        if not assigned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "标签或文献不存在")
        return {"assigned": True}

    @app.delete("/api/v1/tags/{tag_id}/papers/{paper_id}")
    async def remove_paper_tag(
        tag_id: str,
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> dict[str, bool]:
        assigned = await services.repository.set_paper_tag(tag_id, paper_id, user.id, False)
        if not assigned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "标签或文献不存在")
        return {"assigned": False}

    @app.get("/api/v1/papers", response_model=list[PaperRead])
    async def list_papers(user: Annotated[UserRecord, Depends(current_user)]) -> list[PaperRead]:
        return [_paper_read(item) for item in await services.repository.list_papers(user.id)]

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

    @app.get("/api/v1/papers/{paper_id}", response_model=PaperRead)
    async def get_paper(
        paper_id: str, user: Annotated[UserRecord, Depends(current_user)]
    ) -> PaperRead:
        return _paper_read(await owned_paper(paper_id, user))

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
        headers = {"Accept-Ranges": "bytes", "Content-Disposition": "inline"}
        try:
            byte_range = parse_byte_range(range_header, total)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                str(exc),
                headers={"Content-Range": f"bytes */{total}"},
            ) from exc
        if byte_range:
            body = await services.storage.read(
                paper.storage_key, byte_range.start, byte_range.end
            )
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
        paper = await owned_paper(paper_id, user)
        if paper.status not in {PaperStatus.failed, PaperStatus.partial}:
            raise HTTPException(status.HTTP_409_CONFLICT, "当前状态不允许重试")
        updated = await services.repository.update_owned_paper(
            paper_id, user.id, status=PaperStatus.queued
        )
        return _paper_read(updated)  # type: ignore[arg-type]

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
            content = await fetch_arxiv_pdf(payload.arxiv_id, config.max_pdf_bytes)
            validate_pdf(content, f"{payload.arxiv_id}.pdf", config.max_pdf_bytes)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "arXiv PDF 下载失败") from exc
        sha256 = hashlib.sha256(content).hexdigest()
        paper_id = str(uuid.uuid4())
        storage_key = f"{user.id}/{paper_id}/{sha256}.pdf"
        await services.storage.put(storage_key, content, "application/pdf")
        record = PaperRecord(
            id=paper_id,
            owner_id=user.id,
            title=f"arXiv {payload.arxiv_id}",
            authors=[],
            year=None,
            abstract=None,
            doi=None,
            arxiv_id=payload.arxiv_id,
            filename=f"{payload.arxiv_id}.pdf",
            storage_key=storage_key,
            mime_type="application/pdf",
            size_bytes=len(content),
            sha256=sha256,
            page_count=None,
            status=PaperStatus.queued,
        )
        try:
            created = await services.repository.create_paper(record)
        except ValueError as exc:
            await services.storage.delete(storage_key)
            raise HTTPException(status.HTTP_409_CONFLICT, "文献已导入") from exc
        return _paper_read(created)

    @app.post("/api/v1/papers/{paper_id}/summary", response_model=SummaryResponse)
    async def summarize_paper(
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> SummaryResponse:
        await owned_paper(paper_id, user)
        if config.is_demo:
            raise HTTPException(status.HTTP_409_CONFLICT, "演示模式不处理真实 PDF")
        evidence = await load_paper_evidence(user.id, paper_id)
        if not evidence:
            raise HTTPException(status.HTTP_409_CONFLICT, "文献尚未完成解析")
        content, mode = await summarize_evidence(evidence)
        return SummaryResponse(
            paper_id=paper_id,
            content=content,
            citations=[
                ArtifactCitation(chunk_id=item.chunk_id, physical_page=item.physical_page)
                for item in evidence[:12]
            ],
            mode=mode,
        )

    @app.post(
        "/api/v1/papers/{paper_id}/structure-graph", response_model=StructureGraphResponse
    )
    async def build_paper_structure_graph(
        paper_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> StructureGraphResponse:
        await owned_paper(paper_id, user)
        if config.is_demo:
            raise HTTPException(status.HTTP_409_CONFLICT, "演示模式不处理真实 PDF")
        evidence = await load_paper_evidence(user.id, paper_id, limit=16)
        if not evidence:
            raise HTTPException(status.HTTP_409_CONFLICT, "文献尚未完成解析")
        nodes, edges, mermaid = structure_graph(evidence)
        return StructureGraphResponse(
            paper_id=paper_id, nodes=nodes, edges=edges, mermaid=mermaid
        )

    @app.post("/api/v1/chat/sessions/{session_id}/messages")
    async def send_chat_message(
        session_id: str,
        payload: ChatMessageRequest,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> StreamingResponse:
        if not 1 <= len(session_id) <= 100:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "会话 ID 长度无效")
        run_id = str(uuid.uuid4())
        thread_id = f"{user.id}:{session_id}:{run_id}"
        initial = {
            "run_id": run_id,
            "session_id": session_id,
            "user_id": user.id,
            "query": payload.content,
            "scope": payload.scope,
            "selected_paper_ids": payload.selected_paper_ids,
            "web_enabled": payload.web_enabled,
            "tool_steps": 0,
            "status": "pending",
        }
        await services.repository.create_agent_run(run_id, user.id, session_id, thread_id)

        async def events() -> AsyncIterator[str]:
            yield SSEEvent(event="run_started", run_id=run_id).encode()
            yield SSEEvent(
                event="tool_started", run_id=run_id, data={"tool": "search_library"}
            ).encode()
            try:
                result = await services.agent_graph.ainvoke(
                    initial, {"recursion_limit": 8, "configurable": {"thread_id": thread_id}}
                )
                interrupts = result.get("__interrupt__", [])
                if interrupts:
                    pending = getattr(interrupts[0], "value", {})
                    await services.repository.update_owned_agent_run(
                        run_id,
                        user.id,
                        status="interrupted",
                        tool_steps=int(result.get("tool_steps", 0)),
                        pending_action=pending,
                        result_summary={"answer": "", "citations": []},
                    )
                    yield SSEEvent(
                        event="interrupt", run_id=run_id, data={"pending_action": pending}
                    ).encode()
                    return
                citation_values = _citation_dicts(result.get("citations", []))
                await services.repository.update_owned_agent_run(
                    run_id,
                    user.id,
                    status=result.get("status", "completed"),
                    tool_steps=int(result.get("tool_steps", 0)),
                    pending_action=None,
                    result_summary={
                        "answer": str(result.get("answer", "")),
                        "citations": citation_values,
                    },
                    error_code=result.get("error"),
                )
                yield SSEEvent(
                    event="tool_finished", run_id=run_id, data={"tool": "search_library"}
                ).encode()
                answer = str(result.get("answer", ""))
                for piece in re.findall(r".{1,48}", answer, flags=re.S):
                    yield SSEEvent(
                        event="message_delta", run_id=run_id, data={"delta": piece}
                    ).encode()
                    await asyncio.sleep(0)
                for data in citation_values:
                    yield SSEEvent(event="citation", run_id=run_id, data=data).encode()
                yield SSEEvent(
                    event="run_finished",
                    run_id=run_id,
                    data={"status": result.get("status", "completed")},
                ).encode()
            except Exception:
                await services.repository.update_owned_agent_run(
                    run_id,
                    user.id,
                    status="failed",
                    error_code="AGENT_RUN_FAILED",
                    result_summary={"answer": "", "citations": []},
                )
                yield SSEEvent(
                    event="error", run_id=run_id, data={"message": "Agent 运行失败"}
                ).encode()

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/v1/agent/runs/{run_id}", response_model=AgentRunRead)
    async def get_agent_run(
        run_id: str, user: Annotated[UserRecord, Depends(current_user)]
    ) -> AgentRunRead:
        run = await services.repository.get_owned_agent_run(run_id, user.id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "运行不存在")
        return _agent_run_read(run)

    @app.post("/api/v1/agent/runs/{run_id}/resume", response_model=AgentRunRead)
    async def resume_agent_run(
        run_id: str,
        payload: AgentResumeRequest,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> AgentRunRead:
        run = await services.repository.get_owned_agent_run(run_id, user.id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "运行不存在")
        if run.status != "interrupted":
            raise HTTPException(status.HTTP_409_CONFLICT, "运行未在等待确认")
        pending = run.pending_action or {}
        if pending.get("action_id") != payload.action_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "待确认动作不匹配")
        if not config.is_demo:
            from langgraph.types import Command

            result = await services.agent_graph.ainvoke(
                Command(resume=payload.decision),
                {
                    "recursion_limit": 8,
                    "configurable": {"thread_id": run.thread_id},
                },
            )
            await services.repository.update_owned_agent_run(
                run_id,
                user.id,
                status=result.get("status", "completed"),
                tool_steps=int(result.get("tool_steps", run.tool_steps)),
                pending_action=None,
                result_summary={
                    "answer": str(result.get("answer", "")),
                    "citations": _citation_dicts(result.get("citations", [])),
                },
                error_code=result.get("error"),
            )
        else:
            await services.repository.update_owned_agent_run(
                run_id,
                user.id,
                status="completed",
                pending_action=None,
                result_summary={
                    "answer": (
                        "已批准导入，请选择候选文献并调用受控导入接口。"
                        if payload.decision == "approve"
                        else "已取消导入。"
                    ),
                    "citations": [],
                },
            )
        return await get_agent_run(run_id, user)

    @app.post("/api/v1/agent/runs/{run_id}/cancel", response_model=AgentRunRead)
    async def cancel_agent_run(
        run_id: str,
        user: Annotated[UserRecord, Depends(current_user)],
        _: Annotated[None, Depends(csrf_protected)],
    ) -> AgentRunRead:
        run = await services.repository.get_owned_agent_run(run_id, user.id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "运行不存在")
        if run.status in {"completed", "failed", "cancelled"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "运行已经结束")
        await services.repository.update_owned_agent_run(
            run_id, user.id, status="cancelled", pending_action=None
        )
        return await get_agent_run(run_id, user)

    return app


app = create_app()
