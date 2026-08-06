import asyncio
import hashlib
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from paperleaf_api import worker
from paperleaf_api.db import Base
from paperleaf_api.model_runtime import ModelRuntimeError
from paperleaf_api.models import (
    Job,
    JobStatus,
    Paper,
    PaperPage,
    PaperStatus,
    PaperTranslation,
    PaperTranslationPage,
    User,
)


class AvailableRouter:
    def has_provider(self, purpose: str) -> bool:
        return purpose == "translation"


def test_chunked_translation_stops_after_fencing_token_is_lost() -> None:
    class ChunkRouter:
        def __init__(self) -> None:
            self.calls = 0

        def has_provider(self, purpose: str) -> bool:
            return purpose == "translation"

        async def execute(self, purpose: str, operation):
            self.calls += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="译文"))]
            )

    async def scenario() -> None:
        router = ChunkRouter()
        guard_results = iter([True, False])

        async def lease_guard() -> bool:
            return next(guard_results)

        with pytest.raises(worker.JobLeaseLostError):
            await worker.translate_page_text(
                "A" * 9000,
                "zh-CN",
                router=router,
                lease_guard=lease_guard,
            )
        assert router.calls == 1

    asyncio.run(scenario())


def test_translation_page_cost_limit_rejects_oversized_text() -> None:
    with pytest.raises(worker.TranslationInputLimitError):
        worker.split_translation_text("A" * (worker.MAX_TRANSLATION_PAGE_CHARS + 1))


async def _seed_translation(sessions, *, page_status: str = "queued", attempts: int = 0):
    source_text = "Source page text with formula E = mc^2 and citation [7]."
    async with sessions() as session:
        session.add(
            User(
                id="user-1",
                email="reader@example.com",
                password_hash="not-used",
                must_change_password=False,
            )
        )
        session.add(
            Paper(
                id="paper-1",
                owner_id="user-1",
                title="Paper",
                authors=[],
                filename="paper.pdf",
                storage_key="user-1/paper-1/paper.pdf",
                size_bytes=100,
                sha256="a" * 64,
                page_count=2,
                status=PaperStatus.ready,
            )
        )
        await session.flush()
        session.add_all(
            [
                PaperPage(
                    id="source-1",
                    paper_id="paper-1",
                    physical_page=1,
                    text=source_text,
                ),
                PaperPage(
                    id="source-2", paper_id="paper-1", physical_page=2, text=""
                ),
            ]
        )
        session.add(
            PaperTranslation(
                id="translation-1",
                paper_id="paper-1",
                owner_id="user-1",
                target_language="zh-CN",
                source_revision="b" * 64,
                status="running",
                total_pages=2,
                priority_page=1,
            )
        )
        await session.flush()
        session.add_all(
            [
                PaperTranslationPage(
                    id="translation-page-1",
                    translation_id="translation-1",
                    physical_page=1,
                    status=page_status,
                    source_text_hash=hashlib.sha256(source_text.encode()).hexdigest(),
                    priority=0,
                    attempts=attempts,
                ),
                PaperTranslationPage(
                    id="translation-page-2",
                    translation_id="translation-1",
                    physical_page=2,
                    status="no_text",
                    source_text_hash=hashlib.sha256(b"").hexdigest(),
                    priority=1002,
                    error_code="NO_TRANSLATABLE_TEXT",
                    error_message="此页暂无可翻译文本",
                ),
            ]
        )
        session.add(
            Job(
                id="job-1",
                paper_id="paper-1",
                translation_id="translation-1",
                type="translate_paper",
                status=JobStatus.running,
                attempts=1,
                claim_token="lease-1",
                claimed_at=worker.utcnow(),
            )
        )
        await session.commit()
    return source_text


def test_translation_worker_persists_valid_page_and_no_text_state(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'success.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_translation(sessions)
        monkeypatch.setattr(worker, "get_session_factory", lambda: sessions)

        calls: list[str] = []

        async def fake_translate(text: str, target: str, router=None, **kwargs) -> str:
            calls.append(text)
            assert target == "zh-CN"
            return "来源页译文，保留公式 E = mc^2 和引用 [7]。"

        monkeypatch.setattr(worker, "translate_page_text", fake_translate)
        await worker.process_translation_job(
            "job-1", "lease-1", router=AvailableRouter()
        )
        async with sessions() as session:
            translation = await session.get(PaperTranslation, "translation-1")
            first = await session.get(PaperTranslationPage, "translation-page-1")
            second = await session.get(PaperTranslationPage, "translation-page-2")
            job = await session.get(Job, "job-1")
            assert translation.status == "completed"
            assert translation.completed_pages == 1
            assert first.status == "completed"
            assert first.translated_text.startswith("来源页译文")
            assert first.error_code is None
            assert first.error_message is None
            assert second.status == "no_text"
            assert job.status == JobStatus.completed
            assert job.progress == 100
            assert job.claim_token is None
        assert len(calls) == 1
        await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error_code", "expected_page", "expected_job"),
    [
        ("MODEL_AUTHENTICATION_FAILED", "failed", JobStatus.failed),
        ("MODEL_TIMEOUT", "queued", JobStatus.queued),
    ],
)
def test_translation_worker_retries_only_transient_errors(
    tmp_path, monkeypatch, error_code: str, expected_page: str, expected_job: JobStatus
) -> None:
    async def scenario() -> None:
        database = tmp_path / f"retry-{error_code}.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_translation(sessions)
        monkeypatch.setattr(worker, "get_session_factory", lambda: sessions)
        calls = 0

        async def failed_translate(text: str, target: str, router=None, **kwargs) -> str:
            nonlocal calls
            calls += 1
            raise ModelRuntimeError(error_code, [])

        monkeypatch.setattr(worker, "translate_page_text", failed_translate)
        await worker.process_translation_job(
            "job-1", "lease-1", router=AvailableRouter()
        )
        async with sessions() as session:
            page = await session.get(PaperTranslationPage, "translation-page-1")
            job = await session.get(Job, "job-1")
            assert page.status == expected_page
            assert page.attempts == 1
            assert job.status == expected_job
            if expected_job == JobStatus.queued:
                assert job.available_at > job.updated_at
        assert calls == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_translation_worker_does_not_expose_generic_exception_text(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'redaction.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_translation(sessions)
        monkeypatch.setattr(worker, "get_session_factory", lambda: sessions)

        async def failed_translate(text: str, target: str, router=None, **kwargs) -> str:
            raise RuntimeError("provider secret=should-never-be-public")

        monkeypatch.setattr(worker, "translate_page_text", failed_translate)
        await worker.process_translation_job(
            "job-1", "lease-1", router=AvailableRouter()
        )
        async with sessions() as session:
            page = await session.get(PaperTranslationPage, "translation-page-1")
            assert page.status == "failed"
            assert page.error_code == "PAGE_TRANSLATION_FAILED"
            assert "secret" not in (page.error_message or "").casefold()
        await engine.dispose()

    asyncio.run(scenario())


def test_translation_worker_rejects_model_output_after_cancel(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_translation(sessions)
        monkeypatch.setattr(worker, "get_session_factory", lambda: sessions)

        async def cancel_then_return(text: str, target: str, router=None, **kwargs) -> str:
            async with sessions() as session:
                translation = await session.get(PaperTranslation, "translation-1")
                translation.cancel_requested = True
                translation.status = "cancelled"
                await session.commit()
            return "这段输出不应入库"

        monkeypatch.setattr(worker, "translate_page_text", cancel_then_return)
        await worker.process_translation_job(
            "job-1", "lease-1", router=AvailableRouter()
        )
        async with sessions() as session:
            page = await session.get(PaperTranslationPage, "translation-page-1")
            assert page.status == "cancelled"
            assert page.translated_text is None
        await engine.dispose()

    asyncio.run(scenario())


def test_claim_job_recovers_stale_translation_page_and_rotates_fencing_token(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_translation(sessions, page_status="running", attempts=1)
        async with sessions() as session:
            job = await session.get(Job, "job-1")
            job.claimed_at = worker.utcnow() - worker.JOB_LEASE - timedelta(seconds=1)
            await session.commit()
        monkeypatch.setattr(worker, "get_session_factory", lambda: sessions)

        claimed = await worker.claim_job()
        assert claimed is not None
        assert claimed.id == "job-1"
        assert claimed.token != "lease-1"
        async with sessions() as session:
            page = await session.get(PaperTranslationPage, "translation-page-1")
            job = await session.get(Job, "job-1")
            # claim 事务只轮换 Job token；页恢复在持有新 token 的 Worker 内完成，
            # 避免与取消路径形成 Job→Translation / Translation→Job 锁顺序反转。
            assert page.status == "running"
            assert job.status == JobStatus.running
            assert job.claim_token == claimed.token
            assert job.attempts == 2

        async def recovered_translate(text: str, target: str, router=None, **kwargs) -> str:
            return "租约恢复后的译文"

        monkeypatch.setattr(worker, "translate_page_text", recovered_translate)
        await worker.process_translation_job(
            claimed.id, claimed.token, router=AvailableRouter()
        )
        async with sessions() as session:
            page = await session.get(PaperTranslationPage, "translation-page-1")
            job = await session.get(Job, "job-1")
            assert page.status == "completed"
            assert page.translated_text == "租约恢复后的译文"
            assert job.status == JobStatus.completed
        await engine.dispose()

    asyncio.run(scenario())


def test_reclaimed_worker_cannot_overwrite_cancelled_translation(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease-cancel.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_translation(sessions, page_status="running", attempts=1)
        async with sessions() as session:
            job = await session.get(Job, "job-1")
            job.claimed_at = worker.utcnow() - worker.JOB_LEASE - timedelta(seconds=1)
            await session.commit()
        monkeypatch.setattr(worker, "get_session_factory", lambda: sessions)
        claimed = await worker.claim_job()
        assert claimed is not None

        async with sessions() as session:
            translation = await session.get(PaperTranslation, "translation-1")
            page = await session.get(PaperTranslationPage, "translation-page-1")
            job = await session.get(Job, "job-1")
            translation.cancel_requested = True
            translation.status = "cancelled"
            page.status = "cancelled"
            job.status = JobStatus.completed
            job.claim_token = None
            job.claimed_at = None
            await session.commit()

        calls = 0

        async def must_not_run(text: str, target: str, router=None, **kwargs) -> str:
            nonlocal calls
            calls += 1
            return "不得写入"

        monkeypatch.setattr(worker, "translate_page_text", must_not_run)
        await worker.process_translation_job(
            claimed.id, claimed.token, router=AvailableRouter()
        )
        async with sessions() as session:
            translation = await session.get(PaperTranslation, "translation-1")
            page = await session.get(PaperTranslationPage, "translation-page-1")
            assert translation.status == "cancelled"
            assert translation.cancel_requested is True
            assert page.status == "cancelled"
            assert page.translated_text is None
        assert calls == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_expired_token_cannot_heartbeat_call_model_or_write_failure(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'expired.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_translation(sessions)
        async with sessions() as session:
            job = await session.get(Job, "job-1")
            job.claimed_at = worker.utcnow() - worker.JOB_LEASE - timedelta(seconds=1)
            await session.commit()
        monkeypatch.setattr(worker, "get_session_factory", lambda: sessions)

        assert await worker._heartbeat_translation_job("job-1", "lease-1") is False
        calls = 0

        async def must_not_run(text: str, target: str, router=None, **kwargs) -> str:
            nonlocal calls
            calls += 1
            return "过期 Worker 不得得到此结果"

        monkeypatch.setattr(worker, "translate_page_text", must_not_run)
        await worker.process_translation_job(
            "job-1", "lease-1", router=AvailableRouter()
        )
        await worker.fail_job(
            worker.ClaimedJob("job-1", "lease-1"), RuntimeError("late failure")
        )

        async with sessions() as session:
            job = await session.get(Job, "job-1")
            translation = await session.get(PaperTranslation, "translation-1")
            page = await session.get(PaperTranslationPage, "translation-page-1")
            assert job.status == JobStatus.running
            assert job.claim_token == "lease-1"
            claimed_at = job.claimed_at
            assert claimed_at is not None
            if claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=timezone.utc)
            assert claimed_at < worker.utcnow() - worker.JOB_LEASE
            assert job.error_code is None
            assert translation.status == "running"
            assert page.status == "queued"
            assert page.translated_text is None
        assert calls == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_claim_job_does_not_reclaim_exhausted_stale_job(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'exhausted.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_translation(sessions, page_status="running", attempts=3)
        async with sessions() as session:
            job = await session.get(Job, "job-1")
            job.attempts = job.max_attempts
            job.claimed_at = worker.utcnow() - worker.JOB_LEASE - timedelta(seconds=1)
            await session.commit()
        monkeypatch.setattr(worker, "get_session_factory", lambda: sessions)

        assert await worker.claim_job() is None
        async with sessions() as session:
            page = await session.get(PaperTranslationPage, "translation-page-1")
            translation = await session.get(PaperTranslation, "translation-1")
            job = await session.get(Job, "job-1")
            assert page.status == "failed"
            assert page.error_code == "WORKER_LEASE_EXHAUSTED"
            assert translation.status == "failed"
            assert translation.failed_pages == 1
            assert job.status == JobStatus.failed
            assert job.attempts == job.max_attempts
            assert job.claim_token is None
        await engine.dispose()

    asyncio.run(scenario())
