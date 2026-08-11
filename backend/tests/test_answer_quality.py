from paperleaf_api.rag.answer_quality import (
    assess_answer_support,
    extract_answer_claims,
    retain_cited_answer_claims,
)
from paperleaf_api.rag.citations import CitationClaim, Evidence
from paperleaf_api.rag.retrieval_quality import AnswerSupport


def _evidence(chunk_id: str, text: str) -> Evidence:
    return Evidence(chunk_id, "paper-1", "测试论文", 3, text)


def test_claim_parser_attaches_trailing_citation_marker() -> None:
    claims = extract_answer_claims("第一条结论。 [chunk:c1]\n第二条结论 [chunk:c2]。")

    assert [claim.text for claim in claims] == ["第一条结论", "第二条结论"]
    assert claims[0].citation_ids == ("c1",)
    assert claims[1].citation_ids == ("c2",)


def test_claim_parser_does_not_attach_previous_sentence_citation_to_next_claim() -> None:
    claims = extract_answer_claims(
        "第一条结论。 [chunk:c1] 第二条结论没有引用。"
    )

    assert [claim.text for claim in claims] == ["第一条结论", "第二条结论没有引用"]
    assert claims[0].citation_ids == ("c1",)
    assert claims[1].citation_ids == ()


def test_claim_parser_ignores_controlled_evidence_notice() -> None:
    claims = extract_answer_claims(
        "论文使用页级检索 [chunk:c1]。\n\n"
        "> 证据说明：当前检索片段与问题的匹配度有限，结论仅供初步参考。"
    )

    assert len(claims) == 1
    assert claims[0].citation_ids == ("c1",)


def test_claim_parser_ignores_markdown_structure_but_keeps_factual_bullets() -> None:
    claims = extract_answer_claims(
        "## 论文概览\n\n"
        "**核心方法**\n"
        "- 模型使用卷积网络编码序列 [chunk:c1]。\n\n"
        "| 方法 | 结果 |\n"
        "| --- | --- |\n"
        "> 证据说明：当前只展示已核验内容。"
    )

    assert [claim.text for claim in claims] == ["模型使用卷积网络编码序列"]
    assert claims[0].citation_ids == ("c1",)


def test_deterministic_support_requires_every_claim_to_be_cited_and_grounded() -> None:
    evidence = [
        _evidence("c1", "模型使用检索证据回答问题。"),
        _evidence("c2", "系统会逐条核验答案引用。"),
    ]
    citations = [
        CitationClaim("c1", "paper-1", 3),
        CitationClaim("c2", "paper-1", 3),
    ]

    support = assess_answer_support(
        "模型使用检索证据回答问题 [chunk:c1]。系统会逐条核验答案引用 [chunk:c2]。",
        citations,
        evidence,
        AnswerSupport(None, None, "not_configured"),
    )

    assert support.supported is True
    assert support.citation_coverage == 1.0
    assert support.support_coverage == 1.0
    assert support.supported_claim_count == 2


def test_semantic_grader_cannot_rescue_an_uncited_claim() -> None:
    evidence = [_evidence("c1", "模型使用检索证据回答问题。")]
    citations = [CitationClaim("c1", "paper-1", 3)]

    support = assess_answer_support(
        "模型使用检索证据回答问题 [chunk:c1]。另一个事实没有引用。",
        citations,
        evidence,
        AnswerSupport(True, 0.99, "answer_supported"),
    )

    assert support.supported is False
    assert support.reason_code == "missing_claim_citations"
    assert support.citation_coverage == 0.5


def test_retain_cited_claims_drops_uncited_text_without_inventing_sources() -> None:
    evidence = [
        _evidence("c1", "论文解决冷启动预测问题。"),
        _evidence("c2", "方法使用支持集校准预测。"),
    ]
    citations = [
        CitationClaim("c1", "paper-1", 3),
        CitationClaim("c2", "paper-1", 3),
    ]

    answer, retained = retain_cited_answer_claims(
        "## 概览\n论文解决冷启动预测问题 [chunk:c1]。"
        "这一句没有引用。方法使用支持集校准预测 [chunk:c2]。",
        citations,
        evidence,
    )

    assert answer == (
        "### 已核验要点\n"
        "- 论文解决冷启动预测问题 [chunk:c1]。\n"
        "- 方法使用支持集校准预测 [chunk:c2]。"
    )
    assert [item.chunk_id for item in retained] == ["c1", "c2"]
    assert "没有引用" not in answer


def test_configured_grader_failure_is_fail_closed() -> None:
    evidence = [_evidence("c1", "模型使用检索证据回答问题。")]

    support = assess_answer_support(
        "模型使用检索证据回答问题 [chunk:c1]。",
        [CitationClaim("c1", "paper-1", 3)],
        evidence,
        AnswerSupport(False, 0.0, "grader_unavailable"),
    )

    assert support.supported is False
    assert support.reason_code == "grader_unavailable"
