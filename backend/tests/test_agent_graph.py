import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from paperleaf_api.agent.graph import (
    _build_citation_aliases,
    _evidence_for_support_check,
    _normalize_answer_citations,
    build_agent_graph,
    build_configured_answerer,
)
from paperleaf_api.agent.tools import ArxivSearchInput, LibrarySearchInput, ToolResult
from paperleaf_api.model_runtime import ModelAttempt, ModelRouter, ModelRuntimeError
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


def test_selection_lock_discards_legacy_evidence_from_other_pages() -> None:
    selected = Evidence("selected", "p1", "测试论文", 2, "选中段落的可信内容。")
    graph = build_agent_graph(EvidenceRetriever(), answerer)
    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": "u1",
                "query": "解释选中内容",
                "selected_paper_ids": ["p1"],
                "selection_evidence": [selected],
                "selection_scope_locked": True,
                "selection_physical_page": 2,
                "selection_paper_id": "p1",
            },
            {"recursion_limit": 8},
        )
    )

    assert result["status"] == "completed"
    assert [item.physical_page for item in result["citations"]] == [2]
    assert [item.physical_page for item in result["retrieved_evidence"]] == [2]


def test_graph_does_not_disguise_raw_extract_as_ai_answer_without_model() -> None:
    no_model_answerer = build_configured_answerer(model_router=ModelRouter([]))
    with pytest.raises(ModelRuntimeError) as captured:
        _run(build_agent_graph(MultiSentenceEvidenceRetriever(), no_model_answerer))

    assert captured.value.error_code == "MODEL_NOT_CONFIGURED"


@pytest.mark.parametrize("first_error", ["MODEL_TIMEOUT", "MODEL_CIRCUIT_OPEN"])
def test_configured_answerer_retries_transient_failure_once_with_compact_context(
    monkeypatch, first_error: str
) -> None:
    captured_prompts: list[list[tuple[str, str]]] = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            assert kwargs["max_tokens"] == 850

        async def astream(self, prompt_messages):
            captured_prompts.append(prompt_messages)
            yield SimpleNamespace(content="精简回答 [chunk:E1]")

    class TransientFailureThenSuccessRouter:
        timeout_seconds = 30.0

        def __init__(self):
            self.timeouts: list[float] = []

        def has_provider(self, purpose):
            return purpose == "answer"

        async def execute(self, purpose, operation, *, timeout_seconds=None):
            self.timeouts.append(timeout_seconds)
            if len(self.timeouts) == 1:
                status = "timed_out" if first_error == "MODEL_TIMEOUT" else "circuit_open"
                attempt = ModelAttempt(
                    "answer", "primary", "deepseek-chat", status, 90000, 1, False,
                    first_error,
                )
                raise ModelRuntimeError(first_error, [attempt])
            provider = SimpleNamespace(
                chat_model="deepseek-chat",
                api_key="test-key",
                base_url="http://model.invalid/v1",
            )
            return await operation(provider)

        def circuit_retry_after_seconds(self, _purpose):
            return 0.0

    fake_langchain = ModuleType("langchain_openai")
    fake_langchain.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_langchain)
    router = TransientFailureThenSuccessRouter()
    config = SimpleNamespace(
        evidence_min_confidence=0.35,
        evidence_min_vector_score=0.35,
        evidence_min_lexical_coverage=0.18,
        agent_answer_timeout_seconds=90.0,
        agent_answer_retry_timeout_seconds=60.0,
    )
    evidence = [
        Evidence(f"c{index}", "p1", "测试论文", index, f"第 {index} 条证据")
        for index in range(1, 13)
    ]

    text, citations = asyncio.run(
        build_configured_answerer(config, router)("比较这些证据", evidence)
    )

    assert router.timeouts == [90.0, 60.0]
    assert text == "精简回答 [chunk:c1]"
    assert citations[0].chunk_id == "c1"
    prompt = "\n".join(content for _, content in captured_prompts[0])
    assert "首次回答因模型响应超时" in prompt
    assert "[chunk:E10" in prompt
    assert "[chunk:E11" not in prompt


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


def test_graph_repairs_invalid_citation_once_without_user_retry() -> None:
    calls = 0

    async def repairing_answerer(query, evidence, messages=None):
        nonlocal calls
        calls += 1
        source = evidence[0]
        if calls == 1:
            return "第一稿引用不存在 [chunk:forged]。", [
                CitationClaim("forged", "p1", 99)
            ]
        assert any(item.get("role") == "answer_repair" for item in (messages or []))
        return f"修复后的结论 [chunk:{source.chunk_id}]。", [
            CitationClaim(source.chunk_id, source.paper_id, source.physical_page)
        ]

    result = _run(build_agent_graph(EvidenceRetriever(), repairing_answerer))

    assert calls == 2
    assert result["status"] == "completed"
    assert result["answer_repair_attempted"] is True
    assert result["answer_repair_succeeded"] is True
    assert result["citations"][0].chunk_id == "c1"


def test_graph_applies_final_budget_and_keeps_tool_call_result_pair_in_model_context() -> None:
    captured_messages: list[dict] = []

    async def capturing_answerer(query, evidence, messages=None):
        captured_messages.extend(messages or [])
        source = evidence[0]
        return f"工具证据已进入回答 [chunk:{source.chunk_id}]。", [
            CitationClaim(source.chunk_id, source.paper_id, source.physical_page)
        ]

    evidence = Evidence("c1", "p1", "测试论文", 4, "论文方法证据。")
    graph = build_agent_graph(EvidenceRetriever(), capturing_answerer)
    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": "u1",
                "query": "方法是什么？",
                "selected_paper_ids": ["p1"],
                "tool_mode_active": True,
                "pre_retrieved_evidence": [evidence],
                "tool_context_entries": [
                    {
                        "kind": "call",
                        "tool_call_id": "call-1",
                        "tool": "search_current_paper",
                        "content": '{"query":"方法"}',
                    },
                    {
                        "kind": "result",
                        "tool_call_id": "call-1",
                        "tool": "search_current_paper",
                        "content": '{"status":"succeeded"}',
                    },
                ],
                "context_budget": {"hard_limit": 3000},
            },
            {"recursion_limit": 8},
        )
    )

    tool_messages = [
        item for item in captured_messages if item.get("role") == "tool_context"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"].count('"tool_call_id": "call-1"') == 2
    assert result["context_usage"]["final_input_tokens"] <= 3000


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
