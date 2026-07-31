"""ä¸šåŠ¡ä»“åº“æŽ¥å£ä¸Žç¦»çº¿å¯ç”¨çš„å†…å­˜å®žçŽ°ã€‚

ç”Ÿäº§éƒ¨ç½²å¯å°†ç›¸åŒæŽ¥å£æ›¿æ¢ä¸º SQLAlchemy å®žçŽ°ï¼›æƒé™åˆ¤æ–­å§‹ç»ˆå‘ç”Ÿåœ¨ä»“åº“æŸ¥è¯¢æ¡ä»¶ä¸­ï¼Œ
é¿å…å…ˆå–å‡ºä»–äººèµ„æºå†åœ¨è·¯ç”±å±‚è¿‡æ»¤ã€‚
"""

from __future__ import annotations

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
    Tag,
    User,
    UserRole,
    UserSession,
    paper_collections,
    paper_tags,
)
from .security import digest_session_token, hash_password, verify_password


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class UserRecord:
    id: str
    email: str
    password_hash: str
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
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class TagRecord:
    id: str
    owner_id: str
    name: str
    color: str | None = None
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


class Repository(Protocol):
    async def find_user_by_email(self, email: str) -> UserRecord | None: ...
    async def get_user(self, user_id: str) -> UserRecord | None: ...
    async def create_user(
        self, email: str, password: str, role: UserRole, must_change_password: bool = True
    ) -> UserRecord: ...
    async def list_users(self) -> list[UserRecord]: ...
    async def update_user(self, user_id: str, **changes: object) -> UserRecord | None: ...
    async def create_session(self, user_id: str, token: str, ttl_seconds: int) -> None: ...
    async def user_for_session(self, token: str) -> UserRecord | None: ...
    async def delete_session(self, token: str) -> None: ...
    async def set_password(self, user_id: str, password: str) -> None: ...
    async def create_paper(self, paper: PaperRecord) -> PaperRecord: ...
    async def list_papers(self, owner_id: str) -> list[PaperRecord]: ...
    async def get_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None: ...
    async def update_owned_paper(
        self, paper_id: str, owner_id: str, **changes: object
    ) -> PaperRecord | None: ...
    async def delete_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None: ...
    async def touch_paper_opened(self, paper_id: str, owner_id: str) -> PaperRecord | None: ...
    async def set_papers_archived(
        self, paper_ids: list[str], owner_id: str, archived: bool
    ) -> list[str] | None: ...
    async def list_collection_memberships(self, owner_id: str) -> dict[str, list[str]]: ...
    async def list_tag_memberships(self, owner_id: str) -> dict[str, list[str]]: ...


class MemoryRepository:
    """Demo/Test ä»“åº“ï¼›å•è¿›ç¨‹ä½¿ç”¨ï¼Œä¸ä½œä¸ºç”Ÿäº§æŒä¹…åŒ–æ–¹æ¡ˆã€‚"""

    def __init__(self, session_secret: str) -> None:
        self.users: dict[str, UserRecord] = {}
        self.papers: dict[str, PaperRecord] = {}
        self.sessions: dict[str, tuple[str, datetime]] = {}
        self.collections: dict[str, CollectionRecord] = {}
        self.tags: dict[str, TagRecord] = {}
        self.paper_collections: set[tuple[str, str]] = set()
        self.paper_tags: set[tuple[str, str]] = set()
        self.jobs: dict[str, JobRecord] = {}
        self.agent_runs: dict[str, AgentRunRecord] = {}
        self.session_secret = session_secret

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
            raise ValueError("é‚®ç®±å·²å­˜åœ¨")
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
        for key in ("active", "role", "must_change_password"):
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

    async def set_password(self, user_id: str, password: str) -> None:
        user = self.users[user_id]
        user.password_hash = hash_password(password)
        user.must_change_password = False

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
            raise ValueError(f"æ–‡çŒ®å·²å­˜åœ¨:{duplicate.id}")
        self.papers[paper.id] = paper
        job = JobRecord(id=str(uuid.uuid4()), paper_id=paper.id, type="parse_pdf")
        self.jobs[job.id] = job
        return paper

    async def list_papers(self, owner_id: str) -> list[PaperRecord]:
        return sorted(
            (
                paper
                for paper in self.papers.values()
                if paper.owner_id == owner_id and paper.status != PaperStatus.deleting
            ),
            key=lambda paper: paper.created_at,
            reverse=True,
        )

    async def get_owned_paper(self, paper_id: str, owner_id: str) -> PaperRecord | None:
        paper = self.papers.get(paper_id)
        return paper if paper and paper.owner_id == owner_id else None

    async def update_owned_paper(
        self, paper_id: str, owner_id: str, **changes: object
    ) -> PaperRecord | None:
        paper = await self.get_owned_paper(paper_id, owner_id)
        if not paper:
            return None
        for key in ("title", "authors", "year", "abstract", "doi", "status"):
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
        self, owner_id: str, name: str, description: str | None
    ) -> CollectionRecord:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("é›†åˆåç§°ä¸èƒ½ä¸ºç©º")
        if any(
            item.owner_id == owner_id and item.name.casefold() == normalized_name.casefold()
            for item in self.collections.values()
        ):
            raise ValueError("é›†åˆåç§°å·²å­˜åœ¨")
        record = CollectionRecord(str(uuid.uuid4()), owner_id, normalized_name, description)
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

    async def update_collection(
        self, collection_id: str, owner_id: str, **changes: object
    ) -> CollectionRecord | None:
        record = self.collections.get(collection_id)
        if not record or record.owner_id != owner_id:
            return None
        if "name" in changes:
            normalized_name = str(changes["name"]).strip()
            if not normalized_name:
                raise ValueError("é›†åˆåç§°ä¸èƒ½ä¸ºç©º")
            if any(
                item.id != record.id
                and item.owner_id == owner_id
                and item.name.casefold() == normalized_name.casefold()
                for item in self.collections.values()
            ):
                raise ValueError("é›†åˆåç§°å·²å­˜åœ¨")
            changes["name"] = normalized_name
        for key in ("name", "description"):
            if key in changes:
                setattr(record, key, changes[key])
        record.updated_at = now()
        return record

    async def delete_collection(self, collection_id: str, owner_id: str) -> bool:
        record = self.collections.get(collection_id)
        if not record or record.owner_id != owner_id:
            return False
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

    async def create_tag(self, owner_id: str, name: str, color: str | None) -> TagRecord:
        normalized_name = name.strip()
        if not normalized_name:
            raise Valuß½y¶‰žËkºwµç@€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€™½È­•ä¥¸€ ‰Ñ¥Ñ±”ˆ°€‰…ÕÑ¡½ÉÌˆ°€‰å•…Èˆ°€‰…‰ÍÑÉ…Ðˆ°€‰‘½¤ˆ°€‰ÍÑ…ÑÕÌˆ¤è(€€€€€€€€€€€€€€€¥˜­•ä¥¸¡…¹•Ì…¹¡…¹•Ím­•åt¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€€€€€Í•Ñ…ÑÑÈ¡Á…Á•È°­•ä°¡…¹•Ím­•åt¤(€€€€€€€€€€€Á…Á•È¹ÕÁ‘…Ñ•‘}…Ð€ô¹½Ü ¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É•™É•Í ¡Á…Á•È¤(€€€€€€€€€€€É•ÑÕÉ¸Á…Á•È((€€€…Íå¹Œ‘•˜‘•±•Ñ•}½Ý¹•‘}Á…Á•È¡Í•±˜°Á…Á•É}¥èÍÑÈ°½Ý¹•É}¥èÍÑÈ¤€´øA…Á•Èð9½¹”è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€Á…Á•È€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡A…Á•È¤¹Ý¡•É”¡A…Á•È¹¥€ôôÁ…Á•É}¥°A…Á•È¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½ÐÁ…Á•Èè(€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€Á…Á•È¹ÍÑ…ÑÕÌ€ôA…Á•ÉMÑ…ÑÕÌ¹‘•±•Ñ¥¹œ(€€€€€€€€€€€Á…Á•È¹ÕÁ‘…Ñ•‘}…Ð€ô¹½Ü ¤(€€€€€€€€€€€…Ñ¥Ù•}‘•±•Ñ”€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡)½ˆ¤¹Ý¡•É” (€€€€€€€€€€€€€€€€€€€)½ˆ¹Á…Á•É}¥€ôôÁ…Á•È¹¥°(€€€€€€€€€€€€€€€€€€€)½ˆ¹ÑåÁ”€ôô€‰‘•±•Ñ•}Á…Á•Èˆ°(€€€€€€€€€€€€€€€€€€€)½ˆ¹ÍÑ…ÑÕÌ¹¥¹|¡m)½‰MÑ…ÑÕÌ¹ÅÕ•Õ•°)½‰MÑ…ÑÕÌ¹ÉÕ¹¹¥¹t¤°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½Ð…Ñ¥Ù•}‘•±•Ñ”è(€€€€€€€€€€€€€€€Í•ÍÍ¥½¸¹…‘¡)½ˆ¡Á…Á•É}¥õÁ…Á•È¹¥°ÑåÁ”ô‰‘•±•Ñ•}Á…Á•Èˆ¤¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É•™É•Í ¡Á…Á•È¤(€€€€€€€€€€€É•ÑÕÉ¸Á…Á•È((€€€…Íå¹Œ‘•˜Ñ½Õ¡}Á…Á•É}½Á•¹•¡Í•±˜°Á…Á•É}¥èÍÑÈ°½Ý¹•É}¥èÍÑÈ¤€´øA…Á•Èð9½¹”è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€Á…Á•È€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡A…Á•È¤¹Ý¡•É”¡A…Á•È¹¥€ôôÁ…Á•É}¥°A…Á•È¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½ÐÁ…Á•Èè(€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€Á…Á•È¹±…ÍÑ}½Á•¹•‘}…Ð€ô¹½Ü ¤(€€€€€€€€€€€Á…Á•È¹ÕÁ‘…Ñ•‘}…Ð€ô¹½Ü ¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É•™É•Í ¡Á…Á•È¤(€€€€€€€€€€€É•ÑÕÉ¸Á…Á•È((€€€…Íå¹Œ‘•˜Í•Ñ}Á…Á•ÉÍ}…É¡¥Ù• (€€€€€€€Í•±˜°Á…Á•É}¥‘Ìè±¥ÍÑmÍÑÉt°½Ý¹•É}¥èÍÑÈ°…É¡¥Ù•è‰½½°(€€€€¤€´ø±¥ÍÑmÍÑÉtð9½¹”è(€€€€€€€Õ¹¥ÅÕ•}¥‘Ì€ô±¥ÍÐ¡‘¥Ð¹™É½µ­•åÌ¡Á…Á•É}¥‘Ì¤¤(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€½Ý¹•‘}¥‘Ì€ô±¥ÍÐ (€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…ÉÌ (€€€€€€€€€€€€€€€€€€€Í•±•Ð¡A…Á•È¹¥¤¹Ý¡•É” (€€€€€€€€€€€€€€€€€€€€€€€A…Á•È¹½Ý¹•É}¥€ôô½Ý¹•É}¥°(€€€€€€€€€€€€€€€€€€€€€€€A…Á•È¹¥¹¥¹|¡Õ¹¥ÅÕ•}¥‘Ì¤°(€€€€€€€€€€€€€€€€€€€€€€€A…Á•È¹ÍÑ…ÑÕÌ€„ôA…Á•ÉMÑ…ÑÕÌ¹‘•±•Ñ¥¹œ°(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜Í•Ð¡½Ý¹•‘}¥‘Ì¤€„ôÍ•Ð¡Õ¹¥ÅÕ•}¥‘Ì¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€Ñ¥µ•ÍÑ…µÀ€ô¹½Ü ¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€ÕÁ‘…Ñ”¡A…Á•È¤(€€€€€€€€€€€€€€€€¹Ý¡•É”¡A…Á•È¹½Ý¹•É}¥€ôô½Ý¹•É}¥°A…Á•È¹¥¹¥¹|¡Õ¹¥ÅÕ•}¥‘Ì¤¤(€€€€€€€€€€€€€€€€¹Ù…±Õ•Ì (€€€€€€€€€€€€€€€€€€€…É¡¥Ù•‘}…ÐõÑ¥µ•ÍÑ…µÀ¥˜…É¡¥Ù••±Í”9½¹”°(€€€€€€€€€€€€€€€€€€€ÕÁ‘…Ñ•‘}…ÐõÑ¥µ•ÍÑ…µÀ°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€É•ÑÕÉ¸Õ¹¥ÅÕ•}¥‘Ì((€€€…Íå¹Œ‘•˜½Õ¹Ñ}…Ñ¥Ù•}…‘µ¥¹Ì¡Í•±˜¤€´ø¥¹Ðè(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€Ù…±Õ”€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡™Õ¹Œ¹½Õ¹Ð ¤¤¹Í•±•Ñ}™É½´¡UÍ•È¤¹Ý¡•É” (€€€€€€€€€€€€€€€€€€€UÍ•È¹…Ñ¥Ù”¹¥Í|¡QÉÕ”¤°UÍ•È¹É½±”€ôôUÍ•ÉI½±”¹…‘µ¥¸(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€€€€€É•ÑÕÉ¸¥¹Ð¡Ù…±Õ”½È€À¤((€€€…Íå¹Œ‘•˜É•…Ñ•}½±±•Ñ¥½¸ (€€€€€€€Í•±˜°½Ý¹•É}¥èÍÑÈ°¹…µ”èÍÑÈ°‘•ÍÉ¥ÁÑ¥½¸èÍÑÈð9½¹”(€€€€¤€´ø½±±•Ñ¥½¸è(€€€€€€€¹½Éµ…±¥é•‘}¹…µ”€ô¹…µ”¹ÍÑÉ¥À ¤(€€€€€€€¥˜¹½Ð¹½Éµ…±¥é•‘}¹…µ”è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‹¦n–B#–B7žžÃ’â7¢÷’âëž¦èˆ¤(€€€€€€€É•½É€ô½±±•Ñ¥½¸¡½Ý¹•É}¥õ½Ý¹•É}¥°¹…µ”õ¹½Éµ…±¥é•‘}¹…µ”°‘•ÍÉ¥ÁÑ¥½¸õ‘•ÍÉ¥ÁÑ¥½¸¤(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€Í•ÍÍ¥½¸¹…‘¡É•½É¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€•á•ÁÐ%¹Ñ•É¥ÑåÉÉ½È…Ì•áŒè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É½±±‰…¬ ¤(€€€€€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‹¦n–B#–B7žžÃ–ÞË–¶c–r ˆ¤™É½´•áŒ(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É•™É•Í ¡É•½É¤(€€€€€€€€€€€É•ÑÕÉ¸É•½É((€€€…Íå¹Œ‘•˜±¥ÍÑ}½±±•Ñ¥½¹Ì¡Í•±˜°½Ý¹•É}¥èÍÑÈ¤€´ø±¥ÍÑm½±±•Ñ¥½¹tè(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É•ÍÕ±Ð€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…ÉÌ (€€€€€€€€€€€€€€€Í•±•Ð¡½±±•Ñ¥½¸¤(€€€€€€€€€€€€€€€€¹Ý¡•É”¡½±±•Ñ¥½¸¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤(€€€€€€€€€€€€€€€€¹½É‘•É}‰ä¡½±±•Ñ¥½¸¹¹…µ”¤(€€€€€€€€€€€€¤(€€€€€€€€€€€É•ÑÕÉ¸±¥ÍÐ¡É•ÍÕ±Ð¤((€€€…Íå¹Œ‘•˜±¥ÍÑ}½±±•Ñ¥½¹}µ•µ‰•ÉÍ¡¥ÁÌ¡Í•±˜°½Ý¹•É}¥èÍÑÈ¤€´ø‘¥ÑmÍÑÈ°±¥ÍÑmÍÑÉutè(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É½ÝÌ€ô€ (€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€€€€€Í•±•Ð¡½±±•Ñ¥½¸¹¥°Á…Á•É}½±±•Ñ¥½¹Ì¹Œ¹Á…Á•É}¥¤(€€€€€€€€€€€€€€€€€€€€¹©½¥¸ (€€€€€€€€€€€€€€€€€€€€€€€Á…Á•É}½±±•Ñ¥½¹Ì°(€€€€€€€€€€€€€€€€€€€€€€€Á…Á•É}½±±•Ñ¥½¹Ì¹Œ¹½±±•Ñ¥½¹}¥€ôô½±±•Ñ¥½¸¹¥°(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€€¹Ý¡•É”¡½±±•Ñ¥½¸¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤(€€€€€€€€€€€€€€€€€€€€¹½É‘•É}‰ä¡½±±•Ñ¥½¸¹¥°Á…Á•É}½±±•Ñ¥½¹Ì¹Œ¹Á…Á•É}¥¤(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤¹…±° ¤(€€€€€€€€€€€µ•µ‰•ÉÍ¡¥ÁÌè‘¥ÑmÍÑÈ°±¥ÍÑmÍÑÉut€ôíô(€€€€€€€€€€€™½È½±±•Ñ¥½¹}¥°Á…Á•É}¥¥¸É½ÝÌè(€€€€€€€€€€€€€€€µ•µ‰•ÉÍ¡¥ÁÌ¹Í•Ñ‘•™…Õ±Ð¡½±±•Ñ¥½¹}¥°mt¤¹…ÁÁ•¹¡Á…Á•É}¥¤(€€€€€€€€€€€É•ÑÕÉ¸µ•µ‰•ÉÍ¡¥ÁÌ((€€€…Íå¹Œ‘•˜ÕÁ‘…Ñ•}½±±•Ñ¥½¸ (€€€€€€€Í•±˜°½±±•Ñ¥½¹}¥èÍÑÈ°½Ý¹•É}¥èÍÑÈ°€¨©¡…¹•Ìè½‰©•Ð(€€€€¤€´ø½±±•Ñ¥½¸ð9½¹”è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É•½É€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡½±±•Ñ¥½¸¤¹Ý¡•É” (€€€€€€€€€€€€€€€€€€€½±±•Ñ¥½¸¹¥€ôô½±±•Ñ¥½¹}¥°½±±•Ñ¥½¸¹½Ý¹•É}¥€ôô½Ý¹•É}¥(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½ÐÉ•½Éè(€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€¥˜€‰¹…µ”ˆ¥¸¡…¹•Ìè(€€€€€€€€€€€€€€€¹½Éµ…±¥é•‘}¹…µ”€ôÍÑÈ¡¡…¹•Íl‰¹…µ”‰t¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½Ð¹½Éµ…±¥é•‘}¹…µ”è(€€€€€€€€€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‹¦n–B#–B7žžÃ’â7¢÷’âëž¦èˆ¤(€€€€€€€€€€€€€€€¡…¹•Íl‰¹…µ”‰t€ô¹½Éµ…±¥é•‘}¹…µ”(€€€€€€€€€€€™½È­•ä¥¸€ ‰¹…µ”ˆ°€‰‘•ÍÉ¥ÁÑ¥½¸ˆ¤è(€€€€€€€€€€€€€€€¥˜­•ä¥¸¡…¹•Ìè(€€€€€€€€€€€€€€€€€€€Í•Ñ…ÑÑÈ¡É•½É°­•ä°¡…¹•Ím­•åt¤(€€€€€€€€€€€É•½É¹ÕÁ‘…Ñ•‘}…Ð€ô¹½Ü ¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€•á•ÁÐ%¹Ñ•É¥ÑåÉÉ½È…Ì•áŒè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É½±±‰…¬ ¤(€€€€€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‹¦n–B#–B7žžÃ–ÞË–¶c–r ˆ¤™É½´•áŒ(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É•™É•Í ¡É•½É¤(€€€€€€€€€€€É•ÑÕÉ¸É•½É((€€€…Íå¹Œ‘•˜‘•±•Ñ•}½±±•Ñ¥½¸¡Í•±˜°½±±•Ñ¥½¹}¥èÍÑÈ°½Ý¹•É}¥èÍÑÈ¤€´ø‰½½°è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É•ÍÕ±Ð€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€‘•±•Ñ”¡½±±•Ñ¥½¸¤¹Ý¡•É” (€€€€€€€€€€€€€€€€€€€½±±•Ñ¥½¸¹¥€ôô½±±•Ñ¥½¹}¥°½±±•Ñ¥½¸¹½Ý¹•É}¥€ôô½Ý¹•É}¥(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€É•ÑÕÉ¸‰½½°¡É•ÍÕ±Ð¹É½Ý½Õ¹Ð¤((€€€…Íå¹Œ‘•˜Í•Ñ}Á…Á•É}½±±•Ñ¥½¸ (€€€€€€€Í•±˜°½±±•Ñ¥½¹}¥èÍÑÈ°Á…Á•É}¥èÍÑÈ°½Ý¹•É}¥èÍÑÈ°…ÍÍ¥¹•è‰½½°(€€€€¤€´ø‰½½°è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€½±±•Ñ¥½¸€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡½±±•Ñ¥½¸¹¥¤¹Ý¡•É” (€€€€€€€€€€€€€€€€€€€½±±•Ñ¥½¸¹¥€ôô½±±•Ñ¥½¹}¥°½±±•Ñ¥½¸¹½Ý¹•É}¥€ôô½Ý¹•É}¥(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€€€€€Á…Á•È€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡A…Á•È¹¥¤¹Ý¡•É”¡A…Á•È¹¥€ôôÁ…Á•É}¥°A…Á•È¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½Ð½±±•Ñ¥½¸½È¹½ÐÁ…Á•Èè(€€€€€€€€€€€€€€€É•ÑÕÉ¸…±Í”(€€€€€€€€€€€•á¥ÍÑÌ€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡Á…Á•É}½±±•Ñ¥½¹Ì¹Œ¹Á…Á•É}¥¤¹Ý¡•É” (€€€€€€€€€€€€€€€€€€€Á…Á•É}½±±•Ñ¥½¹Ì¹Œ¹Á…Á•É}¥€ôôÁ…Á•É}¥°(€€€€€€€€€€€€€€€€€€€Á…Á•É}½±±•Ñ¥½¹Ì¹Œ¹½±±•Ñ¥½¹}¥€ôô½±±•Ñ¥½¹}¥°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜…ÍÍ¥¹•…¹¹½Ð•á¥ÍÑÌè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€€€€€¥¹Í•ÉÐ¡Á…Á•É}½±±•Ñ¥½¹Ì¤¹Ù…±Õ•Ì (€€€€€€€€€€€€€€€€€€€€€€€Á…Á•É}¥õÁ…Á•É}¥°½±±•Ñ¥½¹}¥õ½±±•Ñ¥½¹}¥(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€•±¥˜¹½Ð…ÍÍ¥¹•…¹•á¥ÍÑÌè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€€€€€‘•±•Ñ”¡Á…Á•É}½±±•Ñ¥½¹Ì¤¹Ý¡•É” (€€€€€€€€€€€€€€€€€€€€€€€Á…Á•É}½±±•Ñ¥½¹Ì¹Œ¹Á…Á•É}¥€ôôÁ…Á•É}¥°(€€€€€€€€€€€€€€€€€€€€€€€Á…Á•É}½±±•Ñ¥½¹Ì¹Œ¹½±±•Ñ¥½¹}¥€ôô½±±•Ñ¥½¹}¥°(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”((€€€…Íå¹Œ‘•˜É•…Ñ•}Ñ…œ¡Í•±˜°½Ý¹•É}¥èÍÑÈ°¹…µ”èÍÑÈ°½±½ÈèÍÑÈð9½¹”¤€´øQ…œè(€€€€€€€¹½Éµ…±¥é•‘}¹…µ”€ô¹…µ”¹ÍÑÉ¥À ¤(€€€€€€€¥˜¹½Ð¹½Éµ…±¥é•‘}¹…µ”è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‹š‚ž¶û–B7žžÃ’â7¢÷’âëž¦èˆ¤(€€€€€€€É•½É€ôQ…œ¡½Ý¹•É}¥õ½Ý¹•É}¥°¹…µ”õ¹½Éµ…±¥é•‘}¹…µ”°½±½Èõ½±½È¤(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€Í•ÍÍ¥½¸¹…‘¡É•½É¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€•á•ÁÐ%¹Ñ•É¥ÑåÉÉ½È…Ì•áŒè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É½±±‰…¬ ¤(€€€€€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‹š‚ž¶û–B7žžÃ–ÞË–¶c–r ˆ¤™É½´•áŒ(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É•™É•Í ¡É•½É¤(€€€€€€€€€€€É•ÑÕÉ¸É•½É((€€€…Íå¹Œ‘•˜±¥ÍÑ}Ñ…Ì¡Í•±˜°½Ý¹•É}¥èÍÑÈ¤€´ø±¥ÍÑmQ…tè(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É•ÍÕ±Ð€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…ÉÌ (€€€€€€€€€€€€€€€Í•±•Ð¡Q…œ¤¹Ý¡•É”¡Q…œ¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤¹½É‘•É}‰ä¡Q…œ¹¹…µ”¤(€€€€€€€€€€€€¤(€€€€€€€€€€€É•ÑÕÉ¸±¥ÍÐ¡É•ÍÕ±Ð¤((€€€…Íå¹Œ‘•˜±¥ÍÑ}Ñ…}µ•µ‰•ÉÍ¡¥ÁÌ¡Í•±˜°½Ý¹•É}¥èÍÑÈ¤€´ø‘¥ÑmÍÑÈ°±¥ÍÑmÍÑÉutè(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É½ÝÌ€ô€ (€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€€€€€Í•±•Ð¡Q…œ¹¥°Á…Á•É}Ñ…Ì¹Œ¹Á…Á•É}¥¤(€€€€€€€€€€€€€€€€€€€€¹©½¥¸¡Á…Á•É}Ñ…Ì°Á…Á•É}Ñ…Ì¹Œ¹Ñ…}¥€ôôQ…œ¹¥¤(€€€€€€€€€€€€€€€€€€€€¹Ý¡•É”¡Q…œ¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤(€€€€€€€€€€€€€€€€€€€€¹½É‘•É}‰ä¡Q…œ¹¥°Á…Á•É}Ñ…Ì¹Œ¹Á…Á•É}¥¤(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤¹…±° ¤(€€€€€€€€€€€µ•µ‰•ÉÍ¡¥ÁÌè‘¥ÑmÍÑÈ°±¥ÍÑmÍÑÉut€ôíô(€€€€€€€€€€€™½ÈÑ…}¥°Á…Á•É}¥¥¸É½ÝÌè(€€€€€€€€€€€€€€€µ•µ‰•ÉÍ¡¥ÁÌ¹Í•Ñ‘•™…Õ±Ð¡Ñ…}¥°mt¤¹…ÁÁ•¹¡Á…Á•É}¥¤(€€€€€€€€€€€É•ÑÕÉ¸µ•µ‰•ÉÍ¡¥ÁÌ((€€€…Íå¹Œ‘•˜ÕÁ‘…Ñ•}Ñ…œ (€€€€€€€Í•±˜°Ñ…}¥èÍÑÈ°½Ý¹•É}¥èÍÑÈ°€¨©¡…¹•Ìè½‰©•Ð(€€€€¤€´øQ…œð9½¹”è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É•½É€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡Q…œ¤¹Ý¡•É”¡Q…œ¹¥€ôôÑ…}¥°Q…œ¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½ÐÉ•½Éè(€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€¥˜€‰¹…µ”ˆ¥¸¡…¹•Ìè(€€€€€€€€€€€€€€€¹½Éµ…±¥é•‘}¹…µ”€ôÍÑÈ¡¡…¹•Íl‰¹…µ”‰t¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½Ð¹½Éµ…±¥é•‘}¹…µ”è(€€€€€€€€€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‹š‚ž¶û–B7žžÃ’â7¢÷’âëž¦èˆ¤(€€€€€€€€€€€€€€€¡…¹•Íl‰¹…µ”‰t€ô¹½Éµ…±¥é•‘}¹…µ”(€€€€€€€€€€€™½È­•ä¥¸€ ‰¹…µ”ˆ°€‰½±½Èˆ¤è(€€€€€€€€€€€€€€€¥˜­•ä¥¸¡…¹•Ìè(€€€€€€€€€€€€€€€€€€€Í•Ñ…ÑÑÈ¡É•½É°­•ä°¡…¹•Ím­•åt¤(€€€€€€€€€€€É•½É¹ÕÁ‘…Ñ•‘}…Ð€ô¹½Ü ¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€•á•ÁÐ%¹Ñ•É¥ÑåÉÉ½È…Ì•áŒè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É½±±‰…¬ ¤(€€€€€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‹š‚ž¶û–B7žžÃ–ÞË–¶c–r ˆ¤™É½´•áŒ(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É•™É•Í ¡É•½É¤(€€€€€€€€€€€É•ÑÕÉ¸É•½É((€€€…Íå¹Œ‘•˜‘•±•Ñ•}Ñ…œ¡Í•±˜°Ñ…}¥èÍÑÈ°½Ý¹•É}¥èÍÑÈ¤€´ø‰½½°è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É•ÍÕ±Ð€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€‘•±•Ñ”¡Q…œ¤¹Ý¡•É”¡Q…œ¹¥€ôôÑ…}¥°Q…œ¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤(€€€€€€€€€€€€¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€É•ÑÕÉ¸‰½½°¡É•ÍÕ±Ð¹É½Ý½Õ¹Ð¤((€€€…Íå¹Œ‘•˜Í•Ñ}Á…Á•É}Ñ…œ (€€€€€€€Í•±˜°Ñ…}¥èÍÑÈ°Á…Á•É}¥èÍÑÈ°½Ý¹•É}¥èÍÑÈ°…ÍÍ¥¹•è‰½½°(€€€€¤€´ø‰½½°è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€Ñ…œ€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡Q…œ¹¥¤¹Ý¡•É”¡Q…œ¹¥€ôôÑ…}¥°Q…œ¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤(€€€€€€€€€€€€¤(€€€€€€€€€€€Á…Á•È€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡A…Á•È¹¥¤¹Ý¡•É”¡A…Á•È¹¥€ôôÁ…Á•É}¥°A…Á•È¹½Ý¹•É}¥€ôô½Ý¹•É}¥¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½ÐÑ…œ½È¹½ÐÁ…Á•Èè(€€€€€€€€€€€€€€€É•ÑÕÉ¸…±Í”(€€€€€€€€€€€•á¥ÍÑÌ€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡Á…Á•É}Ñ…Ì¹Œ¹Á…Á•É}¥¤¹Ý¡•É” (€€€€€€€€€€€€€€€€€€€Á…Á•É}Ñ…Ì¹Œ¹Á…Á•É}¥€ôôÁ…Á•É}¥°Á…Á•É}Ñ…Ì¹Œ¹Ñ…}¥€ôôÑ…}¥(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜…ÍÍ¥¹•…¹¹½Ð•á¥ÍÑÌè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹•á•ÕÑ”¡¥¹Í•ÉÐ¡Á…Á•É}Ñ…Ì¤¹Ù…±Õ•Ì¡Á…Á•É}¥õÁ…Á•É}¥°Ñ…}¥õÑ…}¥¤¤(€€€€€€€€€€€•±¥˜¹½Ð…ÍÍ¥¹•…¹•á¥ÍÑÌè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€€€€€‘•±•Ñ”¡Á…Á•É}Ñ…Ì¤¹Ý¡•É” (€€€€€€€€€€€€€€€€€€€€€€€Á…Á•É}Ñ…Ì¹Œ¹Á…Á•É}¥€ôôÁ…Á•É}¥°Á…Á•É}Ñ…Ì¹Œ¹Ñ…}¥€ôôÑ…}¥(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”((€€€…Íå¹Œ‘•˜±¥ÍÑ}©½‰Ì¡Í•±˜¤€´ø±¥ÍÑm)½‰tè(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É•ÍÕ±Ð€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…ÉÌ¡Í•±•Ð¡)½ˆ¤¹½É‘•É}‰ä¡)½ˆ¹É•…Ñ•‘}…Ð¹‘•ÍŒ ¤¤¹±¥µ¥Ð ÈÀÀ¤¤(€€€€€€€€€€€É•ÑÕÉ¸±¥ÍÐ¡É•ÍÕ±Ð¤((€€€…Íå¹Œ‘•˜É•ÑÉå}©½ˆ¡Í•±˜°©½‰}¥èÍÑÈ¤€´ø)½ˆð9½¹”è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€©½ˆ€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡)½ˆ¤¹Ý¡•É”¡)½ˆ¹¥€ôô©½‰}¥°)½ˆ¹ÍÑ…ÑÕÌ€ôô)½‰MÑ…ÑÕÌ¹™…¥±•¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½Ð©½ˆè(€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€©½ˆ¹ÍÑ…ÑÕÌ€ô)½‰MÑ…ÑÕÌ¹ÅÕ•Õ•(€€€€€€€€€€€©½ˆ¹ÁÉ½É•ÍÌ€ô€À(€€€€€€€€€€€©½ˆ¹…ÑÑ•µÁÑÌ€ô€À(€€€€€€€€€€€©½ˆ¹•ÉÉ½É}½‘”€ô9½¹”(€€€€€€€€€€€©½ˆ¹•ÉÉ½É}µ•ÍÍ…”€ô9½¹”(€€€€€€€€€€€©½ˆ¹…Ù…¥±…‰±•}…Ð€ô¹½Ü ¤(€€€€€€€€€€€©½ˆ¹ÕÁ‘…Ñ•‘}…Ð€ô¹½Ü ¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É•™É•Í ¡©½ˆ¤(€€€€€€€€€€€É•ÑÕÉ¸©½ˆ((€€€…Íå¹Œ‘•˜É•…Ñ•}…•¹Ñ}ÉÕ¸ (€€€€€€€Í•±˜°ÉÕ¹}¥èÍÑÈ°ÕÍ•É}¥èÍÑÈ°Í•ÍÍ¥½¹}¥èÍÑÈ°Ñ¡É•…‘}¥èÍÑÈ(€€€€¤€´ø•¹ÑIÕ¸è(€€€€€€€É•½É€ô•¹ÑIÕ¸ (€€€€€€€€€€€¥õÉÕ¹}¥°(€€€€€€€€€€€ÕÍ•É}¥õÕÍ•É}¥°(€€€€€€€€€€€Í•ÍÍ¥½¹}¥õÍ•ÍÍ¥½¹}¥°(€€€€€€€€€€€Ñ¡É•…‘}¥õÑ¡É•…‘}¥°(€€€€€€€€€€€ÍÑ…ÑÕÌô‰Á•¹‘¥¹œˆ°(€€€€€€€€¤(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€Í•ÍÍ¥½¸¹…‘¡É•½É¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€•á•ÁÐ%¹Ñ•É¥ÑåÉÉ½È…Ì•áŒè(€€€€€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É½±±‰…¬ ¤(€€€€€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰•¹ÐIÕ¸ƒ–ÞË–¶c–r ˆ¤™É½´•áŒ(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É•™É•Í ¡É•½É¤(€€€€€€€€€€€É•ÑÕÉ¸É•½É((€€€…Íå¹Œ‘•˜•Ñ}½Ý¹•‘}…•¹Ñ}ÉÕ¸¡Í•±˜°ÉÕ¹}¥èÍÑÈ°ÕÍ•É}¥èÍÑÈ¤€´ø•¹ÑIÕ¸ð9½¹”è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É•ÑÕÉ¸…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡•¹ÑIÕ¸¤¹Ý¡•É”¡•¹ÑIÕ¸¹¥€ôôÉÕ¹}¥°•¹ÑIÕ¸¹ÕÍ•É}¥€ôôÕÍ•É}¥¤(€€€€€€€€€€€€¤((€€€…Íå¹Œ‘•˜ÕÁ‘…Ñ•}½Ý¹•‘}…•¹Ñ}ÉÕ¸ (€€€€€€€Í•±˜°ÉÕ¹}¥èÍÑÈ°ÕÍ•É}¥èÍÑÈ°€¨©¡…¹•Ìè½‰©•Ð(€€€€¤€´ø•¹ÑIÕ¸ð9½¹”è(€€€€€€€…Íå¹ŒÝ¥Ñ •Ñ}Í•ÍÍ¥½¹}™…Ñ½Éä ¤ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É•½É€ô…Ý…¥ÐÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡•¹ÑIÕ¸¤¹Ý¡•É”¡•¹ÑIÕ¸¹¥€ôôÉÕ¹}¥°•¹ÑIÕ¸¹ÕÍ•É}¥€ôôÕÍ•É}¥¤(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½ÐÉ•½Éè(€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€™½È­•ä¥¸€ (€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆ°(€€€€€€€€€€€€€€€€‰Ñ½½±}ÍÑ•ÁÌˆ°(€€€€€€€€€€€€€€€€‰‘ÕÉ…Ñ¥½¹}µÌˆ°(€€€€€€€€€€€€€€€€‰Ñ½­•¹}ÕÍ…”ˆ°(€€€€€€€€€€€€€€€€‰É•ÍÕ±Ñ}ÍÕµµ…Éäˆ°(€€€€€€€€€€€€€€€€‰Á•¹‘¥¹}…Ñ¥½¸ˆ°(€€€€€€€€€€€€€€€€‰•ÉÉ½É}½‘”ˆ°(€€€€€€€€€€€€¤è(€€€€€€€€€€€€€€€¥˜­•ä¥¸¡…¹•Ìè(€€€€€€€€€€€€€€€€€€€Í•Ñ…ÑÑÈ¡É•½É°­•ä°¡…¹•Ím­•åt¤(€€€€€€€€€€€É•½É¹ÕÁ‘…Ñ•‘}…Ð€ô¹½Ü ¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹½µµ¥Ð ¤(€€€€€€€€€€€…Ý…¥ÐÍ•ÍÍ¥½¸¹É•™É•Í ¡É•½É¤(€€€€€€€€€€€É•ÑÕÉ¸É•½É(