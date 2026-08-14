"""生产检索的确定性增强组件。

本模块不访问数据库或模型，线上 SQL 检索与离线评测共用同一组规则：

- 判断弱结果是否需要查询改写；
- 在多论文范围内按论文轮转候选，避免单篇占满 Top-K；
- 生成短句窗，供可选 Cross-Encoder 重排；
- 构造带论文与页级上下文的 Embedding 输入。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from .citations import Evidence
from .retrieval_quality import lexical_coverage

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_TECHNICAL_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]*|\d+(?:\.\d+)*")
_BROAD_INTENT_RE = re.compile(
    r"比较|对比|区别|差异|共同点|局限|限制|方法|实验|结果|趋势|演进|"
    r"\b(?:compare|contrast|difference|limitation|method|experiment|result|trend)\b",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+(?=[A-Z0-9(])|\n+")


@dataclass(frozen=True)
class RewriteDecision:
    required: bool
    reasons: tuple[str, ...]
    lexical_coverage: float
    score_gap: float | None


@dataclass(frozen=True)
class SentenceWindow:
    text: str
    start_sentence: int
    end_sentence: int
    token_count: int


class RerankScorer(Protocol):
    def score(self, query: str, documents: Sequence[str]) -> Sequence[float]: ...


class MultiGranularLexicalScorer:
    """以完整句窗、技术实体和字符片段进行确定性候选重排。"""

    @staticmethod
    def _terms(text: str) -> tuple[str, ...]:
        normalized = " ".join(text.casefold().split())
        latin = re.findall(r"[a-z0-9]+(?:[-_./][a-z0-9]+)*", normalized)
        cjk_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
        cjk = [
            run[index : index + 2]
            for run in cjk_runs
            for index in range(max(1, len(run) - 1))
        ]
        return tuple((*latin, *cjk))

    def score(self, query: str, documents: Sequence[str]) -> Sequence[float]:
        query_terms = self._terms(query)
        if not query_terms:
            return [0.0] * len(documents)
        document_terms = [self._terms(document) for document in documents]
        document_frequency = Counter(
            term for terms in document_terms for term in set(terms)
        )
        total_documents = max(1, len(documents))
        weighted_query = Counter(query_terms)

        def weight(term: str) -> float:
            return math.log((total_documents + 1) / (document_frequency[term] + 1)) + 1

        query_weight = sum(count * weight(term) for term, count in weighted_query.items())
        technical = {token.casefold() for token in technical_tokens(query)}
        normalized_query = " ".join(query.casefold().split())
        scores: list[float] = []
        for document, terms in zip(documents, document_terms):
            counts = Counter(terms)
            matched_weight = sum(
                min(count, counts.get(term, 0)) * weight(term)
                for term, count in weighted_query.items()
            )
            coverage = matched_weight / query_weight if query_weight else 0.0
            normalized_document = " ".join(document.casefold().split())
            phrase_bonus = float(
                bool(normalized_query and normalized_query in normalized_document)
            )
            technical_bonus = (
                sum(token in normalized_document for token in technical) / len(technical)
                if technical
                else 0.0
            )
            density = min(1.0, sum(counts.get(term, 0) for term in weighted_query) / 12)
            scores.append(
                round(
                    min(
                        1.0,
                        0.62 * coverage
                        + 0.18 * technical_bonus
                        + 0.12 * phrase_bonus
                        + 0.08 * density,
                    ),
                    8,
                )
            )
        return scores


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*|[^\s]", text)


def technical_tokens(text: str) -> tuple[str, ...]:
    """保留缩写、数字和英文实体，供改写提示做不可丢失约束。"""

    result: list[str] = []
    for token in _TECHNICAL_TOKEN_RE.findall(text):
        normalized = token.strip()
        if normalized and normalized.casefold() not in {item.casefold() for item in result}:
            result.append(normalized)
    return tuple(result[:16])


def assess_rewrite_need(
    query: str,
    candidates: Sequence[Evidence],
    *,
    min_lexical_coverage: float = 0.18,
    ambiguous_score_gap: float = 0.002,
) -> RewriteDecision:
    """根据检索信号触发补充查询，而不是只在零结果时触发。"""

    reasons: list[str] = []
    if not candidates:
        reasons.append("no_candidates")
        if _BROAD_INTENT_RE.search(query):
            reasons.append("broad_or_comparison_intent")
        return RewriteDecision(True, tuple(reasons), 0.0, None)
    coverage = max(lexical_coverage(query, item.text) for item in candidates[:5])
    if coverage < min_lexical_coverage:
        reasons.append("low_lexical_coverage")
    score_gap: float | None = None
    if len(candidates) >= 2:
        score_gap = abs(float(candidates[0].retrieval_score) - float(candidates[1].retrieval_score))
        if score_gap < ambiguous_score_gap:
            reasons.append("ambiguous_ranking")
    query_has_cjk = bool(_CJK_RE.search(query))
    evidence_has_latin = any(_LATIN_RE.search(item.text) for item in candidates[:5])
    evidence_has_cjk = any(_CJK_RE.search(item.text) for item in candidates[:5])
    if query_has_cjk and evidence_has_latin and not evidence_has_cjk:
        reasons.append("cross_language")
    if _BROAD_INTENT_RE.search(query):
        reasons.append("broad_or_comparison_intent")
    return RewriteDecision(
        required=bool(reasons),
        reasons=tuple(dict.fromkeys(reasons)),
        lexical_coverage=round(coverage, 6),
        score_gap=None if score_gap is None else round(score_gap, 8),
    )


def balance_evidence_by_paper(
    candidates: Sequence[Evidence],
    *,
    paper_ids: Sequence[str],
    limit: int,
    per_paper_limit: int,
) -> list[Evidence]:
    """先让每篇论文获得一个位置，再按各自排名逐轮补齐。"""

    if limit <= 0 or per_paper_limit <= 0:
        raise ValueError("limit 与 per_paper_limit 必须为正数")
    scope = list(dict.fromkeys(str(value) for value in paper_ids if str(value)))
    allowed = set(scope)
    grouped: dict[str, list[Evidence]] = {paper_id: [] for paper_id in scope}
    seen_chunks: set[str] = set()
    seen_pages: set[tuple[str, int]] = set()
    for item in candidates:
        page_key = (item.paper_id, item.physical_page)
        if item.paper_id not in allowed or item.chunk_id in seen_chunks or page_key in seen_pages:
            continue
        seen_chunks.add(item.chunk_id)
        seen_pages.add(page_key)
        grouped[item.paper_id].append(item)
    selected: list[Evidence] = []
    round_index = 0
    while len(selected) < limit:
        added = False
        for paper_id in scope:
            items = grouped.get(paper_id, [])
            if round_index < min(len(items), per_paper_limit):
                selected.append(items[round_index])
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
        round_index += 1
    return selected


def merge_paper_subquery_evidence(
    candidates: Sequence[Evidence],
    *,
    paper_ids: Sequence[str],
    limit: int,
) -> list[Evidence]:
    """按论文内页排序后，以“前三篇各 1 + 全局剩余”合并跨论文 Top-K。

    K=5 且 scope 为三篇论文时，配额严格为 1+1+1+2。保底位只覆盖服务端
    冻结顺序中的前三篇；剩余位置按各论文内去重后的分数统一竞争。
    """

    if limit <= 0:
        raise ValueError("limit 必须为正数")
    scope = list(dict.fromkeys(str(value) for value in paper_ids if str(value)))
    scope_order = {paper_id: index for index, paper_id in enumerate(scope)}
    grouped: dict[str, list[Evidence]] = {paper_id: [] for paper_id in scope}
    seen_pages: set[tuple[str, int]] = set()
    for item in candidates:
        page_key = (item.paper_id, item.physical_page)
        if item.paper_id not in grouped or page_key in seen_pages:
            continue
        seen_pages.add(page_key)
        grouped[item.paper_id].append(item)
    for paper_id in scope:
        grouped[paper_id].sort(
            key=lambda item: (-item.retrieval_score, item.physical_page, item.chunk_id)
        )

    selected: list[Evidence] = []
    selected_chunks: set[str] = set()
    for paper_id in scope[: min(3, limit)]:
        if grouped[paper_id]:
            item = grouped[paper_id][0]
            selected.append(item)
            selected_chunks.add(item.chunk_id)
    residual = sorted(
        (
            item
            for paper_id in scope
            for item in grouped[paper_id]
            if item.chunk_id not in selected_chunks
        ),
        key=lambda item: (
            -item.retrieval_score,
            scope_order[item.paper_id],
            item.physical_page,
            item.chunk_id,
        ),
    )
    selected.extend(residual[: max(0, limit - len(selected))])
    return selected[:limit]


def sentence_windows(
    text: str,
    *,
    target_tokens: int = 200,
    min_tokens: int = 160,
    max_tokens: int = 220,
) -> tuple[SentenceWindow, ...]:
    """按完整句子构造 160～220 Token 的重排窗口。"""

    if not 1 <= min_tokens <= target_tokens <= max_tokens:
        raise ValueError("句窗 Token 范围必须满足 1 <= min <= target <= max")
    sentences: list[str] = []
    for item in _SENTENCE_BOUNDARY_RE.split(text):
        rendered = item.strip()
        if not rendered:
            continue
        tokens = _tokens(rendered)
        if len(tokens) <= max_tokens:
            sentences.append(rendered)
            continue
        # 公式、表格行或 OCR 文本有时没有句号。此时只能在 Token 上限处确定性
        # 降级，避免把超长输入交给重排模型；正常段落仍只在完整句子边界切分。
        stride = max(1, max_tokens - min(32, max_tokens // 5))
        for start in range(0, len(tokens), stride):
            window_tokens = tokens[start : start + max_tokens]
            if window_tokens:
                sentences.append(" ".join(window_tokens))
            if start + max_tokens >= len(tokens):
                break
    if not sentences:
        return ()
    windows: list[SentenceWindow] = []
    start = 0
    while start < len(sentences):
        current: list[str] = []
        count = 0
        end = start
        while end < len(sentences):
            sentence_count = len(_tokens(sentences[end]))
            if current and count + sentence_count > max_tokens:
                break
            current.append(sentences[end])
            count += sentence_count
            end += 1
            if count >= target_tokens:
                break
        if not current:
            current = [sentences[start]]
            count = len(_tokens(sentences[start]))
            end = start + 1
        # 尾部过短时向前吸收完整句子。
        if count < min_tokens and windows:
            previous_start = max(0, start - 2)
            merged = sentences[previous_start:end]
            merged_count = len(_tokens(" ".join(merged)))
            if merged_count <= max_tokens:
                current = merged
                count = merged_count
                start = previous_start
        rendered = " ".join(current).strip()
        candidate = SentenceWindow(rendered, start, end, count)
        if not windows or candidate.text != windows[-1].text:
            windows.append(candidate)
        if end >= len(sentences):
            break
        # 只重叠最后一个完整句子。
        start = max(start + 1, end - 1)
    return tuple(windows)


def rerank_evidence_by_sentence_windows(
    query: str,
    candidates: Sequence[Evidence],
    scorer: RerankScorer,
    *,
    limit: int,
    rrf_weight: float = 0.35,
    document_texts: Sequence[str] | None = None,
    channel_name: str = "sentence_reranker",
) -> list[Evidence]:
    """以每页最高句窗分数融合原 RRF 分数，返回稳定页级排名。"""

    if limit <= 0 or not 0.0 <= rrf_weight <= 1.0:
        raise ValueError("重排参数无效")
    documents: list[str] = []
    owners: list[int] = []
    if document_texts is not None and len(document_texts) != len(candidates):
        raise ValueError("重排页文本数量与候选数量不一致")
    for index, item in enumerate(candidates):
        source_text = document_texts[index] if document_texts is not None else item.text
        windows = sentence_windows(source_text)
        rendered = [window.text for window in windows] or [source_text]
        documents.extend(rendered)
        owners.extend([index] * len(rendered))
    if not documents:
        return []
    scores = list(scorer.score(query, documents))
    if len(scores) != len(documents):
        raise ValueError("重排模型返回的分数数量与句窗不一致")
    page_scores = [float("-inf")] * len(candidates)
    for owner, score in zip(owners, scores):
        page_scores[owner] = max(page_scores[owner], float(score))
    rrf_values = [float(item.retrieval_score) for item in candidates]
    rrf_min = min(rrf_values, default=0.0)
    rrf_max = max(rrf_values, default=0.0)
    scored: list[tuple[float, Evidence]] = []
    for item, rerank_score, rrf_score in zip(candidates, page_scores, rrf_values):
        normalized_rrf = (rrf_score - rrf_min) / (rrf_max - rrf_min) if rrf_max > rrf_min else 1.0
        fused = (1.0 - rrf_weight) * rerank_score + rrf_weight * normalized_rrf
        scored.append(
            (
                fused,
                replace(
                    item,
                    retrieval_score=fused,
                    retrieval_channels=tuple(
                        dict.fromkeys((*item.retrieval_channels, channel_name))
                    ),
                    channel_scores=tuple(
                        (*item.channel_scores, (channel_name, rerank_score))
                    ),
                ),
            )
        )
    return [
        item
        for _score, item in sorted(
            scored,
            key=lambda pair: (
                -pair[0],
                pair[1].paper_id,
                pair[1].physical_page,
                pair[1].chunk_id,
            ),
        )[:limit]
    ]


def infer_section_title(chunk_text: str) -> str:
    """从 Chunk 开头提取保守章节线索；不确定时返回空字符串。"""

    first = next((line.strip() for line in chunk_text.splitlines() if line.strip()), "")
    normalized = " ".join(first.split())
    if not normalized or len(normalized) > 140:
        return ""
    if re.match(
        r"^(?:\d+(?:\.\d+){0,3}[.)]?\s+)?(?:abstract|introduction|background|"
        r"related work|method(?:ology)?|experiment(?:s)?|result(?:s)?|discussion|"
        r"conclusion|limitation(?:s)?|appendix|摘要|引言|背景|相关工作|方法|实验|"
        r"结果|讨论|结论|局限|附录)\b",
        normalized,
        re.IGNORECASE,
    ):
        return normalized
    return ""


def contextual_embedding_text(
    *,
    paper_title: str,
    physical_page: int,
    chunk_text: str,
    section_title: str | None = None,
) -> str:
    """构造索引输入；引用与界面仍使用未改写的 ``chunk_text``。"""

    if physical_page < 1:
        raise ValueError("物理页码必须从 1 开始")
    section = " ".join((section_title or infer_section_title(chunk_text)).split())
    parts = [
        f"论文标题：{' '.join(paper_title.split()) or '未识别'}",
        *([f"章节：{section}"] if section else []),
        f"物理页：{physical_page}",
        "正文：",
        chunk_text.strip(),
    ]
    return "\n".join(parts)
