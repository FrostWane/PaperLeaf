"""聚合七个预注册检索方案，并生成配对 Bootstrap 与证据索引。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .evaluation_formal_protocol import FORMAL_VARIANTS
from .evaluation_production import paired_bootstrap_interval

COMPARISONS = (
    ("plain_embedding_control", "contextual_embedding", "上下文化 Embedding"),
    ("contextual_embedding", "per_paper_retrieval", "逐论文专属检索"),
    ("contextual_embedding", "weak_query_rewrite", "弱结果 Query Rewrite"),
    ("contextual_embedding", "multigranular_page_reranker", "页级多粒度重排"),
    ("production_baseline", "final_combined", "最终组合方案"),
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _artifact_sha(path: Path) -> str:
    """使用与 Git 文本对象一致的 LF SHA，保证 Windows/Linux 可复核。"""

    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _case_scores(case: dict[str, Any]) -> dict[str, float | None]:
    group = case["best_evidence_group"]
    ranks = [item["rank"] for item in case["gold_page_ranks"] if item["rank"] is not None]
    coverage = case["required_paper_coverage"]
    return {
        "page_recall": (
            group["retrieved_pages"] / group["required_pages"] if group["required_pages"] else None
        ),
        "mrr": 1 / min(ranks) if ranks else 0.0,
        "complete_group_hit": float(group["complete_hit"]),
        "required_paper_coverage": (
            coverage["numerator"] / coverage["denominator"] if coverage["denominator"] else None
        ),
    }


def _paired(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]], metric: str
) -> dict[str, Any]:
    left_by_id = {item["case_id"]: _case_scores(item)[metric] for item in baseline}
    right_by_id = {item["case_id"]: _case_scores(item)[metric] for item in candidate}
    if set(left_by_id) != set(right_by_id):
        raise RuntimeError("消融方案逐题 ID 不一致")
    pairs = [(left_by_id[case_id], right_by_id[case_id]) for case_id in sorted(left_by_id)]
    usable = [(left, right) for left, right in pairs if left is not None and right is not None]
    if not usable:
        return {"status": "not_measured", "reason": "no_eligible_cases"}
    return paired_bootstrap_interval(
        [float(left) for left, _right in usable],
        [float(right) for _left, right in usable],
    )


def _nearest_rank(values: list[int], quantile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def _bucket_metrics(subset: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in subset if row.get("answerable")]
    page_numerator = sum(int(row["best_evidence_group"]["retrieved_pages"]) for row in answerable)
    page_denominator = sum(int(row["best_evidence_group"]["required_pages"]) for row in answerable)
    reciprocal_sum = 0.0
    for row in answerable:
        ranks = [
            int(item["rank"])
            for item in row["gold_page_ranks"]
            if item.get("rank") is not None and int(item["rank"]) <= 5
        ]
        reciprocal_sum += 1 / min(ranks) if ranks else 0.0
    group_hits = sum(bool(row["best_evidence_group"]["complete_hit"]) for row in answerable)
    required_numerator = sum(int(row["required_paper_coverage"]["numerator"]) for row in subset)
    required_denominator = sum(int(row["required_paper_coverage"]["denominator"]) for row in subset)
    unanswerable = [row for row in subset if not row.get("answerable")]
    false_retrievals = sum(bool(row.get("top_5")) for row in unanswerable)
    latencies = [int(row["latency_ms"]) for row in subset]
    return {
        "case_count": len(subset),
        "page_micro_recall_at_5": {
            "numerator": page_numerator,
            "denominator": page_denominator,
            "value": page_numerator / page_denominator if page_denominator else None,
        },
        "mrr_at_5": {
            "numerator": reciprocal_sum,
            "denominator": len(answerable),
            "value": reciprocal_sum / len(answerable) if answerable else None,
        },
        "complete_evidence_group_at_5": {
            "numerator": group_hits,
            "denominator": len(answerable),
            "value": group_hits / len(answerable) if answerable else None,
        },
        "required_paper_coverage_at_5": {
            "numerator": required_numerator,
            "denominator": required_denominator,
            "value": (required_numerator / required_denominator if required_denominator else None),
        },
        "unanswerable_false_retrieval_rate": {
            "numerator": false_retrievals,
            "denominator": len(unanswerable),
            "value": false_retrievals / len(unanswerable) if unanswerable else None,
        },
        "latency_ms": {
            "p50": _nearest_rank(latencies, 0.50),
            "p95": _nearest_rank(latencies, 0.95),
        },
    }


def _language_bucket_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """从冻结逐题结果补齐中英文桶，不重新运行检索。"""

    return {
        name: _bucket_metrics([row for row in rows if predicate(str(row.get("query", "")))])
        for name, predicate in (
            ("cjk_query", lambda query: bool(_CJK_RE.search(query))),
            ("latin_query", lambda query: not _CJK_RE.search(query)),
        )
    }


def _frozen_category_metrics(
    rows: list[dict[str, Any]], category_by_id: dict[str, str]
) -> dict[str, Any]:
    return {
        category: _bucket_metrics(
            [row for row in rows if category_by_id.get(str(row["case_id"])) == category]
        )
        for category in sorted(set(category_by_id.values()))
    }


def aggregate(root: Path, *, mode: str) -> dict[str, Any]:
    variants: dict[str, dict[str, Any]] = {}
    common_dataset: tuple[Any, ...] | None = None
    common_chunk_snapshot: str | None = None
    combined_rows: list[dict[str, Any]] = []
    for name in FORMAL_VARIANTS:
        directory = root / name
        manifest = _read_json(directory / "run_manifest.json")
        metrics = _read_json(directory / "metrics.json")
        rows = _read_jsonl(directory / "per_query_results.jsonl")
        if manifest.get("status") != "completed":
            raise RuntimeError(f"{name} 未完整完成，禁止聚合")
        identity = (
            manifest["dataset"]["dataset_id"],
            manifest["dataset"]["manifest_sha256"],
            manifest["dataset"]["cases_or_questions_sha256"],
            manifest["dataset"]["oracle_sha256"],
            manifest["dataset"]["case_count"],
        )
        if common_dataset is None:
            common_dataset = identity
        elif identity != common_dataset:
            raise RuntimeError("消融方案数据集、问题或 Ground Truth 口径不一致")
        chunk_snapshot = manifest["corpus"]["chunk_snapshot_sha256"]
        if common_chunk_snapshot is None:
            common_chunk_snapshot = chunk_snapshot
        elif chunk_snapshot != common_chunk_snapshot:
            raise RuntimeError("消融方案 Chunk 快照不一致")
        if len(rows) != manifest["dataset"]["case_count"]:
            raise RuntimeError(f"{name} 逐题结果不完整")
        variants[name] = {"manifest": manifest, "metrics": metrics, "rows": rows}
        combined_rows.extend({"variant": name, **row} for row in rows)

    comparisons: dict[str, Any] = {}
    for baseline, candidate, label in COMPARISONS:
        comparisons[f"{baseline}__to__{candidate}"] = {
            "label": label,
            "baseline": baseline,
            "candidate": candidate,
            "paired_bootstrap_95ci": {
                metric: _paired(variants[baseline]["rows"], variants[candidate]["rows"], metric)
                for metric in (
                    "page_recall",
                    "mrr",
                    "complete_group_hit",
                    "required_paper_coverage",
                )
            },
        }
    oracle_path = root / "ground_truth_oracle.jsonl"
    category_by_id = (
        {str(item["id"]): str(item["category"]) for item in _read_jsonl(oracle_path)}
        if oracle_path.exists()
        else {}
    )
    output_metrics = {
        "schema_version": 1,
        "status": "retrieval_completed_human_review_pending",
        "mode": mode,
        "dataset": {
            "dataset_id": common_dataset[0],
            "case_count": common_dataset[4],
            "chunk_snapshot_sha256": common_chunk_snapshot,
        },
        "variants": {
            name: {
                **value["metrics"],
                "by_query_language": _language_bucket_metrics(value["rows"]),
                **(
                    {"by_frozen_category": _frozen_category_metrics(value["rows"], category_by_id)}
                    if category_by_id
                    else {}
                ),
            }
            for name, value in variants.items()
        },
        "comparisons": comparisons,
    }
    (root / "per_query_results.jsonl").write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            for item in combined_rows
        ),
        encoding="utf-8",
    )
    (root / "metrics.json").write_text(
        json.dumps(output_metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    root_manifest = {
        "schema_version": 1,
        "status": "retrieval_completed_human_review_pending",
        "mode": mode,
        "dataset": output_metrics["dataset"],
        "variants": {
            name: {
                "run_manifest_sha256": _artifact_sha(root / name / "run_manifest.json"),
                "per_query_results_sha256": _artifact_sha(
                    root / name / "per_query_results.jsonl"
                ),
                "metrics_sha256": _artifact_sha(root / name / "metrics.json"),
            }
            for name in FORMAL_VARIANTS
        },
        "artifacts": {
            "per_query_results.jsonl": _artifact_sha(root / "per_query_results.jsonl"),
            "metrics.json": _artifact_sha(root / "metrics.json"),
        },
        "human_review": {
            "status": "pending",
            "minimum_answers": 30,
            "note": "LLM Judge 不得写成人工准确率",
        },
    }
    (root / "run_manifest.json").write_text(
        json.dumps(root_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_report(root, variants, comparisons, mode=mode)
    root_manifest["artifacts"]["REPORT.md"] = _artifact_sha(root / "REPORT.md")
    (root / "run_manifest.json").write_text(
        json.dumps(root_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return root_manifest


def _pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def _write_report(
    root: Path,
    variants: dict[str, dict[str, Any]],
    comparisons: dict[str, Any],
    *,
    mode: str,
) -> None:
    lines = [
        "# PaperLeaf 正式生产同源 RAG 评测",
        "",
        "状态：`"
        + ("diagnostic_not_blind" if mode == "diagnostic" else "hidden_first_formal_batch")
        + "`。",
        "",
        "所有方案使用同一问题、论文范围、Chunk 快照与 K=5。旧 MiniLM 句窗重排未启用。",
        "",
        "## 检索结果",
        "",
        "| 方案 | 页级 micro Recall@5 | MRR@5 | 完整证据组@5 | "
        "跨论文 required-paper coverage@5 | warm p95 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in FORMAL_VARIANTS:
        metrics = variants[name]["metrics"]
        lines.append(
            "| "
            + " | ".join(
                (
                    name,
                    _pct(metrics["page_micro_recall_at_k"]["value"]),
                    _pct(metrics["retrieval_mrr_at_k"]["value"]),
                    _pct(metrics["evidence_group_recall_at_k"]["value"]),
                    _pct(metrics["required_paper_coverage_at_k"]["value"]),
                    f"{metrics['latency_cold_warm_ms']['warm']['p95']} ms",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 配对 Bootstrap",
            "",
            "差值为候选减基线；区间跨 0 时不得声称稳定提升。",
            "",
        ]
    )
    for comparison in comparisons.values():
        lines.append(f"### {comparison['label']}")
        lines.append("")
        for metric, result in comparison["paired_bootstrap_95ci"].items():
            if result.get("status") == "not_measured":
                lines.append(f"- {metric}：未测量。")
            else:
                lines.append(
                    f"- {metric}：Δ {result['mean_delta']:.4f}，"
                    f"95% CI [{result['ci95_lower']:.4f}, {result['ci95_upper']:.4f}]，"
                    f"n={result['sample_count']}。"
                )
        lines.append("")
    lines.extend(
        [
            "## 边界",
            "",
            "- 该报告只覆盖检索；端到端回答、多 Agent 与人工盲评另行落盘。",
            "- 冷启动为新建检索器后的首题，不代表清空操作系统、PostgreSQL 和 Ollama 缓存。",
            "- 隐藏集运行后不得依据错误继续调参；后续复跑必须标记为揭盲后诊断。",
        ]
    )
    (root / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="聚合 PaperLeaf 正式 RAG 消融")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--mode", choices=["diagnostic", "hidden"], required=True)
    args = parser.parse_args()
    result = aggregate(args.root, mode=args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
