import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from paperleaf_api import worker
from paperleaf_api.models import Base, Job, JobStatus, Paper, PaperStatus, User


async def _seed(factory, *, attempts: int, max_attempts: int, token: str) -> None:
    async with factory() as session:
        session.add(
            User(
                id="user",
                email="worker-hardening@example.com",
                password_hash="hash",
                must_change_password=False,
            )
        )
        session.add(
            Paper(
                id="paper",
                owner_id="user",
                title="故障注入论文",
                authors=[],
                filename="paper.pdf",
                storage_key="user/paper.pdf",
                size_bytes=100,
                sha256="a" * 64,
                page_count=1,
                status=PaperStatus.extracting,
            )
        )
        session.add(
            Job(
                id="job",
                paper_id="paper",
                type="parse_pdf",
                status=JobStatus.running,
                attempts=attempts,
                max_attempts=max_attempts,
                claimed_at=worker.utcnow(),
                claim_token=token,
            )
        )
        await session.commit()


def test_pdf_parse_crash_requeues_then_reaches_explicit_terminal_state(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'parse-crash.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(worker, "get_session_factory", lambda: factory)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await _seed(factory, attempts=1, max_attempts=2, token="lease-1")

        await worker.fail_job(worker.ClaimedJob("job", "lease-1"), RuntimeError("boom secret"))
        async with factory() as session:
            job = await session.get(Job, "job")
            paper = await session.get(Paper, "paper")
            assert job.status == JobStatus.queued
            assert job.error_code == "JOB_EXECUTION_FAILED"
            assert job.error_message == "作业执行失败，请查看服务日志"
            assert job.claim_token is None
            assert paper.status == PaperStatus.extracting

            job.status = JobStatus.running
            job.attempts = 2
            job.claim_token = "lease-2"
            job.claimed_at = worker.utcnow()
            await session.commit()

        await worker.fail_job(
            worker.ClaimedJob("job", "lease-2"), RuntimeError("PDF_PARSE_FAILED")
        )
        async with factory() as session:
            job = await session.get(Job, "job")
            paper = await session.get(Paper, "paper")
            assert job.status == JobStatus.failed
            assert job.error_code == "PDF_PARSE_FAILED"
            assert job.error_message == "作业执行失败，请查看服务日志"
            assert paper.status == PaperStatus.failed
        await engine.dispose()

    asyncio.run(scenario())


def test_stale_pdf_worker_cannot_overwrite_new_lease(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fencing.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(worker, "get_session_factory", lambda: factory)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await _seed(factory, attempts=2, max_attempts=3, token="new-lease")

        await worker.fail_job(
            worker.ClaimedJob("job", "expired-lease"), RuntimeError("PDF_PARSE_FAILED")
        )
        async with factory() as session:
            job = await session.get(Job, "job")
            paper = await session.get(Paper, "paper")
            assert job.status == JobStatus.running
            assert job.claim_token == "new-lease"
            assert job.error_code is None
            assert paper.status == PaperStatus.extracting
        await engine.dispose()

    asyncio.run(scenario())
