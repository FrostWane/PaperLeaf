"""生成后主张—证据契约与答案支持判定。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .citations import CitationClaim, Evidence
from .retrieval_quality import AnswerSupport, lexical_coverage

_CITATION_RE = re.compile(r"\[chunk:([^\]]+)\]")
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+(?:[。！？!?；;\n]+|$)")
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)、])\s*")


@dataclass(frozen=True)
class AnswerClaim:
    index: int
    text: str
    citation_ids: tuple[str, ...]


@dataclass(frozen=True)
class AnswerQualityPolicy:
    min_citation_coverage: float = 1.0
    min_claim_lexical_support: float = 0.12
    min_model_support_confidence: float = 0.6


def extract_answer_claims(answer: str) -> list[AnswerClaim]:
    """提取用户可见主张，并把紧随句后的引用标记绑定到上一条主张。"""

    claims: list[AnswerClaim] = []
    for match in _SENTENCE_RE.finditer(answer):
        fragment = match.group(0).strip()
        if not fragment:
            continue
        citation_ids = tuple(dict.fromkeys(_CITATION_RE.findall(fragment)))
        visible = _LIST_PREFIX_RE.sub("", _CITATION_RE.sub("", fragment)).strip()
        visible = visible.strip("。！？!?；; ")
        if not visible:
            if citation_ids and claims:
                previous = claims[-1]
                claims[-1] = AnswerClaim(
                    index=previous.index,
                    text=previous.text,
                    citation_ids=tuple(
                        dict.fromkeys((*previous.citation_ids, *citation_ids))
                    ),
                )
            continue
        claims.append(
            AnswerClaim(index=len(claims) + 1, text=visible, citation_ids=citation_ids)
        )
    return claims


def assess_answer_support(
    answer: str,
    citations: list[CitationClaim],
    evidence: list[Evidence],
    semantic_support: AnswerSupport,
    *,
    policy: AnswerQualityPolicy | None = None,
) -> AnswerSupport:
    """合并结构化引用覆盖、确定性文本支持与可选模型判定。"""

    policy = policy or AnswerQualityPolicy()
    claims = extract_answer_claims(answer)
    if not claims:
        return AnswerSupport(False, 0.0, "no_answer_claims")

    evidence_by_id = {item.chunk_id: item for item in evidence}
    validated_ids = {item.chunk_id for item in citations if item.chunk_id in evidence_by_id}
    cited_claims = 0
    lexical_supported_claims = 0
    lexical_scores: list[float] = []
    for claim in claims:
        source_ids = [
            chunk_id
            for chunk_id in claim.citation_ids
            if chunk_id in validated_ids and chunk_id in evidence_by_id
        ]
        if source_ids:
            cited_claims += 1
        score = max(
            (
                lexical_coverage(claim.text, evidence_by_id[chunk_id].text)
                for chunk_id in source_ids
            ),
            default=0.0,
        )
        lexical_scores.append(score)
        lexical_supported_claims += int(
            bool(source_ids) and score >= policy.min_claim_lexical_support
        )

    claim_count = len(claims)
    citation_coverage = cited_claims / claim_count
    deterministic_support_coverage = lexical_supported_claims / claim_count
    structural_pass = citation_coverage >= policy.min_citation_coverage
    deterministic_confidence = sum(lexical_scores) / claim_count

    if not structural_pass:
        return AnswerSupport(
            False,
            round(citation_coverage, 6),
            "missing_claim_citations",
            claim_count,
            cited_claims,
            lexical_supported_claims,
            citation_coverage,
            deterministic_support_coverage,
        )

    if semantic_support.supported is None:
        supported = deterministic_support_coverage == 1.0
        return AnswerSupport(
            supported,
            round(deterministic_confidence, 6),
            "deterministic_claim_support" if supported else "claim_not_grounded",
            claim_count,
            cited_claims,
            lexical_supported_claims,
            citation_coverage,
            deterministic_support_coverage,
        )

    semantic_confidence = semantic_support.confidence or 0.0
    supported = bool(
        semantic_support.supported
        and semantic_confidence >= policy.min_model_support_confidence
    )
    return AnswerSupport(
        supported,
        round(semantic_confidence, 6),
        semantic_support.reason_code
        if supported or not semantic_support.supported
        else "support_confidence_too_low",
        claim_count,
        cited_claims,
        claim_count if supported else 0,
        citation_coverage,
        1.0 if supported else 0.0,
    )
