"""生成后主张—证据契约与答案支持判定。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .citations import CitationClaim, Evidence
from .retrieval_quality import AnswerSupport, lexical_coverage

_CITATION_RE = re.compile(r"\[chunk:([^\]]+)\]")
_LEADING_CITATIONS_RE = re.compile(
    r"^\s*((?:\[chunk:[^\]]+\]\s*)+)(.*)$",
    re.DOTALL,
)
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+(?:[。！？!?；;\n]+|$)")
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)、]\s*)")
_CONTROLLED_NOTICE_RE = re.compile(r"^\s*>?\s*证据说明[：:]", re.IGNORECASE)
_STRUCTURAL_FRAGMENT_RE = re.compile(
    r"^\s*(?:#{1,6}\s+[^\n]+|[-*_]{3,}|```[^\n]*|~~~[^\n]*|"
    r"\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?)\s*$"
)
_BOLD_HEADING_RE = re.compile(r"^\s*\*\*[^*\n]{1,40}\*\*\s*[：:]?\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|[^\n]+\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?\s*$"
)
_MALFORMED_LEADING_EMPHASIS_RE = re.compile(
    r"^\*{1,2}([^*\n]{1,40})\*{1,2}(?=[：:])"
)


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
    matches = list(_SENTENCE_RE.finditer(answer))
    for match_index, match in enumerate(matches):
        fragment = match.group(0).strip()
        if not fragment:
            continue
        leading = _LEADING_CITATIONS_RE.match(fragment)
        if leading and claims:
            # 模型经常输出“事实。 [chunk:E1] 下一条事实。”。句子切分后
            # 引用会落在下一片段开头；它语义上属于上一句，不能错误绑定
            # 给下一条事实，否则支持分类器会把正确回答判成引用错位。
            leading_ids = tuple(dict.fromkeys(_CITATION_RE.findall(leading.group(1))))
            previous = claims[-1]
            claims[-1] = AnswerClaim(
                index=previous.index,
                text=previous.text,
                citation_ids=tuple(
                    dict.fromkeys((*previous.citation_ids, *leading_ids))
                ),
            )
            fragment = leading.group(2).strip()
            if not fragment:
                continue
        if _CONTROLLED_NOTICE_RE.match(fragment):
            continue
        if _STRUCTURAL_FRAGMENT_RE.match(fragment) or _BOLD_HEADING_RE.match(fragment):
            # Markdown 标题、分隔线和表格分隔行是结构，不是需要论文证据
            # 支持的事实主张。若将它们计入分母，正常的概览回答会被误判
            # 为“引用覆盖不足”。
            continue
        if _TABLE_ROW_RE.match(fragment) and match_index + 1 < len(matches):
            next_fragment = matches[match_index + 1].group(0).strip()
            if _TABLE_SEPARATOR_RE.match(next_fragment):
                # Markdown 表头由紧随其后的分隔行确定；数据行仍会继续接受逐条引用检查。
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


def retain_cited_answer_claims(
    answer: str,
    citations: list[CitationClaim],
    evidence: list[Evidence],
    *,
    allowed_claim_indices: set[int] | None = None,
) -> tuple[str, list[CitationClaim]]:
    """只保留带有本轮合法证据的事实主张，生成可再次核验的紧凑稿。

    模型在引用修复后偶尔仍会漏掉少量句末引用。此处不猜测证据、也不
    自动给无引用句补来源，而是删除这些句子，并保留原有合法引用关系。
    返回结果仍须经过语义支持分类器，不能绕过答案支持门禁。
    """

    evidence_ids = {item.chunk_id for item in evidence}
    citations_by_id = {
        item.chunk_id: item for item in citations if item.chunk_id in evidence_ids
    }
    retained: list[tuple[AnswerClaim, tuple[str, ...]]] = []
    for claim in extract_answer_claims(answer):
        if allowed_claim_indices is not None and claim.index not in allowed_claim_indices:
            continue
        source_ids = tuple(
            chunk_id for chunk_id in claim.citation_ids if chunk_id in citations_by_id
        )
        if source_ids:
            retained.append((claim, source_ids))

    if not retained:
        return "", []

    lines = ["### 已核验要点"]
    used_ids: list[str] = []
    for claim, source_ids in retained:
        used_ids.extend(source_ids)
        markers = "".join(f"[chunk:{chunk_id}]" for chunk_id in source_ids)
        visible_text = _MALFORMED_LEADING_EMPHASIS_RE.sub(r"\1", claim.text)
        lines.append(f"- {visible_text.rstrip('。！？!?；; ')} {markers}。")

    unique_ids = list(dict.fromkeys(used_ids))
    return "\n".join(lines), [citations_by_id[chunk_id] for chunk_id in unique_ids]


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
    supported_indices = tuple(
        sorted(
            {
                index
                for index in semantic_support.supported_claim_indices
                if 1 <= index <= claim_count
            }
        )
    )
    supported = bool(
        semantic_support.supported
        and semantic_confidence >= policy.min_model_support_confidence
    )
    if supported and not supported_indices:
        supported_indices = tuple(range(1, claim_count + 1))
    supported_claim_count = claim_count if supported else len(supported_indices)
    return AnswerSupport(
        supported,
        round(semantic_confidence, 6),
        semantic_support.reason_code
        if supported or not semantic_support.supported
        else "support_confidence_too_low",
        claim_count,
        cited_claims,
        supported_claim_count,
        citation_coverage,
        supported_claim_count / claim_count,
        supported_indices,
    )
