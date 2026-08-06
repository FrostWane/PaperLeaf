"""业务仓库接口与离线可用的内存实现。

生产部署可将相同接口替换为 SQLAlchemy 实现；权限判断始终发生在仓库查询条件中，
避免先取出他人资源再在路由层过滤。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from .db import get_session_factory
from .models import (
    AgentRun,
    Collection,
    Job,
    JobStatus,
    Paper,
    PaperStatus,
    User,
    UserRole,
    UserSession,
    paper_collections,
)
from .security import digest_session_token, hash_password, verify_password


def now() -> datetime:
    return datetime.now(timezone.utc)


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
    status: PaperStatus = PaperStatus.uploaded
    archived_at: datetime | None = None
    last_opened_at: datetime | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


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
    status: JobStatus = JobStatus.queued
    progress: int = 0
    attempts: int = 0
    max_attempts: int = 3
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
    error_code: str | None = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


class ManagedUserNotFoundError(ValueError):
    """管理员准备修改的用户不存在。"""


class CurrentAdminProtectionError(ValueError):
    """当前登录管理员不能停用自己。"""


class LastAdminProtectionError(ValueError):
    """变更会移除最后一名活跃管理员。"""


MAX_COLLECTION_DEPTH = 5


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
                (item.id, height + 1)
                for item in owned.values()
                if item.parent_id == current
            )
    if parent_depth + subtree_height > MAX_COLLECTION_DEPTH:
        raise ValueError(f"集合最多支持 {MAX_COLLECTION_DEPTH} 层")


class Repository(Protocol):
    async def find_user_by_email(self, email: str) -> UserRecord | None: ...
    async def get_user(self, user_id: str) -> UserRecord | None: ...
    async def create_user(
        self, email: str, password: str, role: UserRole, must_change_password: bool = True
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
    async def list_papers(
        self,
        owner_id: str,
        collection_id: str | None = None,
        unfiled: bool = False,
    ) -> list[PaperRecord]: ...
    async def get_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None: ...
    async def update_owned_paper(
        self, paper_id: str, owner_id: str, **changes: object
    ) -> PaperRecord | None: ...
    async def requeue_owned_paper(
        self, paper_id: str, owner_id: str
    ) -> PaperRecord | None: ...
    async def delete_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None: ...
    async def touch_paper_opened(self, paper_id: str, owner_id: str) -> PaperRecord | None: ...
    async def set_papers_archived(
        self, paper_ids: list[str], owner_id: str, archived: bool
    ) -> list[str] | None: ...
    async def list_collection_memberships(self, owner_id: str) -> dict[str, list[str]]: ...
    async def resolve_collection_paper_ids(
        self, collection_id: str, owner_id: str, *, ready_only: bool = False
    ) -> list[str] | None: ...


class MemoryRepository:
    """Demo/Test 仓库；单进程使用，不作为生产持久化方案。"""

    def __init__(self, session_secret: str) -> None:
        self.users: dict[str, UserRecord] = {}
        self.papers: dict[str, PaperRecord] = {}
        self.sessions: dict[str, tuple[str, datetime]] = {}
        self.collections: dict[str, CollectionRecord] = {}
        self.paper_collections: set[tuple[str, str]] = set()
        self.jobs: dict[str, JobRecord] = {}
        self.agent_runs: dict[str, AgentRunRecord] = {}
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
        self, email: str, password: str, role: UserRole, must_change_password: bool = True
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
        for key in ("active", "role", "must_change_password", "display_name", "preferences"):
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
            removes_active_admin = user.active and user.role == UserRole.admin and (
                changes.get("active") is False or changes.get("role") == UserRole.user
            )
            if removes_active_admin:
                active_admins = sum(
                    1
                    for item in self.users.values()
                    if item.active and item.role == UserRole.admin
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
                    digest: value
                    for digest, value in self.sessions.items()
                    if value[0] != user_id
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
        filed_ids = (
            {paper_id for paper_id, _ in self.paper_collections} if unfiled else set()
        )
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

    async def get_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None:
        paper = self.papers.get(paper_id)
        return paper if paper and paper.owner_id == owner_id else None

    async def requeue_owned_paper(
        self, paper_id: str, owner_id: str
    ) -> PaperRecord | None:
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

    async def delete_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None:
        paper = await self.get_owned_paper(paper_id, owner_id)
        if not paper:
            return None
        paper.status = PaperStatus.deleting
        paper.updated_at = now()
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

    async def touch_paper_opened(
        self, paper_id: str, owner_id: str
    ) -> PaperRecord | None:
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
        proposed_parent_id = (
            changes["parent_id"] if "parent_id" in changes else record.parent_id
        )
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

    async def list_jobs(self) -> list[JobRecord]:
        return sorted(self.jobs.values(), key=lambda item: item.created_at, reverse=True)

    async def retry_job(self, job_id: str) -> JobRecord | None:
        job = self.jobs.get(job_id)
        if not job or job.status != JobStatus.failed:
            return None
        job.status = JobStatus.queued
        job.progress = 0
        job.attempts = 0
        job.error_code = None
        job.updated_at = now()
        return job

    async def create_agent_run(
        self, run_id: str, user_id: str, session_id: str, thread_id: str
    ) -> AgentRunRecord:
        record = AgentRunRecord(run_id, user_id, session_id, thread_id)
        self.agent_runs[run_id] = record
        return record

    async def get_owned_agent_run(
        self, run_id: str, user_id: str
    ) -> AgentRunRecord | None:
        record = self.agent_runs.get(run_id)
        return record if record and record.user_id == user_id else None

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
        self, email: str, password: str, role: UserRole, must_change_password: bool = True
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

            removes_active_admin = user.active and user.role == UserRole.admin and (
                changes.get("active") is False or changes.get("role") == UserRole.user
            )
            if removes_active_admin:
                # READ COMMITTED 下等待行锁后用新语句重新计数，不能复用等待前的查询快照。
                active_admin_count = await session.scalar(
                    select(func.count()).select_from(User).where(
                        User.active.is_(True), User.role == UserRole.admin
                    )
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
                statement = statement.where(
                    ~Paper.id.in_(select(paper_collections.c.paper_id))
                )
            result = await session.scalars(statement.order_by(Paper.created_at.desc()))
            return list(result)

    async def get_owned_paper(self, paper_id: str, owner_id: str) -> Paper | None:
        async with get_session_factory()() as session:
            return await session.scalar(
                select(Paper).where(Paper.id == paper_id, Paper.owner_id == owner_id)
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
            session.add(Job(paper_id=paper.id, type="parse_pdf", status=JobStatus.queued))
            await session.commit()
            await session.refresh(paper)
            return paper

    async def delete_owned_paper(self, paper_id: str, owner_id: str) -> Paper | None:
        async with get_session_factory()() as session:
            paper = await session.scalar(
                select(Paper).where(Paper.id == paper_id, Paper.owner_id == owner_id)
            )
            if not paper:
                return None
            paper.status = PaperStatus.deleting
            paper.updated_at = now()
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
                select(func.count()).select_from(User).where(
                    User.active.is_(True), User.role == UserRole.admin
                )
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
                await session.scalars(
                    select(Collection).where(Collection.owner_id == owner_id)
                )
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

    async def list_collections(self, owner_id: str) -> list[Collection]:
        async with get_session_factory()() as session:
            result = await session.scalars(
                select(Collection)
                .where(Collection.owner_id == owner_id)
                .order_by(Collection.name)
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
                await session.scalars(
                    select(Collection).where(Collection.owner_id == owner_id)
                )
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
                    insert(paper_collections).values(
                        paper_id=paper_id, collection_id=collection_id
                    )
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

    async def list_jobs(self) -> list[Job]:
        async with get_session_factory()() as session:
            result = await session.scalars(select(Job).order_by(Job.created_at.desc()).limit(200))
            return list(result)

    async def retry_job(self, job_id: str) -> Job | None:
        async with get_session_factory()() as session:
            job = await session.scalar(
                select(Job).where(Job.id == job_id, Job.status == JobStatus.failed)
            )
            if not job:
                return None
            job.status = JobStatus.queued
            job.progress = 0
            job.attempts = 0
            job.error_code = None
            job.error_message = None
            job.available_at = now()
            job.updated_at = now()
            await session.commit()
            await session.refresh(job)
            return job

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
