"""页级检索后处理与证据质量门禁。

这里保存线上 Agent 与离线评测都能复用的确定性规则。它不尝试判断模型的
隐藏推理，只回答两个可审计问题：召回结果是否覆盖不同物理页，以及检索信号
是否足以支持把证据交给生成节点。
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, replace
from typing import Literal

from .citations import Evidence

_LATIN_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_LATIN_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "paper",
    "study",
    "the",
    "this",
    "to",
    "what",
    "which",
    "with",
}
_CJK_STOP_PHRASES = (
    "这篇论文",
    "这篇文献",
    "论文中",
    "文献中",
    "研究中",
    "请问",
    "什么",
    "哪些",
    "如何",
    "是否",
    "为什么",
)


@dataclass(frozen=True)
class EvidenceQualityPolicy:
    min_confidence: float = 0.35
    min_vector_score: float = 0.35
    min_lexical_coverage: float = 0.18


@dataclass(frozen=True)
class EvidenceQuality:
    grade: Literal["sufficient", "insufficient"]
    confidence: float
    reason_code: str
    summary: str
    evidence_count: int
    page_count: int
    paper_count: int
    channels: tuple[str, ...]
    lexical_coverage: float
    vector_score: float
    retrieval_grade: Literal["sufficient", "insufficient"]
    answer_support_grade: Literal["supported", "unsupported", "not_checked"]
    answer_support_confidence: float | None
    claim_count: int = 0
    cited_claim_count: int = 0
    supported_claim_count: int = 0
    claim_citation_coverage: float = 0.0
    claim_support_coverage: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AnswerSupport:
    supported: bool | None
    confidence: float | None
    reason_code: str
    claim_count: int = 0
    cited_claim_count: int = 0
    supported_claim_count: int = 0
    citation_coverage: float = 0.0
    support_coverage: float = 0.0


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _terms(text: str) -> set[str]:
    normalized = text.casefold()
    terms = {
        token
        for token in _LATIN_RE.findall(normalized)
        if token not in _LATIN_STOPWORDS and len(token) > 1
    }
    for run in _CJK_RE.findall(normalized):
        for phrase in _CJK_STOP_PHRASES:
            run = run.replace(phrase, "")
        if len(run) == 1:
            terms.add(run)
        elif run:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
            if len(run) <= 6:
                terms.add(run)
    return terms


def lexical_coverage(query: str, text: str) -> float:
    query_terms = _terms(query)
    if not query_terms:
        return 0.0
    return len(query_terms & _terms(text)) / len(query_terms)


def deduplicate_evidence_by_page(evidence: list[Evidence], *, limit: int) -> list[Evidence]:
    """同一物理页只占一个召回位，并合并该页的通道信号。"""
    if limit <= 0:
        raise ValueError("limit 必须为正数")
    grouped: dict[tuple[str, int], list[Evidence]] = {}
    order: list[tuple[str, int]] = []
    for item in evidence:
        key = (item.paper_id, item.physical_page)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(item)

    result: list[Evidence] = []
    for key in order:
        page_items = grouped[key]
        primary = max(page_items, key=lambda item: item.retrieval_score)
        channels = sorted({channel for item in page_items for channel in item.retrieval_channels})
        scores: dict[str, float] = {}
        for item in page_items:
            for channel, score in item.channel_scores:
                scores[channel] = max(scores.get(channel, float("-inf")), score)
        result.append(
            replace(
                primary,
                retrieval_score=max(item.retrieval_score for item in page_items),
                retrieval_channels=tuple(channels),
                channel_scores=tuple(sorted(scores.items())),
            )
        )
        if len(result) == limit:
            break
    return result


def assess_evidence(
    query: str,
    evidence: list[Evidence],
    *,
    policy: EvidenceQualityPolicy | None = None,
) -> EvidenceQuality:
    policy = policy or EvidenceQualityPolicy()
    page_count = len({(item.paper_id, item.physical_page) for item in evidence})
    paper_count = len({item.paper_id for item in evidence})
    channels = tuple(sorted({channel for item in evidence for channel in item.retrieval_channels}))
    base = {
        "evidence_count": len(evidence),
        "page_count": page_count,
        "paper_count": paper_count,
        "channels": channels,
    }
    if not evidence:
        return EvidenceQuality(
            grade="insufficient",
            confidence=0.0,
            reason_code="no_evidence",
            summary="没有找到可核验的证据页",
            lexical_coverage=0.0,
            vector_score=0.0,
            retrieval_grade="insufficient",
            answer_support_grade="not_checked",
            answer_support_confidence=None,
            **base,
        )

    if "demo" in channels:
        return EvidenceQuality(
            grade="sufficient",
            confidence=1.0,
            reason_code="demo_evidence",
            summary=f"已定位 {page_count} 个演示证据页",
            lexical_coverage=1.0,
            vector_score=1.0,
            retrieval_grade="sufficient",
            answer_support_grade="not_checked",
            answer_support_confidence=None,
            **base,
        )

    if "scoped_overview" in channels:
        return EvidenceQuality(
            grade="sufficient",
            confidence=1.0,
            reason_code="scoped_overview_support",
            summary=f"已从当前论文的 {page_count} 个代表性页面提取可核验证据",
            lexical_coverage=1.0,
            vector_score=0.0,
            retrieval_grade="sufficient",
            answer_support_grade="not_checked",
            answer_support_confidence=None,
            **base,
        )

    coverage = max(
        max(
            lexical_coverage(query, item.text),
            lexical_coverage(item.retrieval_query, item.text)
            if item.retrieval_query
            else 0.0,
        )
        for item in evidence[:5]
    )
    channel_scores = [pair for item in evidence for pair in item.channel_scores]
    vector_score = max((score for name, score in channel_scores if name == "vector"), default=0.0)
    keyword_raw = max(
        (
            score
            for name, score in channel_scores
            if name in {"keyword", "keyword_rewrite"}
        ),
        default=0.0,
    )
    keyword_score = max(coverage, 1 - math.exp(-4 * max(0.0, keyword_raw)))
    agreed = any({"keyword", "vector"}.issubset(item.retrieval_channels) for item in evidence)

    if vector_score > 0:
        confidence = 0.7 * _clamp(vector_score) + 0.2 * coverage + 0.1 * float(agreed)
    elif {"keyword", "keyword_rewrite"} & set(channels):
        confidence = 0.65 * _clamp(keyword_score) + 0.35 * coverage
    else:
        # 自定义检索器必须至少提供可复核的文本重合，不能因“列表非空”直接放行。
        confidence = coverage
    confidence = _clamp(confidence)

    vector_supported = vector_score >= policy.min_vector_score
    lexical_supported = coverage >= policy.min_lexical_coverage
    sufficient = confidence >= policy.min_confidence and (vector_supported or lexical_supported)
    if sufficient and agreed:
        reason = "channel_agreement"
        summary = f"已定位 {page_count} 个证据页，关键词与语义检索相互印证"
    elif sufficient and vector_supported:
        reason = "semantic_support"
        summary = f"已定位 {page_count} 个证据页，语义匹配通过质量门禁"
    elif sufficient and "keyword_rewrite" in channels:
        reason = "query_rewrite_support"
        summary = f"已定位 {page_count} 个证据页，查询改写后的术语与原文匹配"
    elif sufficient:
        reason = "lexical_support"
        summary = f"已定位 {page_count} 个证据页，原文术语与问题匹配"
    else:
        reason = "weak_match"
        summary = f"检索到 {page_count} 个候选证据页，但与问题的匹配度不足"
    return EvidenceQuality(
        grade="sufficient" if sufficient else "insufficient",
        confidence=round(confidence, 6),
        reason_code=reason,
        summary=summary,
        lexical_coverage=round(coverage, 6),
        vector_score=round(vector_score, 6),
        retrieval_grade="sufficient" if sufficient else "insufficient",
        answer_support_grade="not_checked",
        answer_support_confidence=None,
        **base,
    )


def apply_answer_support(quality: EvidenceQuality, support: AnswerSupport) -> EvidenceQuality:
    metrics = {
        "claim_count": support.claim_count,
        "cited_claim_count": support.cited_claim_count,
        "supported_claim_count": support.supported_claim_count,
        "claim_citation_coverage": round(_clamp(support.citation_coverage), 6),
        "claim_support_coverage": round(_clamp(support.support_coverage), 6),
    }
    if quality.retrieval_grade == "insufficient" or support.supported is None:
        return quality
    confidence = None if support.confidence is None else round(_clamp(support.confidence), 6)
    if support.supported:
        return replace(
            quality,
            grade="sufficient",
            answer_support_grade="supported",
            answer_support_confidence=confidence,
            reason_code=support.reason_code,
            summary=(
                f"{quality.summary}；回答的 {support.claim_count} 条主张均有可回读证据"
            ),
            **metrics,
        )
    return replace(
        quality,
        grade="insufficient",
        answer_support_grade="unsupported",
        answer_support_confidence=confidence,
        reason_code=support.reason_code,
        summary=(
            f"已定位 {quality.page_count} 个相关证据页，但最终回答没有通过逐条证据核验"
            if support.reason_code != "grader_unavailable"
            else "证据支持检查暂时不可用"
        ),
        **metrics,
    )
