"""可选的本地神经语义检索与 Cross-Encoder 重排评测。"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evaluation import EvaluationCase, EvaluationPrediction, evaluate
from .evaluation_dataset import read_frozen_cases, read_manifest, validate_dataset
from .evaluation_offline import (
    OfflineRetrievalIndex,
    QueryRanking,
    ScoredChunk,
    _prediction,
    _tokens,
)
from .rag.citations import Evidence
from .rag.retrieval_quality import assess_evidence, deduplicate_evidence_by_page
from .rag.rrf import RankedHit, reciprocal_rank_fusion

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


@dataclass(frozen=True)
class NeuralProtocol:
    embedding_model: str
    reranker_model: str | None
    candidate_limit: int
    rerank_focus_window: bool = False


def _cosine(left: Any, right: Any) -> float:
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


class NeuralRetrievalIndex:
    """在同一页级 Chunk 上比较真实 dense、RRF 与可选重排。"""

    def __init__(
        self,
        base: OfflineRetrievalIndex,
        *,
        embedding_model_name: str = DEFAULT_EMBEDDING_MODEL,
        reranker_model_name: str | None = None,
        cache_dir: Path | None = None,
        embedding_model: Any | None = None,
        reranker: Any | None = None,
    ) -> None:
        self.base = base
        self.embedding_model_name = embedding_model_name
        self.reranker_model_name = reranker_model_name
        if embedding_model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover - 由 CLI 安装项覆盖
                raise RuntimeError("神经评测需要安装 `pip install -e .[eval]`") from exc
            embedding_model = TextEmbedding(
                model_name=embedding_model_name,
                cache_dir=str(cache_dir) if cache_dir else None,
            )
        self.embedding_model = embedding_model
        vectors = list(
            self.embedding_model.passage_embed([chunk.text for chunk in self.base.chunks])
        )
        self.vector_by_id = {chunk.id: vector for chunk, vector in zip(self.base.chunks, vectors)}
        if reranker is not None:
            self.reranker = reranker
        elif reranker_model_name:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self.reranker = TextCrossEncoder(
                model_name=reranker_model_name,
                cache_dir=str(cache_dir) if cache_dir else None,
            )
        else:
            self.reranker = None

    def dense(self, query: str, paper_ids: list[str], *, limit: int) -> QueryRanking:
        query_vector = next(iter(self.embedding_model.query_embed(query)))
        hits = [
            ScoredChunk(chunk, _cosine(query_vector, self.vector_by_id[chunk.id]))
            for chunk in self.base._scope(paper_ids)
        ]
        hits = sorted(hits, key=lambda item: (-item.score, item.chunk.id))[: max(limit * 5, 40)]
        evidence = deduplicate_evidence_by_page(
            [
                Evidence(
                    chunk_id=hit.chunk.id,
                    paper_id=hit.chunk.paper_id,
                    paper_title=hit.chunk.paper_id,
                    physical_page=hit.chunk.physical_page,
                    text=hit.chunk.text,
                    retrieval_score=hit.score,
                    retrieval_channels=("vector",),
                    channel_scores=(("vector", hit.score),),
                )
                for hit in hits
            ],
            limit=limit,
        )
        quality = assess_evidence(query, evidence)
        return QueryRanking(
            [
                ScoredChunk(self.base.by_id[item.chunk_id], item.retrieval_score)
                for item in evidence
            ],
            quality.confidence,
            quality,
        )

    def hybrid(
        self,
        query: str,
        paper_ids: list[str],
        *,
        limit: int,
        rerank: bool = False,
    ) -> QueryRanking:
        candidate_limit = max(limit * 8, 40)
        dense = self.dense(query, paper_ids, limit=candidate_limit)
        keyword = self.base.bm25(query, paper_ids, limit=candidate_limit)
        dense_hits = [RankedHit(hit.chunk.id, hit.score, hit.chunk) for hit in dense.hits]
        keyword_hits = [RankedHit(hit.chunk.id, hit.score, hit.chunk) for hit in keyword.hits]
        dense_scores = {hit.id: hit.score for hit in dense_hits}
        keyword_scores = {hit.id: hit.score for hit in keyword_hits}
        fused = reciprocal_rank_fusion(
            [channel for channel in (dense_hits, keyword_hits) if channel],
            limit=candidate_limit,
        )
        candidates = deduplicate_evidence_by_page(
            [
                Evidence(
                    chunk_id=hit.payload.id,
                    paper_id=hit.payload.paper_id,
                    paper_title=hit.payload.paper_id,
                    physical_page=hit.payload.physical_page,
                    text=hit.payload.text,
                    retrieval_score=hit.score,
                    retrieval_channels=tuple(
                        name
                        for name, scores in (
                            ("keyword", keyword_scores),
                            ("vector", dense_scores),
                        )
                        if hit.id in scores
                    ),
                    channel_scores=tuple(
                        (name, scores[hit.id])
                        for name, scores in (
                            ("keyword", keyword_scores),
                            ("vector", dense_scores),
                        )
                        if hit.id in scores
                    ),
                )
                for hit in fused
                if hit.payload is not None
            ],
            limit=candidate_limit,
        )
        if rerank:
            if self.reranker is None:
                raise RuntimeError("请求了重排，但没有配置 reranker")
            scores = list(self.reranker.rerank(query, [item.text for item in candidates]))
            candidates = [
                item
                for _, item in sorted(
                    zip(scores, candidates),
                    key=lambda pair: (-float(pair[0]), pair[1].chunk_id),
                )
            ]
        evidence = candidates[:limit]
        quality = assess_evidence(query, evidence)
        return QueryRanking(
            [
                ScoredChunk(self.base.by_id[item.chunk_id], item.retrieval_score)
                for item in evidence
            ],
            quality.confidence,
            quality,
        )


class CrossEncoderRerankIndex:
    """只对页级 RRF 候选做重排，避免重复计算 dense 索引。"""

    def __init__(
        self,
        base: OfflineRetrievalIndex,
        *,
        reranker_model_name: str = DEFAULT_RERANKER_MODEL,
        cache_dir: Path | None = None,
        reranker: Any | None = None,
        candidate_limit: int = 40,
        focus_window: bool = False,
    ) -> None:
        self.base = base
        self.reranker_model_name = reranker_model_name
        self.candidate_limit = candidate_limit
        self.focus_window = focus_window
        if reranker is None:
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
            except ImportError as exc:  # pragma: no cover - 由 CLI 安装项覆盖
                raise RuntimeError("神经重排需要安装 `pip install -e .[eval]`") from exc
            reranker = TextCrossEncoder(
                model_name=reranker_model_name,
                cache_dir=str(cache_dir) if cache_dir else None,
            )
        self.reranker = reranker

    @staticmethod
    def _focus_text(query: str, text: str, *, max_characters: int = 1800) -> str:
        sentences = [
            item.strip() for item in re.split(r"(?<=[.!?。！？])\s+|\n+", text) if item.strip()
        ]
        if not sentences:
            return text[:max_characters]
        query_terms = set(_tokens(query))
        ranked: list[tuple[int, int, str]] = []
        for index, _sentence in enumerate(sentences):
            window = " ".join(sentences[max(0, index - 1) : index + 2])
            overlap = len(query_terms & set(_tokens(window)))
            ranked.append((overlap, -index, window))
        selected: list[str] = []
        size = 0
        for overlap, _, window in sorted(ranked, reverse=True):
            if not overlap and selected:
                break
            if window in selected:
                continue
            remaining = max_characters - size
            if remaining <= 0:
                break
            selected.append(window[:remaining])
            size += len(selected[-1]) + 1
        return " ".join(selected)[:max_characters] or text[:max_characters]

    def retrieve(self, query: str, paper_ids: list[str], *, limit: int) -> QueryRanking:
        candidates = self.base.fused(
            query,
            paper_ids,
            limit=self.candidate_limit,
            page_dedup=True,
        ).hits
        documents = [
            self._focus_text(query, hit.chunk.text) if self.focus_window else hit.chunk.text
            for hit in candidates
        ]
        scores = list(self.reranker.rerank(query, documents))
        reranked = [
            ScoredChunk(item.chunk, float(score))
            for score, item in sorted(
                zip(scores, candidates),
                key=lambda pair: (-float(pair[0]), pair[1].chunk.id),
            )[:limit]
        ]
        evidence = [
            Evidence(
                chunk_id=hit.chunk.id,
                paper_id=hit.chunk.paper_id,
                paper_title=hit.chunk.paper_id,
                physical_page=hit.chunk.physical_page,
                text=hit.chunk.text,
                retrieval_score=hit.score,
                retrieval_channels=("reranker",),
                channel_scores=(("reranker", hit.score),),
            )
            for hit in reranked
        ]
        quality = assess_evidence(query, evidence)
        return QueryRanking(reranked, quality.confidence, quality)


def _run_variant(
    cases: list[EvaluationCase], retrieve: Any
) -> tuple[list[EvaluationPrediction], dict[str, QueryRanking], dict[str, int]]:
    rankings: dict[str, QueryRanking] = {}
    latencies: dict[str, int] = {}
    for case in cases:
        started = time.perf_counter()
        rankings[case.id] = retrieve(case)
        latencies[case.id] = max(0, round((time.perf_counter() - started) * 1000))
    return (
        [
            _prediction(
                case,
                rankings[case.id],
                latency_ms=latencies[case.id],
                threshold=None,
            )
            for case in cases
        ],
        rankings,
        latencies,
    )


def run_neural_experiment(
    index: NeuralRetrievalIndex,
    cases: list[EvaluationCase],
    *,
    k: int,
) -> dict[str, Any]:
    variants: dict[str, list[EvaluationPrediction]] = {}
    dense_predictions, _, _ = _run_variant(
        cases, lambda case: index.dense(case.query, case.paper_ids, limit=k)
    )
    variants["bge_dense_page"] = dense_predictions
    hybrid_predictions, hybrid_rankings, hybrid_latencies = _run_variant(
        cases,
        lambda case: index.hybrid(case.query, case.paper_ids, limit=k),
    )
    variants["bge_rrf_page"] = hybrid_predictions
    variants["bge_rrf_page_quality_gate"] = [
        _prediction(
            case,
            hybrid_rankings[case.id],
            latency_ms=hybrid_latencies[case.id],
            threshold=None,
            quality_gate=True,
        )
        for case in cases
    ]
    if index.reranker is not None:
        reranked, _, _ = _run_variant(
            cases,
            lambda case: index.hybrid(case.query, case.paper_ids, limit=k, rerank=True),
        )
        variants["bge_rrf_page_rerank"] = reranked
    return {
        "variants": {
            name: {"metrics": evaluate(cases, predictions, k=k)}
            for name, predictions in variants.items()
        }
    }


def run_rerank_experiment(
    index: CrossEncoderRerankIndex,
    cases: list[EvaluationCase],
    *,
    k: int,
) -> dict[str, Any]:
    baseline, _, _ = _run_variant(
        cases,
        lambda case: index.base.fused(
            case.query,
            case.paper_ids,
            limit=k,
            page_dedup=True,
        ),
    )
    reranked, _, _ = _run_variant(
        cases,
        lambda case: index.retrieve(case.query, case.paper_ids, limit=k),
    )
    variants = {
        "rrf_page": baseline,
        (
            "rrf_page_cross_encoder_focus" if index.focus_window else "rrf_page_cross_encoder"
        ): reranked,
    }
    return {
        "variants": {
            name: {"metrics": evaluate(cases, predictions, k=k)}
            for name, predictions in variants.items()
        }
    }


def _percent(metric: dict[str, Any]) -> str:
    value = metric["value"]
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def render_report(result: dict[str, Any], protocol: NeuralProtocol) -> str:
    rows = []
    for name, value in result["variants"].items():
        test = value["metrics"]["by_split"]["test"]
        rows.append(
            f"| {name} | {_percent(test['retrieval_recall_at_k'])} | "
            f"{_percent(test['retrieval_mrr_at_k'])} | "
            f"{_percent(test['citation_page_accuracy'])} | "
            f"{_percent(test['citation_coverage'])} | "
            f"{_percent(test['unanswerable_wrong_answer_rate'])} |"
        )
    return "\n".join(
        (
            "# PaperLeaf 本地神经检索诊断",
            "",
            f"- Embedding：`{protocol.embedding_model}`",
            f"- Reranker：`{protocol.reranker_model or '未启用'}`",
            "- 数据边界：v1 test 已用于诊断，本报告不是盲测结果。",
            "",
            "| 方案 | Recall@5 | MRR@5 | 首引页准确率 | 引用覆盖率 | 不可回答错误作答率 |",
            "|---|---:|---:|---:|---:|---:|",
            *rows,
            "",
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 PaperLeaf 本地神经检索诊断")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--pdf-dir", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--reranker-model")
    parser.add_argument("--reranker-only", action="store_true")
    parser.add_argument("--rerank-focus-window", action="store_true")
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args()

    manifest = read_manifest(args.manifest)
    frozen_cases = read_frozen_cases(args.cases)
    validation = validate_dataset(manifest, frozen_cases, pdf_dir=args.pdf_dir)
    cases = [EvaluationCase.model_validate(case.model_dump()) for case in frozen_cases]
    base = OfflineRetrievalIndex.from_pdf_dir(
        args.manifest,
        args.pdf_dir,
        target_tokens=manifest.chunking.target_tokens,
        overlap_tokens=manifest.chunking.overlap_tokens,
    )
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if args.reranker_only:
        reranker_model = args.reranker_model or DEFAULT_RERANKER_MODEL
        index = CrossEncoderRerankIndex(
            base,
            reranker_model_name=reranker_model,
            cache_dir=args.cache_dir,
            focus_window=args.rerank_focus_window,
        )
        protocol = NeuralProtocol(
            "未启用",
            reranker_model,
            index.candidate_limit,
            args.rerank_focus_window,
        )
        result = run_rerank_experiment(index, cases, k=args.k)
    else:
        neural_index = NeuralRetrievalIndex(
            base,
            embedding_model_name=args.embedding_model,
            reranker_model_name=args.reranker_model,
            cache_dir=args.cache_dir,
        )
        protocol = NeuralProtocol(args.embedding_model, args.reranker_model, 40)
        result = run_neural_experiment(neural_index, cases, k=args.k)
    index_ms = round((time.perf_counter() - started) * 1000)
    result.update(
        {
            "schema_version": 1,
            "dataset": validation,
            "protocol": {
                **protocol.__dict__,
                "k": args.k,
                "chunk_count": len(base.chunks),
                "index_build_ms": index_ms,
                "reported_split": "test_diagnostic_only",
            },
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.report.write_text(render_report(result, protocol), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "report": str(args.report)}))


if __name__ == "__main__":
    main()
