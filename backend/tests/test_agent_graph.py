import asyncio

from paperleaf_api.agent.graph import (
    _evidence_for_support_check,
    build_agent_graph,
    build_configured_answerer,
)
from paperleaf_api.agent.tools import ArxivSearchInput, LibrarySearchInput, ToolResult
from paperleaf_api.model_runtime import ModelRouter
from paperleaf_api.rag.answer_quality import extract_answer_claims
from paperleaf_api.rag.citations import CitationClaim, Evidence
from paperleaf_api.rag.retrieval_quality import AnswerSupport


class EvidenceRetriever:
    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        return [
            Evidence(
                "c1",
                "p1",
                "测试论文",
                4,
                "论文结论是模型通过检索证据回答。",
                retrieval_score=0.03,
                retrieval_channels=("keyword",),
                channel_scores=(("keyword", 0.4),),
            )
        ]


class IrrelevantRetriever:
    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        return [Evidence("c2", "p1", "测试论文", 9, "附录列出了实验硬件。")]


class MultiSentenceEvidenceRetriever:
    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        return [
            Evidence(
                "c-multi",
                "p1",
                "多句测试论文",
                6,
                (
                    "论文结论是模型通过检索证据回答。"
                    "实验结论显示引用提高了可核验性。"
                    "作者结论要求每条主张附带来源。"
                ),
                retrieval_score=0.03,
                retrieval_channels=("keyword",),
                channel_scores=(("keyword", 0.4),),
            )
        ]


class EmptyRetriever:
    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        return []


def test_support_check_uses_cited_evidence_instead_of_first_retrieval_items() -> None:
    evidence = [
        Evidence(f"c{index}", "p1", "测试论文", index, f"第 {index} 页证据")
        for index in range(1, 9)
    ]

    selected = _evidence_for_support_check("结论来自后文 [chunk:c8]。", evidence)

    assert [item.chunk_id for item in selected] == ["c8"]


class FakeArxivSearch:
    async def __call__(self, request: ArxivSearchInput) -> ToolResult:
        return ToolResult(
            data=[{"arxiv_id": "2401.00001", "title": "候选论文"}],
            audit_summary="返回一个候选",
        )


async def answerer(query: str, evidence: list[Evidence]):
    source = evidence[0]
    return f"{source.text} [chunk:{source.chunk_id}]", [
        CitationClaim(source.chunk_id, source.paper_id, source.physical_page, source.text)
    ]


async def forged_answerer(query: str, evidence: list[Evidence]):
    return "伪造回答", [CitationClaim("forged", "p1", 99, "")]


async def unsupported_grader(
    query: str, answer: str, evidence: list[Evidence]
) -> AnswerSupport:
    assert "论文结论" in answer
    return AnswerSupport(False, 0.94, "answer_not_supported")


async def partially_cited_answerer(query: str, evidence: list[Evidence]):
    source = evidence[0]
    return (
        f"{source.text} [chunk:{source.chunk_id}] 另一个结论没有引用。",
        [CitationClaim(source.chunk_id, source.paper_id, source.physical_page, source.text)],
    )


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
    assert result["answer"].startswith("论文结论")
    assert result["citations"][0].physical_page == 4
    assert result["evidence_quality"]["grade"] == "sufficient"
    assert result["evidence_quality"]["claim_citation_coverage"] == 1.0
    assert result["evidence_quality"]["answer_support_grade"] == "supported"


def test_graph_no_model_fallback_cites_every_extractive_claim() -> None:
    no_model_answerer = build_configured_answerer(model_router=ModelRouter([]))
    result = _run(build_agent_graph(MultiSentenceEvidenceRetriever(), no_model_answerer))

    claims = extract_answer_claims(result["answer"])
    assert result["status"] == "completed"
    assert result["answer"].startswith("原文摘录：")
    assert len(claims) == 3
    assert all(claim.citation_ids == ("c-multi",) for claim in claims)
    assert result["citations"][0].chunk_id == "c-multi"
    assert result["evidence_quality"]["claim_citation_coverage"] == 1.0
    assert result["evidence_quality"]["claim_support_coverage"] == 1.0
    assert result["evidence_quality"]["answer_support_grade"] == "supported"


def test_graph_abstains_when_no_evidence_exists() -> None:
    result = _run(build_agent_graph(EmptyRetriever(), answerer))

    assert result["status"] == "completed"
    assert result["citations"] == []
    assert "没有找到可核验的证据页" in result["answer"]


def test_graph_abstains_when_retrieval_is_nonempty_but_irrelevant() -> None:
    result = _run(build_agent_graph(IrrelevantRetriever(), answerer))

    assert result["status"] == "completed"
    assert result["citations"] == []
    assert result["evidence_quality"]["reason_code"] == "weak_match"
    assert "匹配度不足" in result["answer"]


def test_graph_suppresses_answer_with_forged_citation() -> None:
    result = _run(build_agent_graph(EvidenceRetriever(), forged_answerer))

    assert result["status"] == "completed"
    assert result["citations"] == []
    assert "未通过服务端校验" in result["answer"]


def test_graph_abstains_when_evidence_is_relevant_but_does_not_support_answer() -> None:
    result = _run(
        build_agent_graph(
            EvidenceRetriever(),
            answerer,
            support_grader=unsupported_grader,
        )
    )

    assert result["status"] == "completed"
    assert result["citations"] == []
    assert result["evidence_quality"]["retrieval_grade"] == "sufficient"
    assert result["evidence_quality"]["answer_support_grade"] == "unsupported"


def test_graph_suppresses_answer_when_one_claim_has_no_citation() -> None:
    result = _run(build_agent_graph(EvidenceRetriever(), partially_cited_answerer))

    assert result["status"] == "completed"
    assert result["citations"] == []
    assert result["evidence_quality"]["reason_code"] == "missing_claim_citations"
    assert result["evidence_quality"]["claim_citation_coverage"] == 0.5
    assert "已覆盖 1/2 条主张" in result["answer"]


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
