import asyncio
import uuid
from dataclasses import replace

from fastapi import Response

from paperleaf_api.config import settings
from paperleaf_api.main import create_app
from paperleaf_api.models import JobStatus, PaperStatus
from paperleaf_api.rag.citations import Evidence
from paperleaf_api.repository import MemoryRepository, PaperRecord
from paperleaf_api.storage import LocalObjectStorage


def _summary_payload(sequence: int) -> dict:
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
                        "text": f"第 {sequence} 次生成",
                        "citations": [{"chunk_id": "c1", "physical_page": 1}],
                    }
                ],
            }
        )
    return {
        "sections": sections,
        "citations": [{"chunk_id": "c1", "physical_page": 1}],
        "mode": "model",
    }


def _structure_payload(sequence: int) -> dict:
    nodes = []
    for index, node_type in enumerate(
        ("研究问题", "方法", "实验", "结果", "局限"), start=1
    ):
        nodes.append(
            {
                "id": f"n{index}",
                "type": node_type,
                "label": f"节点 {index}",
                "summary": f"第 {sequence} 次生成",
                "citations": [{"chunk_id": "c1", "physical_page": 1}],
            }
        )
    edges = [
        {"source": f"n{index}", "target": f"n{index + 1}"}
        for index in range(1, 5)
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "mermaid": "flowchart TD\n    n1 --> n2",
    }


def test_artifact_refresh_query_bypasses_ready_cache(tmp_path, monkeypatch) -> None:
    config = replace(settings, mode="local", local_storage_path=tmp_path)
    repository = MemoryRepository(config.session_secret)
    app = create_app(
        config,
        repository=repository,
        storage=LocalObjectStorage(tmp_path),
    )
    evidence = [Evidence("c1", "paper", "测试论文", 1, "可核验证据")]

    async def load_evidence(*args, **kwargs):
        return evidence

    async def load_revision(*args, **kwargs):
        return "revision-1"

    monkeypatch.setattr("paperleaf_api.main.load_paper_evidence", load_evidence)
    monkeypatch.setattr("paperleaf_api.main.load_paper_source_revision", load_revision)

    paths = app.openapi()["paths"]
    assert any(
        item["name"] == "refresh"
        for item in paths["/api/v1/papers/{paper_id}/summary"]["post"]["parameters"]
    )
    assert any(
        item["name"] == "refresh"
        for item in paths["/api/v1/papers/{paper_id}/structure-graph"]["post"][
            "parameters"
        ]
    )

    async def exercise() -> None:
        user = await repository.ensure_admin("admin@example.com", "secure-password")
        paper = await repository.create_paper(
            PaperRecord(
                id=str(uuid.uuid4()),
                owner_id=user.id,
                title="测试论文",
                authors=[],
                year=None,
                abstract=None,
                doi=None,
                arxiv_id=None,
                filename="paper.pdf",
                storage_key="paper.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                sha256="a" * 64,
                page_count=1,
                status=PaperStatus.ready,
            )
        )
        endpoints = {
            route.path: route.endpoint
            for route in app.routes
            if hasattr(route, "path") and hasattr(route, "endpoint")
        }
        summary = endpoints["/api/v1/papers/{paper_id}/summary"]
        structure = endpoints["/api/v1/papers/{paper_id}/structure-graph"]

        first_http = Response()
        first = await summary(paper.id, user, None, first_http, refresh=False)
        second = await summary(paper.id, user, None, Response(), refresh=False)
        summary_jobs = [job for job in repository.jobs.values() if job.type == "summarize_paper"]
        assert first.status == second.status == "processing"
        assert first_http.status_code == 202
        assert len(summary_jobs) == 1

        summary_jobs[0].status = JobStatus.completed
        await repository.upsert_paper_artifact(
            paper.id, user.id, "summary", "revision-1", "ready", None,
            _summary_payload(1), "稳定中文总结",
        )
        cached = await summary(paper.id, user, None, Response(), refresh=False)
        assert cached.status == "ready"
        assert cached.sections[0].facts[0].text == "第 1 次生成"

        refreshed = await summary(paper.id, user, None, Response(), refresh=True)
        assert refreshed.status == "processing"
        assert refreshed.sections[0].facts[0].text == "第 1 次生成"
        assert len([job for job in repository.jobs.values() if job.type == "summarize_paper"]) == 2

        first_structure = await structure(
            paper.id, user, None, Response(), refresh=False
        )
        second_structure = await structure(
            paper.id, user, None, Response(), refresh=False
        )
        assert first_structure.status == second_structure.status == "processing"
        structure_jobs = [
            job
            for job in repository.jobs.values()
            if job.type == "build_structure_graph"
        ]
        assert len(structure_jobs) == 1

        cached_summary = await repository.get_owned_paper_artifact(
            paper.id, user.id, "summary"
        )
        assert cached_summary is not None
        assert cached_summary.status == "ready"

    asyncio.run(exercise())
