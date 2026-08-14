import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from paperleaf_api.agent.answerability import AnswerabilityDecision
from paperleaf_api.agent.graph import (
    _build_citation_aliases,
    _ensure_external_recommendation_shape,
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


def test_exact_year_external_results_never_fall_back_to_older_local_references() -> None:
    contexts = [
        json.dumps(
            [
                {
                    "kind": "result",
                    "tool": "mcp__academic__search_openalex",
                    "content": json.dumps(
                        {
                            "source": "OpenAlex",
                            "available": True,
                            "items": [
                                {
                                    "title": "Recent DTA Method",
                                    "year": 2025,
                                    "doi": "10.1000/recent-dta",
                                }
                            ],
                        }
                    ),
                }
            ]
        )
    ]
    answer = _ensure_external_recommendation_shape(
        "模型从本地 PDF 参考文献拼出了 2024–2025 年候选。",
        "有没有更近的论文，如2026年的\n\n[已验证阅读上下文]\n继续联网推荐 5 篇相关论文",
        contexts,
        [Evidence("local", "p1", "DeepDTA", 8, "2024 年参考文献")],
    )

    assert answer.startswith("### 联网推荐")
    assert "没有返回符合条件" in answer
    assert "没有用当前文献库参考文献" in answer
    assert "Recent DTA Method" not in answer
    assert "2024 年参考文献" not in answer


def test_exact_year_external_recommendation_keeps_only_requested_year() -> None:
    items = [
        {
            "title": f"DTA 2026 paper {index}",
            "year": 2026,
            "publication": "Test Journal",
            "doi": f"10.1000/dta.{index}",
        }
        for index in range(1, 6)
    ] + [{"title": "Older DTA", "year": 2025}]
    contexts = [
        json.dumps(
            {
                "kind": "result",
                "tool": "mcp__academic__search_openalex",
                "content": json.dumps(
                    {"source": "OpenAlex", "available": True, "items": items}
                ),
            }
        )
    ]

    answer = _ensure_external_recommendation_shape(
        "",
        "再推荐 5 篇 2026 年论文",
        contexts,
        [],
    )

    assert answer.count("| **DTA 2026 paper") == 5
    assert "Older DTA" not in answer


def test_full_scope_titles_are_used_for_dedup_beyond_first_eight() -> None:
    items = [
        {
            "title": f"Cross-domain candidate {index}",
            "year": 2026,
            "publication": "General Research",
            "doi": f"10.1000/general.{index}",
        }
        for index in range(1, 8)
    ]
    contexts = [
        json.dumps(
            {
                "kind": "result",
                "tool": "mcp__academic__search_openalex",
                "content": json.dumps(
                    {"source": "OpenAlex", "available": True, "items": items}
                ),
            }
        )
    ]
    existing = [f"Library paper {index}" for index in range(1, 9)]
    existing.append("Cross-domain candidate 1")

    answer = _ensure_external_recommendation_shape(
        "",
        "请推荐五篇相关论文",
        contexts,
        [],
        existing,
    )

    assert "Cross-domain candidate 1" not in answer
    assert answer.count("| **Cross-domain candidate") == 5
    assert "DTA" not in answer
    assert "药物" not in answer


def test_external_shortfall_uses_only_real_metadata_instead_of_model_fill() -> None:
    contexts = [
        json.dumps(
            {
                "kind": "result",
                "tool": "mcp__academic__search_openalex",
                "content": json.dumps(
                    {
                        "source": "OpenAlex",
                        "available": True,
                        "items": [
                            {
                                "title": "Only verified candidate",
                                "year": 2026,
                                "doi": "10.1000/verified",
                            }
                        ],
                    }
                ),
            }
        )
    ]

    answer = _ensure_external_recommendation_shape(
        "模型虚构候选 A、B、C、D、E",
        "recommend five papers",
        contexts,
        [],
    )

    assert answer.startswith("### 联网推荐")
    assert "Only verified candidate" in answer
    assert "模型虚构" not in answer
    assert "本轮只找到 1 篇" in answer


def test_failed_external_search_does_not_claim_zero_verified_results() -> None:
    contexts = [
        json.dumps(
            [
                {
                    "kind": "call",
                    "tool": "mcp__academic__search_openalex",
                    "content": '{"query":"DeepDTA","year_from":2026}',
                },
                {
                    "kind": "result",
                    "tool": "mcp__academic__search_openalex",
                    "content": (
                        '{"tool":"mcp__academic__search_openalex","status":"failed",'
                        '"error_code":"OPENALEX_TIMEOUT"}'
                    ),
                },
            ]
        )
    ]

    answer = _ensure_external_recommendation_shape(
        "OpenAlex 超时，无法完成联网检索。",
        "再推荐 5 篇 2026 年论文",
        contexts,
        [],
    )

    assert answer.startswith("### 联网推荐")
    assert "OpenAlex 本轮响应超时" in answer
    assert "没有返回可核验的候选论文" in answer
    assert "模型猜测" in answer


def test_rate_limited_source_returns_controlled_notice_not_local_recommendations() -> None:
    contexts = [
        json.dumps(
            [
                {
                    "kind": "result",
                    "tool": "mcp__academic__search_semantic_scholar",
                    "content": json.dumps(
                        {
                            "tool": "mcp__academic__search_semantic_scholar",
                            "status": "failed",
                            "error_code": "SEMANTIC_SCHOLAR_RATE_LIMITED",
                        }
                    ),
                }
            ]
        )
    ]

    answer = _ensure_external_recommendation_shape(
        "模型根据本地论文给出了一批候选。",
        "请只使用 Semantic Scholar 推荐 five papers",
        contexts,
        [Evidence("local", "p1", "本地论文", 1, "本地 PDF 片段")],
    )

    assert "Semantic Scholar 本轮请求频率受限" in answer
    assert "本地论文给出" not in answer


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
    assert result["evidence_quality"]["answer_support_grade"] == "supported"


def test_graph_passes_frozen_retrieval_config_to_worker_retriever() -> None:
    captured = {}

    class CapturingRetriever(EvidenceRetriever):
        async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
            captured.update(request.retrieval_config)
            return await super().__call__(request)

    frozen = {"schema_version": 1, "fingerprint": "frozen-config"}
    graph = build_agent_graph(CapturingRetriever(), answerer)
    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": "u1",
                "query": "结论是什么？",
                "selected_paper_ids": [],
                "retrieval_config": frozen,
            },
            {"recursion_limit": 8},
        )
    )

    assert result["status"] == "completed"
    assert captured == frozen


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


def test_configured_answerer_uses_compact_specialist_synthesis_context(monkeypatch) -> None:
    captured_prompts: list[list[tuple[str, str]]] = []
    captured_timeouts: list[float] = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            assert kwargs["max_tokens"] == 500

        async def astream(self, prompt_messages):
            captured_prompts.append(prompt_messages)
            yield SimpleNamespace(content="三篇论文采用不同视觉表示 [chunk:E1]。")

    class SuccessRouter:
        def has_provider(self, purpose):
            return purpose == "answer"

        async def execute(self, _purpose, operation, *, timeout_seconds=None):
            captured_timeouts.append(timeout_seconds)
            provider = SimpleNamespace(
                chat_model="deepseek-chat",
                api_key="test-key",
                base_url="http://model.invalid/v1",
            )
            return await operation(provider)

    fake_langchain = ModuleType("langchain_openai")
    fake_langchain.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_langchain)
    config = SimpleNamespace(
        evidence_min_confidence=0.35,
        evidence_min_vector_score=0.35,
        evidence_min_lexical_coverage=0.18,
        agent_answer_timeout_seconds=90.0,
        agent_answer_retry_timeout_seconds=60.0,
    )
    evidence = [Evidence("c1", "p1", "论文一", 1, "卷积网络学习视觉表示。")]

    text, citations = asyncio.run(
        build_configured_answerer(config, SuccessRouter())(
            "比较三篇论文",
            evidence,
            [
                {
                    "role": "research_synthesis",
                    "content": '{"findings":[{"claim":"ResNet 使用卷积残差结构"}]}',
                }
            ],
        )
    )

    assert text == "三篇论文采用不同视觉表示 [chunk:c1]。"
    assert citations[0].chunk_id == "c1"
    assert captured_timeouts == [60.0]
    prompt = "\n".join(content for _, content in captured_prompts[0])
    assert "多个只读 Specialist" in prompt
    assert "不是引用源" in prompt
    assert "ResNet 使用卷积残差结构" in prompt
    assert "首次回答因模型响应超时" not in prompt


def test_configured_answerer_treats_openalex_results_as_metadata_not_pdf_evidence(
    monkeypatch,
) -> None:
    captured_prompts: list[list[tuple[str, str]]] = []

    class FakeChatOpenAI:
        def __init__(self, **_kwargs):
            pass

        async def astream(self, prompt_messages):
            captured_prompts.append(prompt_messages)
            yield SimpleNamespace(content="推荐 DeepDTA（来源：OpenAlex；2018）。")

    class SuccessRouter:
        def has_provider(self, purpose):
            return purpose == "answer"

        async def execute(self, _purpose, operation, *, timeout_seconds=None):
            provider = SimpleNamespace(
                chat_model="deepseek-chat",
                api_key="test-key",
                base_url="http://model.invalid/v1",
            )
            return await operation(provider)

    fake_langchain = ModuleType("langchain_openai")
    fake_langchain.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_langchain)
    config = SimpleNamespace(
        evidence_min_confidence=0.35,
        evidence_min_vector_score=0.35,
        evidence_min_lexical_coverage=0.18,
        agent_answer_timeout_seconds=90.0,
        agent_answer_retry_timeout_seconds=60.0,
    )

    text, citations = asyncio.run(
        build_configured_answerer(config, SuccessRouter())(
            "请用 OpenAlex 找相关论文",
            [],
            [
                {
                    "role": "tool_context",
                    "content": (
                        '{"source":"OpenAlex","items":['
                        '{"title":"DeepDTA","year":2018}]}'
                    ),
                }
            ],
        )
    )

    assert text == "推荐 DeepDTA（来源：OpenAlex；2018）。"
    assert citations == []
    prompt = "\n".join(content for _, content in captured_prompts[0])
    assert "就必须直接整理这些真实结果" in prompt
    assert "尚未导入和核验 PDF 全文" in prompt
    assert "本地 PDF 证据质量" in prompt
    assert '"title":"DeepDTA"' in prompt


def test_external_recommendation_falls_back_to_complete_metadata_list(
    monkeypatch,
) -> None:
    class FakeChatOpenAI:
        def __init__(self, **_kwargs):
            pass

        async def astream(self, _prompt_messages):
            yield SimpleNamespace(content="1. Candidate 1\n2. Candidate 2")

    class SuccessRouter:
        def has_provider(self, purpose):
            return purpose == "answer"

        async def execute(self, _purpose, operation, *, timeout_seconds=None):
            provider = SimpleNamespace(
                chat_model="deepseek-chat",
                api_key="test-key",
                base_url="http://model.invalid/v1",
            )
            return await operation(provider)

    fake_langchain = ModuleType("langchain_openai")
    fake_langchain.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_langchain)
    config = SimpleNamespace(
        evidence_min_confidence=0.35,
        evidence_min_vector_score=0.35,
        evidence_min_lexical_coverage=0.18,
        agent_answer_timeout_seconds=90.0,
        agent_answer_retry_timeout_seconds=60.0,
    )
    items = [
        {
            "title": "DeepDTA: deep drug-target binding affinity prediction",
            "year": 2018,
            "publication": "Bioinformatics",
            "doi": "10.0000/existing",
        },
        *[
                {
                    "title": f"Candidate {index}",
                    "year": 2020 + index,
                    "publication": f"Venue {index}",
                    "doi": None if index == 1 else f"10.0000/candidate.{index}",
                    "url": "javascript:alert(1)" if index == 1 else None,
            }
            for index in range(1, 8)
        ],
    ]
    evidence = [
        Evidence(
            "deepdta-chunk",
            "deepdta",
            "DeepDTA: deep drug-target binding affinity prediction",
            1,
            "本地论文证据",
        )
    ]

    text, citations = asyncio.run(
        build_configured_answerer(config, SuccessRouter())(
            "请联网推荐 5 篇尚未在库中的相关论文",
            evidence,
            [
                {
                    "role": "tool_context",
                    "content": json.dumps(
                        {
                            "source": "OpenAlex",
                            "existing_scope_titles": [
                                "DeepDTA",
                                "Candidate 3",
                            ],
                            "items": items,
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        )
    )

    assert "Candidate 1" in text
    assert "Candidate 3" not in text
    assert "Candidate 6" in text
    assert "Candidate 7" not in text
    assert "DeepDTA:" not in text
    assert "10.0000/candidate.5" in text
    assert "javascript:" not in text
    assert text.count("| OpenAlex |") == 5
    assert "[chunk:" not in text
    assert citations == []


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
    assert result["evidence_quality"]["answer_support_grade"] == "supported"
    assert "附录列出了实验硬件" in result["answer"]


def test_answerability_gate_stops_adjacent_evidence_before_answer_generation() -> None:
    calls = 0

    async def should_not_answer(query, evidence):
        nonlocal calls
        calls += 1
        return await answerer(query, evidence)

    async def unanswerable(query, evidence):
        assert evidence
        return AnswerabilityDecision(
            answerable=False,
            confidence=0.97,
            reason_code="adjacent_topic",
        )

    result = _run(
        build_agent_graph(
            IrrelevantRetriever(),
            should_not_answer,
            answerability_grader=unanswerable,
            answerability_min_confidence=0.72,
        )
    )

    assert calls == 0
    assert result["status"] == "completed"
    assert result["answerability_status"] == "unanswerable"
    assert result["citations"] == []
    assert "没有直接提供所问信息" in result["answer"]


def test_answerability_gate_allows_direct_evidence() -> None:
    async def answerable(query, evidence):
        return AnswerabilityDecision(
            answerable=True,
            confidence=0.93,
            reason_code="direct_answer",
        )

    result = _run(
        build_agent_graph(
            EvidenceRetriever(),
            answerer,
            answerability_grader=answerable,
            answerability_min_confidence=0.72,
        )
    )

    assert result["answerability_status"] == "answerable"
    assert result["citations"][0].chunk_id == "c1"


def test_answerability_grader_failure_does_not_block_normal_answer() -> None:
    async def unavailable(query, evidence):
        return AnswerabilityDecision(
            answerable=None,
            confidence=None,
            reason_code="grader_unavailable",
        )

    result = _run(
        build_agent_graph(
            EvidenceRetriever(),
            answerer,
            answerability_grader=unavailable,
        )
    )

    assert result["answerability_status"] == "not_checked"
    assert result["citations"][0].chunk_id == "c1"


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


def test_graph_repairs_missing_claim_citations_before_suppressing_answer() -> None:
    calls = 0

    async def overview_answerer(query, evidence, messages=None):
        nonlocal calls
        calls += 1
        source = evidence[0]
        citation = CitationClaim(source.chunk_id, source.paper_id, source.physical_page)
        if calls == 1:
            return (
                f"论文使用页级检索 [chunk:{source.chunk_id}]。"
                "它还得出了另一个没有引用的结论。",
                [citation],
            )
        assert any(item.get("role") == "answer_repair" for item in (messages or []))
        return f"论文使用页级检索 [chunk:{source.chunk_id}]。", [citation]

    async def supporting_grader(query, answer, evidence):
        return AnswerSupport(True, 0.99, "answer_supported")

    result = _run(
        build_agent_graph(
            EvidenceRetriever(),
            overview_answerer,
            support_grader=supporting_grader,
        )
    )

    assert calls == 2
    assert result["status"] == "completed"
    assert result["answer_repair_attempted"] is True
    assert result["support_repair_attempted"] is True
    assert result["support_repair_succeeded"] is True
    assert result["evidence_quality"]["answer_support_grade"] == "supported"
    assert result["citations"][0].chunk_id == "c1"


def test_graph_uses_strict_deterministic_support_when_semantic_grader_is_unavailable() -> None:
    async def unavailable_grader(query, answer, evidence):
        return AnswerSupport(False, 0.0, "grader_unavailable")

    result = _run(
        build_agent_graph(
            EvidenceRetriever(),
            answerer,
            support_grader=unavailable_grader,
        )
    )

    assert result["status"] == "completed"
    assert result["evidence_quality"]["answer_support_grade"] == "supported"
    assert result["evidence_quality"]["reason_code"] == "deterministic_claim_support"
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


@pytest.mark.parametrize("use_langgraph", [False, True])
def test_external_tool_context_generates_answer_without_falling_through_to_arxiv(
    use_langgraph: bool,
) -> None:
    captured_messages: list[dict] = []

    async def answer_from_openalex(query, evidence, messages=None):
        assert evidence == []
        captured_messages.extend(messages or [])
        return "OpenAlex 返回了相关论文元数据。", []

    async def unexpected_arxiv_search(_request: ArxivSearchInput) -> ToolResult:
        raise AssertionError("已有 OpenAlex 结果时不应再次搜索 arXiv")

    graph = build_agent_graph(
        EmptyRetriever(),
        answer_from_openalex,
        arxiv_search=unexpected_arxiv_search,
        use_langgraph=use_langgraph,
    )
    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": "u1",
                "query": "请用 OpenAlex 查找相关论文",
                "selected_paper_ids": [],
                "web_enabled": True,
                "tool_mode_active": True,
                "tool_context_entries": [
                    {
                        "kind": "call",
                        "tool_call_id": "openalex-1",
                        "tool": "mcp__academic__search_openalex",
                        "content": '{"query":"DeepDTA"}',
                    },
                    {
                        "kind": "result",
                        "tool_call_id": "openalex-1",
                        "tool": "mcp__academic__search_openalex",
                        "content": '{"source":"OpenAlex","items":[{"title":"DeepDTA"}]}',
                    },
                ],
                "context_budget": {"hard_limit": 3000},
            },
            {"recursion_limit": 8},
        )
    )

    assert result["status"] == "completed"
    assert result["answer"] == "OpenAlex 返回了相关论文元数据。"
    assert any(item.get("role") == "tool_context" for item in captured_messages)


@pytest.mark.parametrize("use_langgraph", [False, True])
def test_external_metadata_answer_bypasses_pdf_citation_and_semantic_gates(
    use_langgraph: bool,
) -> None:
    async def external_answerer(query, evidence, messages=None, scope_titles=()):
        return "### 联网推荐\n\n服务端将使用真实元数据重建清单。", []

    local_evidence = Evidence("local-c1", "p1", "库内论文", 1, "本地证据")
    graph = build_agent_graph(
        EmptyRetriever(),
        external_answerer,
        use_langgraph=use_langgraph,
        support_grader=unsupported_grader,
    )
    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": "u1",
                "query": "推荐五篇相关论文",
                "selected_paper_ids": ["p1"],
                "selected_skill": "find_related_papers",
                "tool_mode_active": True,
                "pre_retrieved_evidence": [local_evidence],
                "tool_context_entries": [
                    {
                        "kind": "call",
                        "tool_call_id": "openalex-1",
                        "tool": "mcp__academic__search_openalex",
                        "content": '{"query":"topic"}',
                    },
                    {
                        "kind": "result",
                        "tool_call_id": "openalex-1",
                        "tool": "mcp__academic__search_openalex",
                        "content": json.dumps(
                            {
                                "source": "OpenAlex",
                                "available": True,
                                "items": [
                                    {"title": "Verified candidate", "year": 2026}
                                ],
                            }
                        ),
                    },
                ],
                "context_budget": {"hard_limit": 3000},
            },
            {"recursion_limit": 8},
        )
    )

    assert result["status"] == "completed"
    assert result["external_metadata_answer"] is True
    assert result["citation_validation_passed"] is True
    assert result["citations"] == []
    assert result["answer"].startswith("### 联网推荐")
    assert "语义支持核验" not in result["answer"]


def test_secondary_support_grader_returns_citation_validated_low_confidence_answer() -> None:
    result = _run(
        build_agent_graph(
            EvidenceRetriever(),
            answerer,
            support_grader=unsupported_grader,
        )
    )

    assert result["status"] == "completed"
    assert result["citations"][0].chunk_id == "c1"
    assert "论文结论" in result["answer"]
    assert "不返回结论" not in result["answer"]
    assert "建议结合页码来源回读" in result["answer"]
    assert result["evidence_quality"]["retrieval_grade"] == "sufficient"
    assert result["evidence_quality"]["answer_support_grade"] == "not_checked"
    assert (
        result["evidence_quality"]["reason_code"]
        == "citation_validated_low_confidence"
    )


def test_graph_prunes_uncited_claims_before_publishing_natural_paragraph() -> None:
    result = _run(build_agent_graph(EvidenceRetriever(), partially_cited_answerer))

    assert result["status"] == "completed"
    assert [item.chunk_id for item in result["citations"]] == ["c1"]
    assert "模型通过检索证据回答" in result["answer"]
    assert "另一个关键事实没有引用" not in result["answer"]
    assert result["evidence_quality"]["answer_support_grade"] == "supported"


def test_graph_returns_supported_claim_subset_instead_of_suppressing_entire_answer() -> None:
    async def two_claim_answerer(query, evidence, messages=None):
        source = evidence[0]
        citation = CitationClaim(source.chunk_id, source.paper_id, source.physical_page)
        return (
            f"论文结论是模型通过检索证据回答 [chunk:{source.chunk_id}]。"
            f"前瞻性实验已经完成 [chunk:{source.chunk_id}]。",
            [citation],
        )

    async def partial_support_grader(query, answer, evidence):
        return AnswerSupport(
            False,
            0.95,
            "answer_not_supported",
            supported_claim_indices=(1,),
        )

    result = _run(
        build_agent_graph(
            EvidenceRetriever(),
            two_claim_answerer,
            support_grader=partial_support_grader,
        )
    )

    assert result["status"] == "completed"
    assert "模型通过检索证据回答" in result["answer"]
    assert "前瞻性实验" not in result["answer"]
    assert "不返回结论" not in result["answer"]
    assert "仅保留了能够直接回读原文的结论" in result["answer"]
    assert result["evidence_quality"]["reason_code"] == "partial_answer_supported"
    assert result["evidence_quality"]["answer_support_grade"] == "supported"


def test_graph_returns_citation_validated_answer_when_semantic_grader_is_unavailable() -> None:
    async def cross_language_answerer(query, evidence, messages=None):
        source = evidence[0]
        return f"论文采用零探针先验完成预测 [chunk:{source.chunk_id}]。", [
            CitationClaim(source.chunk_id, source.paper_id, source.physical_page)
        ]

    async def unavailable_grader(query, answer, evidence):
        return AnswerSupport(False, 0.0, "grader_unavailable")

    result = _run(
        build_agent_graph(
            EvidenceRetriever(),
            cross_language_answerer,
            support_grader=unavailable_grader,
        )
    )

    assert result["status"] == "completed"
    assert "论文采用零探针先验" in result["answer"]
    assert "不返回结论" not in result["answer"]
    assert "语义复核服务暂时不可用" in result["answer"]
    assert result["evidence_quality"]["reason_code"] == "citation_validated_provisional"
    assert result["evidence_quality"]["answer_support_grade"] == "not_checked"


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
