import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from paperleaf_api import db, worker
from paperleaf_api.artifacts import ArtifactGeneration
from paperleaf_api.models import (
    Base,
    Job,
    JobStatus,
    Paper,
    PaperArtifact,
    PaperChunk,
    PaperPage,
    PaperStatus,
    User,
    UserRole,
)


def _summary_payload() -> dict:
    sections = []
    for key, title in (
        ("research_question", "研究问题"),
        ("core_method", "核心方法"),
        ("experimental_setup", "实验设置"),
        ("main_results", "主要结果"),
        ("limitations_scope", "局限与适用范围"),
    ):
        sections.append(
            {
                "key": key,
                "title": title,
                "facts": [
                    {
                        "text": "这是经过引用校验的中文概括。",
                        "citations": [
                            {"chunk_id": "paper:p1:c0", "physical_page": 1}
                        ],
                    }
                ],
            }
        )
    return {
        "sections": sections,
        "citations": [{"chunk_id": "paper:p1:c0", "physical_page": 1}],
        "mode": "model",
    }


async def _seed(factory, *, artifact_status: str = "processing") -> tuple[str, str]:
    token = "artifact-claim-token"
    async with factory() as session:
        session.add(
            User(
                id="user",
                email="reader@example.com",
                password_hash="hash",
                role=UserRole.user,
                active=True,
                must_change_password=False,
            )
        )
        session.add(
            Paper(
                id="paper",
                owner_id="user",
                title="测试论文",
                authors=[],
                filename="paper.pdf",
                storage_key="user/paper.pdf",
                mime_type="application/pdf",
                size_bytes=100,
                sha256="a" * 64,
                page_count=1,
                status=PaperStatus.ready,
            )
        )
        session.add(
            PaperPage(
                id="page",
                paper_id="paper",
                physical_page=1,
                text="A source paragraph used only as untrusted model evidence.",
            )
        )
        session.add(
            PaperChunk(
                id="paper:p1:c0",
                page_id="page",
                paper_id="paper",
                physical_page=1,
                chunk_index=0,
                text="A source paragraph used only as untrusted model evidence.",
                token_count=10,
            )
        )
        session.add(
            PaperArtifact(
                id="artifact",
                paper_id="paper",
                owner_id="user",
                type="summary",
                source_revision="pending",
                status=artifact_status,
                fallback_reason=None,
                structured_payload=(
                    _summary_payload() if artifact_status == "ready" else {}
                ),
                markdown=("旧的中文概括" if artifact_status == "ready" else ""),
            )
        )
        session.add(
            Job(
                id="job",
                paper_id="paper",
                type="summarize_paper",
                status=JobStatus.running,
                attempts=2,
                max_attempts=2,
                claim_token=token,
                claimed_at=worker.utcnow(),
            )
        )
        await session.commit()
    return "job", token


def test_artifact_worker_persists_verified_chinese_summary(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
        previous_engine, previous_factory = db._engine, db._session_factory
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        db._engine, db._session_factory = engine, factory
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            job_id, token = await _seed(factory)

            async def generated(*args, **kwargs):
                return ArtifactGeneration(
                    "ready", None, _summary_payload(), "## 研究问题\n\n- 中文概括"
                )

            monkeypatch.setattr(worker, "generate_summary_artifact", generated)
            await worker.process_artifact_job(job_id, token)

            async with factory() as session:
                job = await session.get(Job, job_id)
                artifact = await session.get(PaperArtifact, "artifact")
                assert job.status == JobStatus.completed
                assert job.progress == 100
                assert artifact.status == "ready"
                assert artifact.structured_payload["mode"] == "model"
                assert "中文概括" in artifact.markdown
        finally:
            await engine.dispose()
            db._engine, db._session_factory = previous_engine, previous_factory

    asyncio.run(scenario())


@pytest.mark.parametrize("existing_status", ["processing", "ready"])
def test_artifact_worker_failure_never_saves_english_excerpt(
    tmp_path, monkeypatch, existing_status: str
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / f'failure-{existing_status}.db'}"
        )
        previous_engine, previous_factory = db._engine, db._session_factory
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        db._engine, db._session_factory = engine, factory
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            job_id, token = await _seed(factory, artifact_status=existing_status)

            async def failed(*args, **kwargs):
                return ArtifactGeneration(
                    "failed",
                    "论文总结模型在完整与精简两次生成中均响应超时",
                    {"sections": [], "citations": [], "mode": "model"},
                    "Raw English excerpt must not be persisted.",
                )

            monkeypatch.setattr(worker, "generate_summary_artifact", failed)
            with pytest.raises(worker.ArtifactJobError) as captured:
                await worker.process_artifact_job(job_id, token)
            await worker.fail_job(worker.ClaimedJob(job_id, token), captured.value)

            async with factory() as session:
                job = await session.get(Job, job_id)
                artifact = await session.get(PaperArtifact, "artifact")
                assert job.status == JobStatus.failed
                assert artifact.markdown != "Raw English excerpt must not be persisted."
                if existing_status == "ready":
                    assert artifact.status == "ready"
                    assert artifact.markdown == "旧的中文概括"
                else:
                    assert artifact.status == "failed"
                    assert artifact.markdown == ""
                    assert artifact.structured_payload == {}
                    assert "超时" in artifact.fallback_reason
        finally:
            await engine.dispose()
            db._engine, db._session_factory = previous_engine, previous_factory

    asyncio.run(scenario())
