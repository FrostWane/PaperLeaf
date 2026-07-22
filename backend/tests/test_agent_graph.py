import asyncio

from paperleaf_api.agent.graph import build_agent_graph
from paperleaf_api.agent.tools import ArxivSearchInput, LibrarySearchInput, ToolResult
from paperleaf_api.rag.citations import CitationClaim, Evidence


class EvidenceRetriever:
    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        return [Evidence("c1", "p1", "测试论文", 4, "模型通过检索证据回答。")]


class EmptyRetriever:
    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        return []


class FakeArxivSearch:
    async def __call__(self, request: ArxivSearchInput) -> ToolResult:
        return ToolResult(
            data=[{"arxiv_id": "2401.00001", "title": "候选论文"}],
            audit_summary="返回一个候选",
        )


async def answerer(query: str, evidence: list[Evidence]):
    source = evidence[0]
    return "有依据的回答", [
        CitationClaim(source.chunk_id, source.paper_id, source.physical_page, source.text)
    ]


async def forged_answerer(query: str, evidence: list[Evidence]):
    return "伪造回答", [CitationClaim("forged", "p1", 99, "")]


def _run(graph):
    return asyncio.run(
        graph.ainvoke(
            {"user_id": "u1", "query": "结论是什么？", "selected_paper_ids": []},
            {"recursion_limit": 8},
        )
    )


def test_graph_returns_cited_answer_when_evidence_exists() -> None:
    result = _run(build_agent_graph(EvidenceRetriever(), answerer))

    assert result["status"] == "completed"
    assert result["answer"] == "有依据的回答"
    assert result["citations"][0].physical_page == 4


def test_graph_abstains_when_no_evidence_exists() -> None:
    result = _run(build_agent_graph(EmptyRetriever(), answerer))

    assert result["status"] == "completed"
    assert result["citations"] == []
    assert "没有足够证据" in result["answer"]


def test_graph_suppresses_answer_with_forged_citation() -> None:
    result = _run(build_agent_graph(EvidenceRetriever(), forged_answerer))

    assert result["status"] == "completed"
    assert result["citations"] == []
    assert "未通过服务端校验" in result["answer"]


def test_graph_interrupts_before_arxiv_import() -> None:
    graph = build_agent_graph(
        EmptyRetriever(), answerer, arxiv_search=FakeArxivSearch(), use_langgraph=False
    )
    result = asyncio.run(
        graph.ainvoke(
            {"user_id": "u1", "query": "联网找论文", "web_enabled": True},
            {"recursion_limit": 8},
        )
    )

    assert result["status"] == "interrupted"
    assert result["pending_action"]["type"] == "confirm_arxiv_import"
    assert result["tool_steps"] == 2
