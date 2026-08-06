import asyncio

from paperleaf_api.models import PaperStatus
from paperleaf_api.repository import MemoryRepository, PaperRecord


def test_artifact_cache_is_user_scoped_and_reindex_marks_it_stale() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("artifact-test")
        paper = PaperRecord(
            id="paper-1",
            owner_id="user-1",
            title="测试论文",
            authors=[],
            year=None,
            abstract=None,
            doi=None,
            arxiv_id=None,
            filename="paper.pdf",
            storage_key="user-1/paper.pdf",
            mime_type="application/pdf",
            size_bytes=100,
            sha256="a" * 64,
            page_count=2,
            status=PaperStatus.ready,
        )
        await repository.create_paper(paper)
        created = await repository.upsert_paper_artifact(
            paper.id,
            paper.owner_id,
            "summary",
            "b" * 64,
            "ready",
            None,
            {"sections": []},
            "## 总结",
        )
        assert created is not None
        assert await repository.get_owned_paper_artifact(
            paper.id, "other-user", "summary"
        ) is None
        cached = await repository.get_owned_paper_artifact(
            paper.id, paper.owner_id, "summary"
        )
        assert cached is not None and cached.status == "ready"

        await repository.mark_paper_artifacts_stale(paper.id)
        stale = await repository.get_owned_paper_artifact(
            paper.id, paper.owner_id, "summary"
        )
        assert stale is not None and stale.status == "stale"

    asyncio.run(scenario())
