"""无需模型密钥的 RAG 检索基线、拒答校准与可复现实验报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .evaluation import (
    CitationPrediction,
    EvaluationCase,
    EvaluationPrediction,
    RetrievedEvidencePrediction,
    evaluate,
)
from .evaluation_dataset import read_frozen_cases, read_manifest, validate_dataset
from .rag.chunking import PageChunk, PageText, chunk_pages
from .rag.citations import Evidence
from .rag.retrieval_quality import (
    EvidenceQuality,
    assess_evidence,
    deduplicate_evidence_by_page,
)
from .rag.rrf import RankedHit, reciprocal_rank_fusion

_WORD_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "do",
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
    "the",
    "this",
    "to",
    "used",
    "what",
    "which",
    "with",
}


@dataclass(frozen=True)
class ScoredChunk:
    chunk: PageChunk
    score: float


@dataclass(frozen=True)
class QueryRanking:
    hits: list[ScoredChunk]
    confidence: float
    quality: EvidenceQuality | None = None


@dataclass(frozen=True)
class SearchWindow:
    id: str
    chunk: PageChunk
    terms: tuple[str, ...]


def _tokens(text: str) -> list[str]:
    return [token for token in _WORD_RE.findall(text.casefold()) if token not in _STOPWORDS]


def _hash_feature(value: str, dimensions: int) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dimensions


def _raw_features(text: str, *, dimensions: int) -> Counter[int]:
    words = _tokens(text)
    features: Counter[int] = Counter()
    for word in words:
        features[_hash_feature(f"w:{word}", dimensions)] += 2
    for left, right in zip(words, words[1:]):
        features[_hash_feature(f"b:{left}:{right}", dimensions)] += 1
    compact = " ".join(words)
    for index in range(max(0, len(compact) - 2)):
        features[_hash_feature(f"c:{compact[index : index + 3]}", dimensions)] += 1
    return features


def _weighted_vector(
    counts: Counter[int], *, idf: dict[int, float]
) -> tuple[dict[int, float], float]:
    vector = {
        feature: (1.0 + math.log(count)) * idf.get(feature, 1.0)
        for feature, count in counts.items()
    }
    norm = math.sqrt(sum(value * value for value in vector.values()))
    return vector, norm


def _cosine(
    left: dict[int, float], left_norm: float, right: dict[int, float], right_norm: float
) -> float:
    if not left_norm or not right_norm:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items()) / (
        left_norm * right_norm
    )


class OfflineRetrievalIndex:
    """纯 Python 检索器；哈希向量仅作零模型下限，不冒充语义嵌入。"""

    def __init__(self, chunks: list[PageChunk], *, dimensions: int = 8192) -> None:
        if not chunks:
            raise ValueError("索引至少需要一个 Chunk")
        if dimensions <= 0:
            raise ValueError("dimensions 必须为正数")
        self.chunks = chunks
        self.dimensions = dimensions
        self.by_paper: dict[str, list[PageChunk]] = defaultdict(list)
        self.by_id = {chunk.id: chunk for chunk in chunks}
        for chunk in chunks:
            self.by_paper[chunk.paper_id].append(chunk)

        self.windows_by_paper: dict[str, list[SearchWindow]] = defaultdict(list)
        for chunk in chunks:
            terms = _tokens(chunk.text)
            step = 72
            for window_index, start in enumerate(range(0, len(terms), step)):
                window_terms = tuple(terms[start : start + 96])
                if not window_terms:
                    break
                self.windows_by_paper[chunk.paper_id].append(
                    SearchWindow(
                        id=f"{chunk.id}:w{window_index}",
                        chunk=chunk,
                        terms=window_terms,
                    )
                )
                if start + 96 >= len(terms):
                    break

        raw_by_id = {chunk.id: _raw_features(chunk.text, dimensions=dimensions) for chunk in chunks}
        document_frequency: Counter[int] = Counter()
        for features in raw_by_id.values():
            document_frequency.update(features.keys())
        document_count = len(chunks)
        self.idf = {
            feature: math.log((document_count + 1) / (frequency + 1)) + 1
            for feature, frequency in document_frequency.items()
        }
        self.vectors: dict[str, tuple[dict[int, float], float]] = {
            chunk_id: _weighted_vector(features, idf=self.idf)
            for chunk_id, features in raw_by_id.items()
        }

    @classmethod
    def from_pdf_dir(
        cls,
        manifest_path: Path,
        pdf_dir: Path,
        *,
        target_tokens: int,
        overlap_tokens: int,
    ) -> OfflineRetrievalIndex:
        try:
            import fitz
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("离线 PDF 评测需要 PyMuPDF") from exc

        manifest = read_manifest(manifest_path)
        pages: list[PageText] = []
        for paper in manifest.papers:
            with fitz.open(pdf_dir / paper.filename) as document:
                pages.extend(
                    PageText(
                        paper_id=paper.id,
                        physical_page=page_number + 1,
                        text=document.load_page(page_number).get_text("text"),
                    )
                    for page_number in range(document.page_count)
                )
        return cls(
            chunk_pages(
                pages,
                target_tokens=target_tokens,
                overlap_tokens=overlap_tokens,
            )
        )

    def _scope(self, paper_ids: Iterable[str]) -> list[PageChunk]:
        return [chunk for paper_id in paper_ids for chunk in self.by_paper.get(paper_id, [])]

    def hashing_vector(self, query: str, paper_ids: list[str], *, limit: int) -> QueryRanking:
        query_vector, query_norm = _weighted_vector(
            _raw_features(query, dimensions=self.dimensions), idf=self.idf
        )
        scored = []
        for chunk in self._scope(paper_ids):
            vector, norm = self.vectors[chunk.id]
            score = _cosine(query_vector, query_norm, vector, norm)
            if score > 0:
                scored.append(ScoredChunk(chunk, score))
        hits = sorted(scored, key=lambda hit: (-hit.score, hit.chunk.id))[:limit]
        return QueryRanking(hits, hits[0].score if hits else 0.0)

    def bm25(self, query: str, paper_ids: list[str], *, limit: int) -> QueryRanking:
        chunks = self._scope(paper_ids)
        query_terms = Counter(_tokens(query))
        tokenized = {chunk.id: _tokens(chunk.text) for chunk in chunks}
        document_frequency: Counter[str] = Counter()
        for terms in tokenized.values():
            document_frequency.update(set(terms))
        average_length = (
            sum(len(terms) for terms in tokenized.values()) / len(chunks) if chunks else 0
        )
        k1, b = 1.5, 0.75
        scored: list[ScoredChunk] = []
        for chunk in chunks:
            terms = tokenized[chunk.id]
            frequencies = Counter(terms)
            score = 0.0
            for term, query_frequency in query_terms.items():
                frequency = frequencies[term]
                if not frequency:
                    continue
                idf = math.log(
                    1
                    + (len(chunks) - document_frequency[term] + 0.5)
                    / (document_frequency[term] + 0.5)
                )
                denominator = frequency + k1 * (1 - b + b * len(terms) / max(average_length, 1))
                score += query_frequency * idf * frequency * (k1 + 1) / denominator
            if score > 0:
                scored.append(ScoredChunk(chunk, score))
        hits = sorted(scored, key=lambda hit: (-hit.score, hit.chunk.id))[:limit]
        confidence = self._lexical_confidence(query_terms, hits)
        return QueryRanking(hits, confidence)

    def window_bm25(self, query: str, paper_ids: list[str], *, limit: int) -> QueryRanking:
        windows = [
            window for paper_id in paper_ids for window in self.windows_by_paper.get(paper_id, [])
        ]
        query_terms = Counter(_tokens(query))
        document_frequency: Counter[str] = Counter()
        for window in windows:
            document_frequency.update(set(window.terms))
        average_length = (
            sum(len(window.terms) for window in windows) / len(windows) if windows else 0
        )
        k1, b = 1.5, 0.75
        best_by_page: dict[tuple[str, int], ScoredChunk] = {}
        for window in windows:
            frequencies = Counter(window.terms)
            score = 0.0
            for term, query_frequency in query_terms.items():
                frequency = frequencies[term]
                if not frequency:
                    continue
                idf = math.log(
                    1
                    + (len(windows) - document_frequency[term] + 0.5)
                    / (document_frequency[term] + 0.5)
                )
                denominator = frequency + k1 * (
                    1 - b + b * len(window.terms) / max(average_length, 1)
                )
                score += query_frequency * idf * frequency * (k1 + 1) / denominator
            if score <= 0:
                continue
            key = (window.chunk.paper_id, window.chunk.physical_page)
            current = best_by_page.get(key)
            if current is None or score > current.score:
                best_by_page[key] = ScoredChunk(window.chunk, score)
        hits = sorted(best_by_page.values(), key=lambda hit: (-hit.score, hit.chunk.id))[:limit]
        confidence = self._lexical_confidence(query_terms, hits)
        return QueryRanking(hits, confidence)

    @staticmethod
    def _lexical_confidence(query_terms: Counter[str], hits: list[ScoredChunk]) -> float:
        if not hits or not query_terms:
            return 0.0
        top_terms = set(_tokens(hits[0].chunk.text))
        matched = sum(count for term, count in query_terms.items() if term in top_terms)
        return matched / sum(query_terms.values())

    def fused(
        self,
        query: str,
        paper_ids: list[str],
        *,
        limit: int,
        page_dedup: bool = False,
        neighbor_weight: float = 0.0,
        scope_diversity: bool = False,
        window_channel: bool = False,
    ) -> QueryRanking:
        channel_limit = max(limit * 8, 40)
        vector = self.hashing_vector(query, paper_ids, limit=channel_limit)
        keyword = self.bm25(query, paper_ids, limit=channel_limit)
        window = (
            self.window_bm25(query, paper_ids, limit=channel_limit)
            if window_channel
            else QueryRanking([], 0.0)
        )
        vector_hits = [RankedHit(hit.chunk.id, hit.score, hit.chunk) for hit in vector.hits]
        keyword_hits = [RankedHit(hit.chunk.id, hit.score, hit.chunk) for hit in keyword.hits]
        window_hits = [RankedHit(hit.chunk.id, hit.score, hit.chunk) for hit in window.hits]
        fused = reciprocal_rank_fusion(
            [channel for channel in (vector_hits, keyword_hits, window_hits) if channel],
            limit=channel_limit,
        )
        hits = [
            ScoredChunk(hit.payload, hit.score)
            for hit in fused
            if isinstance(hit.payload, PageChunk)
        ]
        vector_score = {hit.chunk.id: hit.score for hit in vector.hits}
        keyword_score = {hit.chunk.id: hit.score for hit in keyword.hits}
        window_score = {hit.chunk.id: hit.score for hit in window.hits}

        def evidence_for(hit: ScoredChunk) -> Evidence:
            channels = tuple(
                name
                for name, scores in (
                    ("keyword", keyword_score),
                    ("vector", vector_score),
                    ("window", window_score),
                )
                if hit.chunk.id in scores
            )
            scores = tuple(
                (name, values[hit.chunk.id])
                for name, values in (
                    ("keyword", keyword_score),
                    ("vector", vector_score),
                    ("window", window_score),
                )
                if hit.chunk.id in values
            )
            return Evidence(
                chunk_id=hit.chunk.id,
                paper_id=hit.chunk.paper_id,
                paper_title=hit.chunk.paper_id,
                physical_page=hit.chunk.physical_page,
                text=hit.chunk.text,
                retrieval_score=hit.score,
                retrieval_channels=channels,
                channel_scores=scores,
            )

        quality_evidence: list[Evidence]
        if page_dedup and not neighbor_weight:
            quality_evidence = deduplicate_evidence_by_page(
                [evidence_for(hit) for hit in hits], limit=channel_limit
            )
            hits = [
                ScoredChunk(self.by_id[item.chunk_id], item.retrieval_score)
                for item in quality_evidence
            ]
        elif page_dedup:
            hits = self._rank_pages(hits, neighbor_weight=neighbor_weight)
            quality_evidence = [evidence_for(hit) for hit in hits]
        else:
            quality_evidence = [evidence_for(hit) for hit in hits]
        if scope_diversity and len(paper_ids) > 1:
            hits = self._diversify_scope(hits, paper_ids=paper_ids, limit=limit)
        hits = hits[:limit]
        quality_by_chunk = {item.chunk_id: item for item in quality_evidence}
        final_evidence = [
            quality_by_chunk[hit.chunk.id] for hit in hits if hit.chunk.id in quality_by_chunk
        ]
        quality = assess_evidence(query, final_evidence)
        return QueryRanking(hits, quality.confidence, quality)

    @staticmethod
    def _rank_pages(hits: list[ScoredChunk], *, neighbor_weight: float) -> list[ScoredChunk]:
        best_by_page: dict[tuple[str, int], ScoredChunk] = {}
        for hit in hits:
            key = (hit.chunk.paper_id, hit.chunk.physical_page)
            best_by_page.setdefault(key, hit)
        rescored: list[ScoredChunk] = []
        for (paper_id, page), hit in best_by_page.items():
            neighbor_score = max(
                (
                    best_by_page[(paper_id, neighbor)].score
                    for neighbor in (page - 1, page + 1)
                    if (paper_id, neighbor) in best_by_page
                ),
                default=0.0,
            )
            rescored.append(ScoredChunk(hit.chunk, hit.score + neighbor_weight * neighbor_score))
        return sorted(rescored, key=lambda hit: (-hit.score, hit.chunk.id))

    @staticmethod
    def _diversify_scope(
        hits: list[ScoredChunk], *, paper_ids: list[str], limit: int
    ) -> list[ScoredChunk]:
        by_paper: dict[str, list[ScoredChunk]] = defaultdict(list)
        for hit in hits:
            by_paper[hit.chunk.paper_id].append(hit)
        diversified: list[ScoredChunk] = []
        rank = 0
        while len(diversified) < limit:
            added = False
            for paper_id in paper_ids:
                paper_hits = by_paper.get(paper_id, [])
                if rank < len(paper_hits):
                    diversified.append(paper_hits[rank])
                    added = True
                    if len(diversified) == limit:
                        break
            if not added:
                break
            rank += 1
        return diversified


def calibrate_abstention(
    cases: list[EvaluationCase], confidence_by_id: dict[str, float]
) -> dict[str, float | int]:
    dev_cases = [case for case in cases if case.split == "dev"]
    if not dev_cases or not any(case.answerable for case in dev_cases):
        raise ValueError("拒答阈值需要包含可回答问题的 dev 集")
    if not any(not case.answerable for case in dev_cases):
        raise ValueError("拒答阈值需要包含不可回答问题的 dev 集")
    values = sorted({confidence_by_id[case.id] for case in dev_cases})
    candidates = [0.0, *(value + 1e-12 for value in values), 1.0 + 1e-12]
    best: tuple[tuple[float, float, float], float, dict[str, float | int]] | None = None
    answerable_total = sum(case.answerable for case in dev_cases)
    unanswerable_total = len(dev_cases) - answerable_total
    for threshold in candidates:
        correct_answerable = sum(
            case.answerable and confidence_by_id[case.id] >= threshold for case in dev_cases
        )
        correct_unanswerable = sum(
            not case.answerable and confidence_by_id[case.id] < threshold for case in dev_cases
        )
        answerable_recall = correct_answerable / answerable_total
        unanswerable_recall = correct_unanswerable / unanswerable_total
        balanced_accuracy = (answerable_recall + unanswerable_recall) / 2
        stats: dict[str, float | int] = {
            "threshold": threshold,
            "dev_answerable_correct": correct_answerable,
            "dev_answerable_total": answerable_total,
            "dev_unanswerable_correct": correct_unanswerable,
            "dev_unanswerable_total": unanswerable_total,
            "dev_balanced_accuracy": balanced_accuracy,
        }
        rank = (unanswerable_recall, balanced_accuracy, answerable_recall)
        if best is None or rank > best[0]:
            best = (rank, threshold, stats)
    assert best is not None
    return best[2]


def _prediction(
    case: EvaluationCase,
    ranking: QueryRanking,
    *,
    latency_ms: int,
    threshold: float | None,
    quality_gate: bool = False,
) -> EvaluationPrediction:
    abstained = (threshold is not None and ranking.confidence < threshold) or (
        quality_gate and (ranking.quality is None or ranking.quality.grade == "insufficient")
    )
    evidence = [
        RetrievedEvidencePrediction(
            chunk_id=hit.chunk.id,
            paper_id=hit.chunk.paper_id,
            physical_page=hit.chunk.physical_page,
            score=hit.score,
        )
        for hit in ranking.hits
    ]
    citations = []
    if evidence and not abstained:
        top = evidence[0]
        citations.append(
            CitationPrediction(
                chunk_id=top.chunk_id,
                paper_id=top.paper_id,
                physical_page=top.physical_page,
            )
        )
    answer = "" if abstained or not ranking.hits else ranking.hits[0].chunk.text[:1600]
    return EvaluationPrediction(
        case_id=case.id,
        answer=answer,
        abstained=abstained,
        retrieved_evidence=evidence,
        citations=citations,
        confidence=ranking.confidence,
        latency_ms=latency_ms,
    )


def run_variants(
    index: OfflineRetrievalIndex, cases: list[EvaluationCase], *, k: int
) -> tuple[dict[str, list[EvaluationPrediction]], dict[str, float | int]]:
    rankings: dict[str, dict[str, QueryRanking]] = defaultdict(dict)
    latencies: dict[str, dict[str, int]] = defaultdict(dict)
    variants = {
        "hashing_vector": lambda case: index.hashing_vector(case.query, case.paper_ids, limit=k),
        "bm25": lambda case: index.bm25(case.query, case.paper_ids, limit=k),
        "rrf": lambda case: index.fused(case.query, case.paper_ids, limit=k),
        "rrf_page": lambda case: index.fused(case.query, case.paper_ids, limit=k, page_dedup=True),
        "rrf_page_neighbor": lambda case: index.fused(
            case.query,
            case.paper_ids,
            limit=k,
            page_dedup=True,
            neighbor_weight=0.15,
        ),
        "rrf_page_scope": lambda case: index.fused(
            case.query,
            case.paper_ids,
            limit=k,
            page_dedup=True,
            scope_diversity=True,
        ),
        "window_bm25": lambda case: index.window_bm25(case.query, case.paper_ids, limit=k),
        "rrf_page_window": lambda case: index.fused(
            case.query,
            case.paper_ids,
            limit=k,
            page_dedup=True,
            window_channel=True,
        ),
    }
    for name, retrieve in variants.items():
        for case in cases:
            started = time.perf_counter()
            rankings[name][case.id] = retrieve(case)
            latencies[name][case.id] = max(0, round((time.perf_counter() - started) * 1000))

    calibration = calibrate_abstention(
        cases,
        {case.id: rankings["rrf_page"][case.id].confidence for case in cases},
    )
    threshold = float(calibration["threshold"])
    predictions: dict[str, list[EvaluationPrediction]] = {}
    for name in variants:
        predictions[name] = [
            _prediction(
                case,
                rankings[name][case.id],
                latency_ms=latencies[name][case.id],
                threshold=None,
            )
            for case in cases
        ]
    predictions["rrf_page_refusal"] = [
        _prediction(
            case,
            rankings["rrf_page"][case.id],
            latency_ms=latencies["rrf_page"][case.id],
            threshold=threshold,
        )
        for case in cases
    ]
    predictions["rrf_page_quality_gate"] = [
        _prediction(
            case,
            rankings["rrf_page"][case.id],
            latency_ms=latencies["rrf_page"][case.id],
            threshold=None,
            quality_gate=True,
        )
        for case in cases
    ]
    return predictions, calibration


def _percent(metric: dict[str, float | int | None]) -> str:
    value = metric["value"]
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def _test_metric(result: dict, variant: str, metric: str) -> float:
    value = result["variants"][variant]["metrics"]["by_split"]["test"][metric]["value"]
    if value is None:
        raise ValueError(f"{variant}.{metric} 没有可比较的分母")
    return float(value)


def render_report(result: dict) -> str:
    rows = []
    for name, variant in result["variants"].items():
        test = variant["metrics"]["by_split"]["test"]
        rows.append(
            "| "
            + " | ".join(
                (
                    name,
                    _percent(test["retrieval_recall_at_k"]),
                    _percent(test["retrieval_mrr_at_k"]),
                    _percent(test["citation_page_accuracy"]),
                    _percent(test["citation_coverage"]),
                    _percent(test["answer_keyword_accuracy"]),
                    _percent(test["unanswerable_wrong_answer_rate"]),
                )
            )
            + " |"
        )
    calibration = result["abstention_calibration"]
    answerable_score = (
        f"{calibration['dev_answerable_correct']}/{calibration['dev_answerable_total']}"
    )
    unanswerable_score = (
        f"{calibration['dev_unanswerable_correct']}/{calibration['dev_unanswerable_total']}"
    )
    bm25_recall = _test_metric(result, "bm25", "retrieval_recall_at_k")
    rrf_recall = _test_metric(result, "rrf", "retrieval_recall_at_k")
    page_recall = _test_metric(result, "rrf_page", "retrieval_recall_at_k")
    neighbor_recall = _test_metric(result, "rrf_page_neighbor", "retrieval_recall_at_k")
    refusal_wrong = _test_metric(result, "rrf_page_refusal", "unanswerable_wrong_answer_rate")
    refusal_coverage = _test_metric(result, "rrf_page_refusal", "citation_coverage")
    quality_wrong = _test_metric(result, "rrf_page_quality_gate", "unanswerable_wrong_answer_rate")
    quality_coverage = _test_metric(result, "rrf_page_quality_gate", "citation_coverage")
    return "\n".join(
        (
            "# PaperLeaf RAG v1 离线基线",
            "",
            "本报告使用 20 篇固定 arXiv 版本、120 个冻结问题；阈值只在 dev 集拟合，",
            "下表只展示 test 集。`hashing_vector` 是无模型密钥的词/字符哈希下限，",
            "不是神经语义嵌入。关键词指标来自首个检索片段，仅作证据命中代理，",
            "不代表 LLM 回答正确率。",
            "",
            "| 方案 | Recall@5 | MRR@5 | 首引页准确率 | 引用覆盖率 | "
            "关键词代理 | 不可回答错误作答率 |",
            "|---|---:|---:|---:|---:|---:|---:|",
            *rows,
            "",
            "## 阶段结论",
            "",
            f"- BM25 → RRF：Recall@5 提升 `{(rrf_recall - bm25_recall) * 100:+.1f}` 个百分点。",
            f"- RRF → 页去重：Recall@5 提升 `{(page_recall - rrf_recall) * 100:+.1f}` 个百分点。",
            f"- 邻页加权：Recall@5 变化 `{(neighbor_recall - page_recall) * 100:+.1f}` 个百分点，"
            "因此不作为默认方案。",
            f"- 严格拒答：test 不可回答错误作答率为 `{refusal_wrong * 100:.1f}%`，"
            f"引用覆盖率为 `{refusal_coverage * 100:.1f}%`；安全性提升伴随覆盖损失。",
            f"- 线上同源质量门禁：test 不可回答错误作答率为 `{quality_wrong * 100:.1f}%`，"
            f"引用覆盖率为 `{quality_coverage * 100:.1f}%`。",
            "",
            "## 拒答阈值",
            "",
            f"- dev 阈值：`{float(calibration['threshold']):.6f}`",
            f"- dev 平衡准确率：`{float(calibration['dev_balanced_accuracy']) * 100:.1f}%`",
            f"- dev 可回答判对：`{answerable_score}`",
            f"- dev 不可回答判对：`{unanswerable_score}`",
            "",
            "## 解释边界",
            "",
            "- 评测只比较检索、物理页引用和确定性拒答，不声称衡量生成模型的完整答案质量。",
            "- PDF 原件不随仓库分发；清单固定官方下载地址、版本、SHA-256 与页数。",
            "- 延迟取决于本机，机器可读文件保留原始中位数与 p95，不跨机器横向宣传。",
            "",
        )
    )


def run_experiment(
    manifest_path: Path,
    cases_path: Path,
    pdf_dir: Path,
    *,
    k: int,
) -> tuple[dict, dict[str, list[EvaluationPrediction]]]:
    manifest = read_manifest(manifest_path)
    frozen_cases = read_frozen_cases(cases_path)
    validation = validate_dataset(manifest, frozen_cases, pdf_dir=pdf_dir)
    cases = [EvaluationCase.model_validate(case.model_dump()) for case in frozen_cases]
    index_started = time.perf_counter()
    index = OfflineRetrievalIndex.from_pdf_dir(
        manifest_path,
        pdf_dir,
        target_tokens=manifest.chunking.target_tokens,
        overlap_tokens=manifest.chunking.overlap_tokens,
    )
    index_ms = round((time.perf_counter() - index_started) * 1000)
    predictions, calibration = run_variants(index, cases, k=k)
    result = {
        "schema_version": 1,
        "dataset": validation,
        "protocol": {
            "k": k,
            "chunk_target_tokens": manifest.chunking.target_tokens,
            "chunk_overlap_tokens": manifest.chunking.overlap_tokens,
            "hash_dimensions": index.dimensions,
            "chunk_count": len(index.chunks),
            "index_build_ms": index_ms,
            "threshold_fit_split": "dev",
            "reported_split": "test",
        },
        "abstention_calibration": calibration,
        "variants": {
            name: {"metrics": evaluate(cases, variant_predictions, k=k)}
            for name, variant_predictions in predictions.items()
        },
    }
    return result, predictions


def _write_predictions(
    output_dir: Path, predictions: dict[str, list[EvaluationPrediction]]
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, records in predictions.items():
        path = output_dir / f"{name}.jsonl"
        path.write_text(
            "\n".join(record.model_dump_json(exclude_defaults=True) for record in records) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 PaperLeaf 无密钥 RAG 离线基线")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--pdf-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--predictions-dir", type=Path)
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args()
    result, predictions = run_experiment(
        args.manifest,
        args.cases,
        args.pdf_dir,
        k=args.k,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.report.write_text(render_report(result), encoding="utf-8")
    if args.predictions_dir:
        _write_predictions(args.predictions_dir, predictions)
    print(
        json.dumps(
            {
                "metrics": str(args.output),
                "report": str(args.report),
                "variants": list(result["variants"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
