import asyncio

import pytest

from paperleaf_api.agent.graph import (
    _build_citation_aliases,
    _evidence_for_support_check,
    _normalize_answer_citations,
    build_agent_graph,
    build_configured_answerer,
)
from paperleaf_api.agent.tools import ArxivSearchInput, LibrarySearchInput, ToolResult
from paperleaf_api.model_runtime import ModelRouter, ModelRuntimeError
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


def test_citation_aliases_restore_full_chunk_ids_without_cross_paper_guessing() -> None:
    evidence = [
        Evidence("paper-a:p1:c0", "paper-a", "论文 A", 1, "证据 A"),
        Evidence("paper-b:p1:c0", "paper-b", "论文 B", 1, "证据 B"),
        Evidence("paper-a:p2:c0", "paper-a", "论文 A", 2, "证据 C"),
    ]
    aliases = _build_citation_aliases(evidence)

    normalized = _normalize_answer_citations(
        "别名 [chunk:E1]；无歧义短 ID [chunk:p2:c0]；"
        "跨论文歧义短 ID [chunk:p1:c0]。",
        evidence,
        aliases,
    )

    assert aliases == {
        "E1": "paper-a:p1:c0",
        "E2": "paper-b:p1:c0",
        "E3": "paper-a:p2:c0",
    }
    assert "[chunk:paper-a:p1:c0]" in normalized
    assert "[chunk:paper-a:p2:c0]" in normalized
    assert "[chunk:p1:c0]" in normalized


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


async def no_evidence_answerer(query: str, evidence: list[Evidence]):
    assert evidence == []
    return (
        "我目前没有检索到能够对应这篇论文的正文片段，因此不能可靠概括它的具体方法和结论。"
        "你可以等待索引完成，或把问题限定到某个章节后再试。当前文献证据不足。",
        [],
    )


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
    assert result["evidence_quality"]["answer_support_grade"] == "not_checked"


def test_graph_does_not_disguise_raw_extract_as_ai_answer_without_model() -> None:
    no_model_answerer = build_configured_answerer(model_router=ModelRouter([]))
    with pytest.raises(ModelRuntimeError) as captured:
        _run(build_agent_graph(MultiSentenceEvidenceRetriever(), no_model_answerer))

    assert captured.value.error_code == "MODEL_NOT_CONFIGURED"


def test_graph_abstains_when_no_evidence_exists() -> None:
    result = _run(build_agent_graph(EmptyRetriever(), no_evidence_answerer))

    assert result["status"] == "completed"
    assert result["citations"] == []
    assert "当前文献证据不足" in result["answer"]


def test_graph_abstains_when_retrieval_is_nonempty_but_irrelevant() -> None:
    result = _run(build_agent_graph(IrrelevantRetriever(), answerer))

    assert result["status"] == "completed"
    assert result["citations"][0].chunk_id == "c2"
    assert result["evidence_quality"]["retrieval_grade"] == "insufficient"
    assert result["evidence_quality"]["answer_support_grade"] == "not_checked"
    assert "附录列出了实验硬件" in result["answer"]


def test_graph_suppresses_answer_with_forged_citation() -> None:
    result = _run(build_agent_graph(EvidenceRetriever(), forged_answerer))

    assert result["status"] == "completed"
    assert result["citations"] == []
    assert "未通过服务端校验" in result["answer"]


def test_secondary_support_grader_cannot_replace_a_valid_cited_answer() -> None:
    result = _run(
        build_agent_graph(
            EvidenceRetriever(),
            answerer,
            support_grader=unsupported_grader,
        )
    )

    assert result["status"] == "completed"
    assert result["citations"][0].chunk_id == "c1"
    assert result["evidence_quality"]["retrieval_grade"] == "sufficient"
    assert result["evidence_quality"]["answer_support_grade"] == "not_checked"


def test_graph_keeps_natural_paragraph_after_citation_ids_are_validated() -> None:
    result = _run(build_agent_graph(EvidenceRetriever(), partially_cited_answerer))

    assert result["status"] == "completed"
    assert result["citations"][0].chunk_id == "c1"
    assert "另一个结论" in result["answer"]


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
