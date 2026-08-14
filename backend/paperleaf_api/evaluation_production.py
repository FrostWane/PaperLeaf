"""使用线上 SQLLibrarySearch 的生产同源 RAG 评测。

脚本不导入、不删除论文，也不会静默缩小冻结 scope。评测用户必须已经拥有清单中的
全部论文，且论文使用当前 Embedding 契约完成索引；否则只输出 ``not_executed`` 预检。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import statistics
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, or_, select

from .agent.tools import LibrarySearchInput, SQLLibrarySearch
from .config import settings
from .db import get_session_factory
from .embedding_contract import configured_embedding_contract
from .evaluation import EvaluationCase, EvaluationPrediction, RetrievedEvidencePrediction, evaluate
from .evaluation_dataset import (
    EvaluationDatasetManifest,
    FrozenEvaluationCase,
    read_frozen_cases,
    read_manifest,
    validate_dataset,
)
from .model_runtime import build_model_router
from .models import Paper, PaperChunk, User
from .rag.citations import Evidence

ProductionRetriever = Callable[[LibrarySearchInput], Awaitable[list[Evidence]]]
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_TECHNICAL_RE = re.compile(r"\b(?:[A-Z]{2,}|[A-Za-z]+\d+[A-Za-z0-9-]*|\d+(?:\.\d+)*)\b")
_LAYOUT_RE = re.compile(r"(?:table|figure|equation|formula|表格|图\s*\d|公式|[=≈≤≥∑∫])", re.I)


def _evaluation_case(case: FrozenEvaluationCase) -> EvaluationCase:
    return EvaluationCase.model_validate(case.model_dump())


def _dimension_tags(case: FrozenEvaluationCase) -> tuple[str, ...]:
    tags = ["cross_paper" if len(case.paper_ids) > 1 else "single_paper"]
    if _CJK_RE.search(case.query):
        tags.append("cjk_query")
    else:
        tags.append("latin_query")
    if _TECHNICAL_RE.search(case.query):
        tags.append("technical_entity")
    anchors = " ".join(item.anchor for item in case.expected_evidence)
    anchors += " " + " ".join(
        item.anchor for group in case.acceptable_evidence_groups for item in group.items
    )
    if _LAYOUT_RE.search(f"{case.query} {anchors}"):
        tags.append("table_or_formula")
    if (
        max((len(" ".join(item.anchor.split())) for item in case.expected_evidence), default=0)
        >= 300
    ):
        tags.append("long_evidence")
    return tuple(tags)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _retrieval_code_sha256() -> str:
    package = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for relative in (
        Path("agent/tools.py"),
        Path("rag/chunking.py"),
        Path("rag/retrieval_quality.py"),
        Path("rag/rrf.py"),
        Path("rag/retrieval_enhancements.py"),
    ):
        path = package / relative
        digest.update(str(relative).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _retrieval_only_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """移除回答生成指标，避免把空回答误解释为拒答或不安全回答。"""

    kept = {
        key: metrics[key]
        for key in (
            "case_count",
            "answerable_count",
            "unanswerable_count",
            "retrieval_recall_at_k",
            "retrieval_mrr_at_k",
            "evidence_group_recall_at_k",
            "evidence_page_recall_at_k",
            "latency_ms",
        )
        if key in metrics
    }
    for group_name in ("by_split", "by_category"):
        groups = metrics.get(group_name)
        if isinstance(groups, dict):
            kept[group_name] = {
                str(name): _retrieval_only_metrics(value)
                for name, value in groups.items()
                if isinstance(value, dict)
            }
    kept["answer_and_citation_metrics"] = {
        "status": "not_measured",
        "reason": "retrieval_only_protocol",
    }
    return kept


async def evaluate_production_cases(
    cases: Sequence[FrozenEvaluationCase],
    *,
    user_id: str,
    paper_id_map: dict[str, str],
    retriever: ProductionRetriever,
    k: int = 5,
    retrieval_mode: Literal["unified", "per_paper_same", "per_paper_specific"] = "per_paper_same",
) -> dict[str, Any]:
    """在相同 K 和冻结 scope 上调用真实检索器，并保留逐题耗时。"""

    if not cases:
        raise ValueError("生产同源评测至少需要一个用例")
    predictions: list[EvaluationPrediction] = []
    cases_by_dimension: dict[str, list[EvaluationCase]] = defaultdict(list)
    predictions_by_id: dict[str, EvaluationPrediction] = {}
    invalid_retrievals = 0
    channels: dict[str, int] = defaultdict(int)
    fallback_reasons: dict[str, int] = defaultdict(int)
    processors: dict[str, int] = defaultdict(int)
    rewrite_reasons: dict[str, int] = defaultdict(int)
    reranker_fallback_reasons: dict[str, int] = defaultdict(int)
    unanswerable_false_retrievals = 0
    required_papers_retrieved = 0
    required_papers_total = 0
    micro_pages_retrieved = 0
    micro_pages_total = 0
    case_results: list[dict[str, Any]] = []
    for case in cases:
        missing = [paper_id for paper_id in case.paper_ids if paper_id not in paper_id_map]
        if missing:
            raise ValueError(f"{case.id} 缺少论文映射：{missing}")
        local_scope = [paper_id_map[paper_id] for paper_id in case.paper_ids]
        started = time.perf_counter()
        ensure_paper_coverage = len(local_scope) > 1 and retrieval_mode != "unified"
        evidence = await retriever(
            LibrarySearchInput(
                user_id=user_id,
                query=case.query,
                paper_ids=local_scope,
                limit=k,
                ensure_paper_coverage=ensure_paper_coverage,
                per_paper_query_mode=(
                    "paper_specific" if retrieval_mode == "per_paper_specific" else "same_query"
                ),
            )
        )
        candidate_snapshot = list(
            getattr(retriever, "last_candidate_snapshot", ()) or evidence
        )
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        logical_by_local = {local: logical for logical, local in paper_id_map.items()}
        converted: list[RetrievedEvidencePrediction] = []
        for item in evidence[:k]:
            logical_id = logical_by_local.get(item.paper_id)
            if logical_id is None or item.paper_id not in local_scope or item.physical_page < 1:
                invalid_retrievals += 1
                continue
            converted.append(
                RetrievedEvidencePrediction(
                    chunk_id=item.chunk_id,
                    paper_id=logical_id,
                    physical_page=item.physical_page,
                    score=item.retrieval_score,
                )
            )
            for channel in item.retrieval_channels:
                channels[channel] += 1
            for processor in item.retrieval_processors:
                processors[processor] += 1
            for reason in item.query_rewrite_reasons:
                rewrite_reasons[reason] += 1
            if item.vector_fallback_reason:
                fallback_reasons[item.vector_fallback_reason] += 1
            if item.reranker_fallback_reason:
                reranker_fallback_reasons[item.reranker_fallback_reason] += 1
        prediction = EvaluationPrediction(
            case_id=case.id,
            answer="",
            abstained=not bool(converted),
            retrieved_evidence=converted,
            latency_ms=latency_ms,
        )
        predictions.append(prediction)
        if not case.answerable:
            unanswerable_false_retrievals += int(bool(converted))
        ranked_pairs = [(item.paper_id, item.physical_page) for item in converted[:k]]
        rank_by_page = {pair: index for index, pair in enumerate(ranked_pairs, 1)}
        evidence_groups = [
            {(item.paper_id, item.physical_page) for item in group.items}
            for group in case.acceptable_evidence_groups
        ]
        if not evidence_groups and case.expected_evidence:
            evidence_groups = [
                {(item.paper_id, item.physical_page) for item in case.expected_evidence}
            ]
        best_group: set[tuple[str, int]] = set()
        if evidence_groups:
            best_group = max(
                evidence_groups,
                key=lambda group: (
                    len(set(ranked_pairs) & group) / len(group),
                    len(set(ranked_pairs) & group),
                    -len(group),
                ),
            )
            micro_pages_retrieved += len(set(ranked_pairs) & best_group)
            micro_pages_total += len(best_group)
        if len(case.paper_ids) > 1 and case.answerable:
            required = min(
                (
                    {paper_id for paper_id, _page in group}
                    for group in evidence_groups
                    if group
                ),
                key=len,
                default=set(case.paper_ids),
            )
            retrieved_papers = {item.paper_id for item in converted[:k]}
            required_papers_retrieved += len(required & retrieved_papers)
            required_papers_total += len(required)
        else:
            required = set()
            retrieved_papers = {item.paper_id for item in converted[:k]}
        predictions_by_id[case.id] = prediction
        case_results.append(
            {
                "case_id": case.id,
                "query": case.query,
                "scope_paper_ids": list(case.paper_ids),
                "answerable": case.answerable,
                "gold_evidence_groups": [
                    [
                        {
                            "paper_id": item.paper_id,
                            "physical_page": item.physical_page,
                            "anchor_sha256": hashlib.sha256(
                                " ".join(item.anchor.split()).encode("utf-8")
                            ).hexdigest(),
                        }
                        for item in group.items
                    ]
                    for group in case.acceptable_evidence_groups
                ],
                "latency_ms": latency_ms,
                "top_40": [
                    {
                        "paper_id": logical_by_local[item.paper_id],
                        "physical_page": item.physical_page,
                        "chunk_id": item.chunk_id,
                        "score": item.retrieval_score,
                        "channels": list(item.retrieval_channels),
                        "channel_scores": [list(value) for value in item.channel_scores],
                        "matched_query": item.retrieval_query,
                    }
                    for item in candidate_snapshot[:40]
                    if item.paper_id in logical_by_local
                    and item.paper_id in local_scope
                    and item.physical_page >= 1
                ],
                "top_5": [
                    {
                        "paper_id": item.paper_id,
                        "physical_page": item.physical_page,
                        "chunk_id": item.chunk_id,
                        "score": item.score,
                    }
                    for item in converted[:k]
                ],
                "gold_page_ranks": [
                    {
                        "paper_id": paper_id,
                        "physical_page": physical_page,
                        "rank": rank_by_page.get((paper_id, physical_page)),
                    }
                    for paper_id, physical_page in sorted(
                        {
                            (item.paper_id, item.physical_page)
                            for group in case.acceptable_evidence_groups
                            for item in group.items
                        }
                        | {
                            (item.paper_id, item.physical_page)
                            for item in case.expected_evidence
                        }
                    )
                ],
                "best_evidence_group": {
                    "required_pages": len(best_group),
                    "retrieved_pages": len(set(ranked_pairs) & best_group),
                    "complete_hit": bool(best_group and best_group <= set(ranked_pairs)),
                },
                "required_paper_coverage": {
                    "required_paper_ids": sorted(required),
                    "retrieved_paper_ids": sorted(required & retrieved_papers),
                    "numerator": len(required & retrieved_papers),
                    "denominator": len(required),
                },
                "rewrite_queries": list(
                    dict.fromkeys(
                        item.retrieval_query
                        for item in candidate_snapshot
                        if item.retrieval_query
                        and " ".join(item.retrieval_query.split()).casefold()
                        != " ".join(case.query.split()).casefold()
                    )
                ),
                "query_rewrite_reasons": list(
                    getattr(retriever, "last_rewrite_reasons", ())
                ),
                "vector_fallback_reason": getattr(
                    retriever, "last_vector_fallback_reason", None
                ),
                "reranker_fallback_reason": getattr(
                    retriever, "last_reranker_fallback_reason", None
                ),
                "final_citations": [],
                "error": None,
            }
        )
        converted_case = _evaluation_case(case)
        for tag in _dimension_tags(case):
            cases_by_dimension[tag].append(converted_case)
    evaluation_cases = [_evaluation_case(case) for case in cases]
    metrics = _retrieval_only_metrics(evaluate(evaluation_cases, predictions, k=k))
    metrics["by_retrieval_dimension"] = {
        name: _retrieval_only_metrics(
            evaluate(
                subset,
                [predictions_by_id[item.id] for item in subset],
                k=k,
            )
        )
        for name, subset in sorted(cases_by_dimension.items())
    }
    for name in (
        "single_paper",
        "cross_paper",
        "cjk_query",
        "latin_query",
        "technical_entity",
        "table_or_formula",
        "long_evidence",
    ):
        metrics["by_retrieval_dimension"].setdefault(
            name,
            {"status": "not_measured", "case_count": 0},
        )
    metrics["invalid_retrieval_count"] = invalid_retrievals
    metrics["retrieval_channels"] = dict(sorted(channels.items()))
    metrics["retrieval_processors"] = dict(sorted(processors.items()))
    metrics["query_rewrite_reasons"] = dict(sorted(rewrite_reasons.items()))
    metrics["vector_fallback_reasons"] = dict(sorted(fallback_reasons.items()))
    metrics["reranker_fallback_reasons"] = dict(sorted(reranker_fallback_reasons.items()))
    metrics["unanswerable_false_retrieval_rate"] = {
        "numerator": unanswerable_false_retrievals,
        "denominator": sum(not case.answerable for case in cases),
        "value": (
            unanswerable_false_retrievals / sum(not case.answerable for case in cases)
            if any(not case.answerable for case in cases)
            else None
        ),
    }
    metrics["required_paper_coverage_at_k"] = {
        "k": k,
        "numerator": required_papers_retrieved,
        "denominator": required_papers_total,
        "value": (
            required_papers_retrieved / required_papers_total
            if required_papers_total
            else None
        ),
    }
    metrics["page_micro_recall_at_k"] = {
        "k": k,
        "numerator": micro_pages_retrieved,
        "denominator": micro_pages_total,
        "value": micro_pages_retrieved / micro_pages_total if micro_pages_total else None,
        "definition": "每题选择命中比例最高的人工可接受证据组后做页级 micro 汇总",
    }
    latencies = [int(item["latency_ms"]) for item in case_results]
    warm = sorted(latencies[1:])
    metrics["latency_cold_warm_ms"] = {
        "cold_start": {
            "sample_count": min(1, len(latencies)),
            "p50": latencies[0] if latencies else None,
            "p95": latencies[0] if latencies else None,
            "definition": "新建检索器后的首题；不等同于清空 PostgreSQL/Ollama 操作系统缓存",
        },
        "warm": {
            "sample_count": len(warm),
            "p50": warm[len(warm) // 2] if warm else None,
            "p95": warm[max(0, min(len(warm) - 1, int(len(warm) * 0.95 + 0.999999) - 1))]
            if warm
            else None,
        },
    }
    metrics["case_results"] = case_results
    return metrics


def paired_bootstrap_interval(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    samples: int = 10_000,
    seed: int = 20260814,
) -> dict[str, float | int]:
    """对逐题配对差值做确定性 Bootstrap 95% 区间。"""

    if len(baseline) != len(candidate) or not baseline:
        raise ValueError("配对 Bootstrap 要求两个等长非空序列")
    if samples < 1000:
        raise ValueError("正式 Bootstrap 至少抽样 1000 次")
    differences = [right - left for left, right in zip(baseline, candidate)]
    rng = random.Random(seed)
    values = sorted(
        statistics.fmean(differences[rng.randrange(len(differences))] for _ in differences)
        for _ in range(samples)
    )
    lower = values[int(samples * 0.025)]
    upper = values[min(samples - 1, int(samples * 0.975))]
    return {
        "sample_count": len(differences),
        "bootstrap_samples": samples,
        "mean_delta": statistics.fmean(differences),
        "ci95_lower": lower,
        "ci95_upper": upper,
        "seed": seed,
    }


def paired_ratio_bootstrap_interval(
    baseline: Sequence[tuple[int, int]],
    candidate: Sequence[tuple[int, int]],
    *,
    samples: int = 10_000,
    seed: int = 20260814,
) -> dict[str, Any]:
    """逐题成对重采样，并在每次抽样后重新汇总 micro 分子和分母。"""

    if len(baseline) != len(candidate) or not baseline:
        raise ValueError("配对 ratio Bootstrap 要求两个等长非空序列")
    if samples < 1000:
        raise ValueError("正式 Bootstrap 至少抽样 1000 次")
    if any(denominator <= 0 for _numerator, denominator in (*baseline, *candidate)):
        raise ValueError("配对 ratio Bootstrap 的分母必须为正数")

    baseline_numerator = sum(item[0] for item in baseline)
    baseline_denominator = sum(item[1] for item in baseline)
    candidate_numerator = sum(item[0] for item in candidate)
    candidate_denominator = sum(item[1] for item in candidate)
    observed_delta = (
        candidate_numerator / candidate_denominator
        - baseline_numerator / baseline_denominator
    )
    rng = random.Random(seed)
    sample_count = len(baseline)
    deltas: list[float] = []
    for _ in range(samples):
        indexes = [rng.randrange(sample_count) for _ in range(sample_count)]
        left_numerator = sum(baseline[index][0] for index in indexes)
        left_denominator = sum(baseline[index][1] for index in indexes)
        right_numerator = sum(candidate[index][0] for index in indexes)
        right_denominator = sum(candidate[index][1] for index in indexes)
        deltas.append(
            right_numerator / right_denominator - left_numerator / left_denominator
        )
    deltas.sort()
    return {
        "sample_count": sample_count,
        "bootstrap_samples": samples,
        "baseline": {
            "numerator": baseline_numerator,
            "denominator": baseline_denominator,
            "value": baseline_numerator / baseline_denominator,
        },
        "candidate": {
            "numerator": candidate_numerator,
            "denominator": candidate_denominator,
            "value": candidate_numerator / candidate_denominator,
        },
        "mean_delta": observed_delta,
        "ci95_lower": deltas[int(samples * 0.025)],
        "ci95_upper": deltas[min(samples - 1, int(samples * 0.975))],
        "seed": seed,
        "estimand": "paired_case_resampled_micro_ratio_delta",
    }


async def preflight_production_corpus(
    manifest: EvaluationDatasetManifest,
    *,
    user_email: str,
    required_paper_ids: set[str] | None = None,
    config: Any = settings,
) -> dict[str, Any]:
    expected_papers = [
        paper
        for paper in manifest.papers
        if required_paper_ids is None or paper.id in required_paper_ids
    ]
    if not expected_papers:
        return {"status": "not_executed", "reason": "empty_evaluation_scope"}
    router = build_model_router(config)
    contract = configured_embedding_contract(config, router)
    async with get_session_factory()() as session:
        user = await session.scalar(
            select(User).where(func.lower(User.email) == user_email.lower())
        )
        if user is None:
            return {"status": "not_executed", "reason": "evaluation_user_not_found"}
        rows = list(
            (
                await session.execute(
                    select(Paper).where(
                        Paper.owner_id == user.id,
                        or_(
                            Paper.arxiv_id.in_(
                                [paper.arxiv_id for paper in expected_papers]
                            ),
                            Paper.sha256.in_([paper.sha256 for paper in expected_papers]),
                        ),
                    )
                )
            ).scalars()
        )
        by_arxiv = {paper.arxiv_id: paper for paper in rows if paper.arxiv_id}
        by_sha = {paper.sha256: paper for paper in rows}
        matched = {
            expected.id: by_sha.get(expected.sha256) or by_arxiv.get(expected.arxiv_id)
            for expected in expected_papers
        }
        missing = [
            paper.arxiv_id for paper in expected_papers if matched.get(paper.id) is None
        ]
        stale: list[str] = []
        empty: list[str] = []
        mapping: dict[str, str] = {}
        for expected in expected_papers:
            paper = matched.get(expected.id)
            if paper is None:
                continue
            if paper.sha256 != expected.sha256 or paper.page_count != expected.page_count:
                stale.append(expected.id)
                continue
            mapping[expected.id] = paper.id
            count = await session.scalar(
                select(func.count(PaperChunk.id)).where(PaperChunk.paper_id == paper.id)
            )
            if int(count or 0) == 0:
                empty.append(expected.id)
            if (
                contract is None
                or paper.embedding_status != "ready"
                or paper.embedding_fingerprint != contract.fingerprint
                or paper.chunking_strategy != "structure_aware_v2"
            ):
                stale.append(expected.id)
    reasons = []
    if missing:
        reasons.append("missing_papers")
    if empty:
        reasons.append("missing_chunks")
    if stale:
        reasons.append("index_contract_not_ready")
    return {
        "status": "ready" if not reasons else "not_executed",
        "reason": reasons[0] if reasons else None,
        "user_id": user.id,
        "paper_id_map": mapping,
        "missing_papers": missing,
        "missing_chunks": empty,
        "stale_or_incompatible": stale,
        "embedding_contract": (
            {
                "provider": contract.provider,
                "model": contract.model,
                "dimensions": contract.dimensions,
                "revision": contract.revision,
                "input_format": contract.input_format,
                "fingerprint": contract.fingerprint,
            }
            if contract is not None
            else None
        ),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = read_manifest(args.manifest)
    cases = read_frozen_cases(args.cases)
    validate_dataset(manifest, cases)
    selected = [case for case in cases if args.split == "all" or case.split == args.split]
    if args.case_id:
        requested = set(args.case_id)
        selected = [case for case in selected if case.id in requested]
        missing_case_ids = sorted(requested - {case.id for case in selected})
        if missing_case_ids:
            raise ValueError(f"指定用例不存在或不属于当前 split：{missing_case_ids}")
    if not selected:
        raise ValueError("当前条件没有可执行评测用例")
    required_paper_ids = {paper_id for case in selected for paper_id in case.paper_ids}
    preflight = await preflight_production_corpus(
        manifest,
        user_email=args.user_email,
        required_paper_ids=required_paper_ids,
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": preflight["status"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol": {
            "dataset_id": manifest.dataset_id,
            "dataset_version": manifest.version,
            "manifest_sha256": _sha256_file(args.manifest),
            "cases_sha256": _sha256_file(args.cases),
            "retrieval_code_sha256": _retrieval_code_sha256(),
            "split": args.split,
            "case_count": len(selected),
            "case_ids": [case.id for case in selected],
            "k": args.k,
            "retrieval_mode": args.retrieval_mode,
            "configuration": {
                "candidate_pool_size": settings.rag_candidate_pool_size,
                "per_paper_retrieval_enabled": settings.rag_per_paper_retrieval_enabled,
                "per_paper_candidate_limit": settings.rag_per_paper_candidate_limit,
                "weak_query_rewrite_enabled": settings.rag_weak_query_rewrite_enabled,
                "query_rewrite_max_queries": settings.rag_query_rewrite_max_queries,
                "reranker_enabled": settings.rag_reranker_enabled,
                "reranker_model": settings.rag_reranker_model,
                "reranker_candidate_limit": settings.rag_reranker_candidate_limit,
            },
            "retrieval_chain": [
                "structure_aware_v2",
                "postgresql_fts_pg_trgm",
                "embedding_contract",
                "rrf",
                "page_dedup",
                "optional_per_paper_balance",
                "optional_weak_query_rewrite",
                "optional_sentence_window_rerank",
            ],
        },
        "preflight": {
            key: value
            for key, value in preflight.items()
            if key not in {"user_id", "paper_id_map"}
        }
        | {"mapped_paper_count": len(preflight.get("paper_id_map", {}))},
    }
    if preflight["status"] == "ready":
        result["metrics"] = await evaluate_production_cases(
            selected,
            user_id=str(preflight["user_id"]),
            paper_id_map=dict(preflight["paper_id_map"]),
            retriever=SQLLibrarySearch(),
            k=args.k,
            retrieval_mode=args.retrieval_mode,
        )
        result["status"] = "completed"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 PaperLeaf 生产同源 RAG 评测")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--user-email", required=True)
    parser.add_argument("--split", choices=["all", "dev", "test", "holdout"], default="test")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="只执行指定冻结用例；可重复传入。省略时执行整个 split。",
    )
    parser.add_argument("-k", type=int, default=5)
    parser.add_argument(
        "--retrieval-mode",
        choices=["unified", "per_paper_same", "per_paper_specific"],
        default="per_paper_same",
    )
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": result["status"], "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
