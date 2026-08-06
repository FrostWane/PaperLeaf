"""真实 PostgreSQL 的翻译事务、租约与 0006 结构测试（默认跳过）。"""

import asyncio
import os
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from paperleaf_api import db, worker
from paperleaf_api.models import (
    Job,
    JobStatus,
    Paper,
    PaperPage,
    PaperStatus,
    PaperTranslation,
    PaperTranslationPage,
    UserRole,
)
from paperleaf_api.repository import PaperRecord, SQLAlchemyRepository

TEST_DATABASE_URL = os.getenv("PAPERLEAF_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要设置 PAPERLEAF_TEST_DATABASE_URL 并先执行 Alembic upgrade head",
)


def _test_database_url() -> str:
    assert TEST_DATABASE_URL
    url = make_url(TEST_DATABASE_URL)
    if "test" not in (url.database or "").casefold():
        pytest.fail("PAPERLEAF_TEST_DATABASE_URL 的数据库名必须包含 test")
    if url.drivername != "postgresql+asyncpg":
        pytest.fail("PAPERLEAF_TEST_DATABASE_URL 必须使用 postgresql+asyncpg")
    return TEST_DATABASE_URL


async def _truncate(engine) -> None:
    async with engine.begin() as connection:
        await connection.execute(sa.text("TRUNCATE TABLE users CASCADE"))


def test_postgres_0006_schema_and_translation_concurrency(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine(_test_database_url(), poolclass=NullPool)
        previous_engine, previous_factory = db._engine, db._session_factory
        db._engine = engine
        db._session_factory = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        try:
            await _truncate(engine)
            async with engine.connect() as connection:
                schema = await connection.run_sync(
                    lambda sync_connection: {
                        "tables": set(sa.inspect(sync_connection).get_table_names()),
                        "job_columns": {
                            item["name"]
                            for item in sa.inspect(sync_connection).get_columns("jobs")
                        },
                        "job_indexes": {
                            item["name"]: item
                            for item in sa.inspect(sync_connection).get_indexes("jobs")
                        },
                        "translation_columns": {
                            item["name"]
                            for item in sa.inspect(sync_connection).get_columns(
                                "paper_translations"
                            )
                        },
                    }
                )
            assert "paper_translations" in schema["tables"]
            assert "paper_translation_pages" in schema["tables"]
            assert {"translation_id", "claimed_at", "claim_token"} <= schema[
                "job_columns"
            ]
            assert schema["job_indexes"]["uq_jobs_translation_id"]["unique"] is True
            assert "source_revision" in schema["translation_columns"]

            repository = SQLAlchemyRepository("translation-test-secret")
            user = await repository.create_user(
                "reader@example.com",
                "reader-password-123",
                UserRole.user,
                must_change_password=False,
            )
            paper = await repository.create_paper(
                PaperRecord(
                    id="paper-translation-test",
                    owner_id=user.id,
                    title="Translation test",
                    authors=[],
                    year=None,
                    abstract=None,
                    doi=None,
                    arxiv_id=None,
                    filename="paper.pdf",
                    storage_key=f"{user.id}/paper.pdf",
                    mime_type="application/pdf",
                    size_bytes=100,
                    sha256="f" * 64,
                    page_count=1,
                    status=PaperStatus.ready,
                )
            )
            async with db.get_session_factory()() as session:
                session.add(
                    PaperPage(
                        paper_id=paper.id,
                        physical_page=1,
                        text="Text for concurrent translation.",
                    )
                )
                await session.commit()

            created = await asyncio.gather(
                repository.create_or_resume_translation(
                    paper.id, user.id, "zh-CN", 1, model_available=True
                ),
                repository.create_or_resume_translation(
                    paper.id, user.id, "zh-CN", 1, model_available=True
                ),
            )
            assert created[0] is not None and created[1] is not None
            assert created[0].id == created[1].id
            translation_id = created[0].id
            async with db.get_session_factory()() as session:
                translate_job_count = await session.scalar(
                    select(func.count()).select_from(Job).where(
                        Job.translation_id == translation_id,
                        Job.type == "translate_paper",
                    )
                )
                assert translate_job_count == 1
                await session.execute(
                    delete(Job).where(Job.type == "parse_pdf", Job.paper_id == paper.id)
                )
                translate_job = await session.scalar(
                    select(Job).where(Job.translation_id == translation_id)
                )
                retry_page = await session.scalar(
                    select(PaperTranslationPage).where(
                        PaperTranslationPage.translation_id == translation_id
                    )
                )
                backoff_until = worker.utcnow() + timedelta(minutes=10)
                translate_job.status = JobStatus.queued
                translate_job.attempts = 2
                translate_job.available_at = backoff_until
                translate_job.claimed_at = None
                translate_job.claim_token = None
                translate_job.error_code = "PAGE_TRANSLATION_RETRY"
                translate_job.error_message = "部分页面将在退避后重试"
                retry_page.status = "queued"
                retry_page.attempts = 2
                retry_page.error_code = "MODEL_TIMEOUT"
                retry_page.error_message = "此页翻译暂时失败，将在退避后重试"
                await session.commit()

            idempotent_backoff = await asyncio.gather(
                repository.create_or_resume_translation(
                    paper.id, user.id, "zh-CN", 1, model_available=True
                ),
                repository.create_or_resume_translation(
                    paper.id, user.id, "zh-CN", 1, model_available=True
                ),
            )
            assert all(
                item and item.id == translation_id for item in idempotent_backoff
            )
            async with db.get_session_factory()() as session:
                translate_job = await session.scalar(
                    select(Job).where(Job.translation_id == translation_id)
                )
                retry_page = await session.scalar(
                    select(PaperTranslationPage).where(
                        PaperTranslationPage.translation_id == translation_id
                    )
                )
                assert translate_job.status == JobStatus.queued
                assert translate_job.attempts == 2
                assert translate_job.available_at == backoff_until
                assert translate_job.claimed_at is None
                assert translate_job.claim_token is None
                assert translate_job.error_code == "PAGE_TRANSLATION_RETRY"
                assert retry_page.attempts == 2
                assert retry_page.error_code == "MODEL_TIMEOUT"

            model_calls = 0

            async def must_not_translate(*args, **kwargs) -> str:
                nonlocal model_calls
                model_calls += 1
                return "退避期不应调用模型"

            monkeypatch.setattr(worker, "translate_page_text", must_not_translate)
            assert await worker.claim_job() is None
            assert model_calls == 0

            cancelled = await repository.cancel_owned_translation(
                paper.id, translation_id, user.id
            )
            assert cancelled is not None and cancelled.status == "cancelled"
            restarted = await asyncio.gather(
                repository.create_or_resume_translation(
                    paper.id, user.id, "zh-CN", 1, model_available=True
                ),
                repository.create_or_resume_translation(
                    paper.id, user.id, "zh-CN", 1, model_available=True
                ),
            )
            assert all(item and item.id == translation_id for item in restarted)

            async with db.get_session_factory()() as session:
                translate_job = await session.scalar(
                    select(Job).where(Job.translation_id == translation_id)
                )
                translation = await session.get(PaperTranslation, translation_id)
                page = await session.scalar(
                    select(PaperTranslationPage).where(
                        PaperTranslationPage.translation_id == translation_id
                    )
                )
                assert translation.cancel_requested is False
                assert translate_job.status == JobStatus.queued
                assert translate_job.attempts == 0
                assert page.status == "queued"
                page.status = "completed"
                page.translated_text = "即将因重索引失效的旧译文"
                translation.status = "completed"
                translation.completed_pages = 1
                translate_job.status = JobStatus.completed
                old_revision = translation.source_revision
                await session.commit()

            requeued = await repository.requeue_owned_paper(paper.id, user.id)
            assert requeued is not None
            async with db.get_session_factory()() as session:
                current_paper = await session.get(Paper, paper.id)
                source_page = await session.scalar(
                    select(PaperPage).where(PaperPage.paper_id == paper.id)
                )
                current_paper.status = PaperStatus.ready
                source_page.text = "Changed text after a completed reindex."
                await session.execute(
                    delete(Job).where(Job.type == "parse_pdf", Job.paper_id == paper.id)
                )
                await session.commit()

            replaced = await asyncio.gather(
                repository.create_or_resume_translation(
                    paper.id, user.id, "zh-CN", 1, model_available=True
                ),
                repository.create_or_resume_translation(
                    paper.id, user.id, "zh-CN", 1, model_available=True
                ),
            )
            assert all(item and item.id == translation_id for item in replaced)
            async with db.get_session_factory()() as session:
                translate_job_count = await session.scalar(
                    select(func.count()).select_from(Job).where(
                        Job.translation_id == translation_id
                    )
                )
                translate_job = await session.scalar(
                    select(Job).where(Job.translation_id == translation_id)
                )
                translation = await session.get(PaperTranslation, translation_id)
                page = await session.scalar(
                    select(PaperTranslationPage).where(
                        PaperTranslationPage.translation_id == translation_id
                    )
                )
                assert translate_job_count == 1
                assert translate_job.status == JobStatus.queued
                assert translate_job.attempts == 0
                assert translate_job.claim_token is None
                assert translation.source_revision != old_revision
                assert translation.completed_pages == 0
                assert page.status == "queued"
                assert page.translated_text is None

            # 两个 Worker 并发领取同一重启作业，只有一个能获得 token，避免重复模型费用。
            concurrent_claims = await asyncio.gather(worker.claim_job(), worker.claim_job())
            active_claims = [item for item in concurrent_claims if item is not None]
            assert len(active_claims) == 1
            async with db.get_session_factory()() as session:
                translate_job = await session.scalar(
                    select(Job).where(Job.translation_id == translation_id)
                )
                translate_job.status = JobStatus.running
                translate_job.claim_token = "expired-token"
                translate_job.claimed_at = worker.utcnow() - worker.JOB_LEASE - timedelta(
                    seconds=1
                )
                page = await session.scalar(
                    select(PaperTranslationPage).where(
                        PaperTranslationPage.translation_id == translation_id
                    )
                )
                page.status = "running"
                await session.commit()

            claimed = await worker.claim_job()
            assert claimed is not None
            assert claimed.token != "expired-token"

            class AvailableRouter:
                def has_provider(self, purpose: str) -> bool:
                    return purpose == "translation"

            async def recovered_translate(
                text: str, target: str, router=None, **kwargs
            ) -> str:
                return "恢复后的译文"

            monkeypatch.setattr(worker, "translate_page_text", recovered_translate)
            await worker.process_translation_job(
                claimed.id, claimed.token, router=AvailableRouter()
            )
            async with db.get_session_factory()() as session:
                page = await session.scalar(
                    select(PaperTranslationPage).where(
                        PaperTranslationPage.translation_id == translation_id
                    )
                )
                assert page.status == "completed"
                assert page.translated_text == "恢复后的译文"

            await asyncio.gather(
                repository.delete_owned_paper(paper.id, user.id),
                repository.delete_owned_paper(paper.id, user.id),
            )
            async with db.get_session_factory()() as session:
                delete_job_count = await session.scalar(
                    select(func.count()).select_from(Job).where(
                        Job.paper_id == paper.id,
                        Job.type == "delete_paper",
                    )
                )
                assert delete_job_count == 1
        finally:
            await _truncate(engine)
            await engine.dispose()
            db._engine, db._session_factory = previous_engine, previous_factory

    asyncio.run(scenario())
