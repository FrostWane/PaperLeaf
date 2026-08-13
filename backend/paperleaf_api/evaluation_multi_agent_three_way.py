"""从真实 capture 与人工盲评生成 v1/v2/v3 对照报告。

该模块只接受 ``evaluation_multi_agent_live`` 生成并冻结的真实结果。任何
``not_measured`` 都保持未知，绝不转成零；人工指标只有填写 annotator 的盲评
记录才会计入分母。
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

VERSIONS = ("v1", "v2", "v3")


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _measured(result: dict[str, Any], key: str) -> Any | None:
    value = dict(result.get("measurements", {})).get(key)
    if not isinstance(value, dict) or value.get("status") != "measured":
        return None
    return value.get("value")


def aggregate_capture(report: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, dict[str, Any]] = {}
    for version in VERSIONS:
        executed = completed = fallback = 0
        durations: list[int] = []
        citation_correct = citation_total = citation_illegal = 0
        covered_claims = expected_claims = 0
        cited_papers = expected_papers = 0
        supported_claims = output_claims = 0
        model_calls = tool_calls = 0
        estimated_input = estimated_output = 0
        branch_tokens_in = branch_tokens_out = 0
        branch_errors: Counter[str] = Counter()
        for pair in report.get("pairs", []):
            result = pair.get(version, {})
            if result.get("execution_status") != "executed":
                continue
            executed += 1
            completed += int(result.get("run_status") == "completed")
            fallback += int(bool(result.get("fallback_to_v1")))
            if isinstance(result.get("duration_ms"), int):
                durations.append(max(0, result["duration_ms"]))
            citation = _measured(result, "citation_audit")
            if isinstance(citation, dict):
                citation_correct += int(citation.get("correct_page_citations", 0))
                citation_total += int(citation.get("total_citations", 0))
                citation_illegal += int(citation.get("illegal_citation_count", 0))
                covered_claims += len(citation.get("covered_source_case_ids", []))
                cited_papers += len(citation.get("cited_paper_ids", []))
            # 每个 pair 的冻结 scope/source oracle 由 capture 在提交前锁定。
            case_meta = pair.get("case_metrics", {})
            expected_claims += int(case_meta.get("expected_claim_count", 0))
            expected_papers += int(case_meta.get("required_paper_count", 0))
            support = _measured(result, "claim_support")
            if isinstance(support, dict):
                supported_claims += int(support.get("supported", 0))
                output_claims += int(support.get("total", 0))
            estimated_input_value = result.get("estimated_input_tokens", {})
            estimated_output_value = result.get("estimated_output_tokens", {})
            if (
                isinstance(estimated_input_value, dict)
                and estimated_input_value.get("status") == "measured"
            ):
                estimated_input += int(estimated_input_value.get("value", 0))
            if (
                isinstance(estimated_output_value, dict)
                and estimated_output_value.get("status") == "measured"
            ):
                estimated_output += int(estimated_output_value.get("value", 0))
            for target, key in (("model", "model_call_count"), ("tool", "tool_call_count")):
                measurement = result.get(key, {})
                value = (
                    int(measurement.get("value", 0))
                    if isinstance(measurement, dict) and measurement.get("status") == "measured"
                    else 0
                )
                if target == "model":
                    model_calls += value
                else:
                    tool_calls += value
            branch = _measured(result, "branch_metrics")
            if isinstance(branch, list):
                for item in branch:
                    if not isinstance(item, dict):
                        continue
                    branch_tokens_in += int(item.get("input_tokens", 0) or 0)
                    branch_tokens_out += int(item.get("output_tokens", 0) or 0)
                    category = str(item.get("error_category") or "")
                    if category:
                        branch_errors[category] += 1
        metrics[version] = {
            "executed_runs": executed,
            "completion_rate": _ratio(completed, executed),
            "fallback_rate": _ratio(fallback, executed),
            "citation_page_accuracy": _ratio(citation_correct, citation_total),
            "illegal_citations": citation_illegal,
            "expected_claim_evidence_coverage": _ratio(covered_claims, expected_claims),
            "required_paper_coverage": _ratio(cited_papers, expected_papers),
            "output_claim_support_rate": _ratio(supported_claims, output_claims),
            "latency_ms": {"p95": _p95(durations)},
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "estimated_tokens": {"input": estimated_input, "output": estimated_output},
            "monetary_cost": {
                "status": "not_measured",
                "value": None,
                "currency": None,
                "reason": "Provider 未持久化完整计费 Token，且评测协议未冻结价格快照",
            },
            "specialist_branch_tokens": {"input": branch_tokens_in, "output": branch_tokens_out},
            "branch_error_categories": dict(branch_errors),
        }
    return metrics


def _blind_key_lookup(key_rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, str]]:
    return {
        (str(row.get("case_id", "")), str(row.get("input_hash", ""))): {
            str(label): str(version)
            for label, version in dict(row.get("mapping", {})).items()
        }
        for row in key_rows
    }


def aggregate_human_ratings(
    rows: list[dict[str, Any]], key_rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    scores = {
        version: {"factuality": [], "usefulness": [], "conflict_handling": []}
        for version in VERSIONS
    }
    preferences: Counter[str] = Counter()
    completed = 0
    key_lookup = _blind_key_lookup(key_rows or [])
    for row in rows:
        rating = row.get("rating", {})
        annotator = str(rating.get("human_annotator") or "").strip()
        mapping = row.get("_private_mapping") or key_lookup.get(
            (str(row.get("case_id", "")), str(row.get("input_hash", ""))), {}
        )
        preferred = str(rating.get("preferred") or "")
        if not annotator or preferred not in {"A", "B", "C", "tie"}:
            continue
        completed += 1
        preferences[mapping.get(preferred, "tie") if preferred != "tie" else "tie"] += 1
        for metric in ("factuality", "usefulness", "conflict_handling"):
            values = rating.get(metric, {})
            for blind_label, version in mapping.items():
                value = values.get(blind_label) if isinstance(values, dict) else None
                if isinstance(value, int | float) and 1 <= float(value) <= 5:
                    scores[version][metric].append(float(value))
    return {
        "completed_reviews": completed,
        "preferences": dict(preferences),
        "scores": {
            version: {
                metric: {
                    "mean": sum(values) / len(values) if values else None,
                    "count": len(values),
                }
                for metric, values in version_scores.items()
            }
            for version, version_scores in scores.items()
        },
        "status": "completed" if completed else "awaiting_human_review",
    }


def evaluate_three_way(
    capture: dict[str, Any],
    blind_rows: list[dict[str, Any]],
    blind_key_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    variants = aggregate_capture(capture)
    human = aggregate_human_ratings(blind_rows, blind_key_rows)
    all_executed = all(variants[version]["executed_runs"] > 0 for version in VERSIONS)
    return {
        "schema_version": 1,
        "capture_content_hash": capture.get("capture_content_hash"),
        "token_measurement": capture.get("token_measurement"),
        "variants": variants,
        "human_blind_review": human,
        "decision": (
            "quality_pending"
            if not all_executed or human["status"] != "completed"
            else "ready_for_engineering_decision"
        ),
    }


def combine_captures(captures: list[dict[str, Any]]) -> dict[str, Any]:
    """合并同一冻结协议的多次真实采集，不改写单次 capture。"""

    if not captures:
        raise ValueError("至少需要一个 capture")
    dataset_ids = {str(item.get("dataset", {}).get("id", "")) for item in captures}
    token_measurements = {str(item.get("token_measurement", "")) for item in captures}
    if len(dataset_ids) != 1 or len(token_measurements) != 1:
        raise ValueError("capture 的数据集或 Token 口径不一致")
    combined = dict(captures[0])
    combined["pairs"] = [pair for capture in captures for pair in capture.get("pairs", [])]
    combined["capture_content_hashes"] = [
        str(capture.get("capture_content_hash", "")) for capture in captures
    ]
    combined["capture_content_hash"] = None
    combined["combined_capture_count"] = len(captures)
    return combined


def main() -> None:
    parser = argparse.ArgumentParser(description="汇总 PaperLeaf v1/v2/v3 真实对照")
    parser.add_argument("--capture", required=True, type=Path, action="append")
    parser.add_argument("--blind", required=True, type=Path, action="append")
    parser.add_argument("--blind-key", type=Path, action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    capture = combine_captures(
        [json.loads(path.read_text(encoding="utf-8")) for path in args.capture]
    )
    blind_rows = [
        json.loads(line)
        for path in args.blind
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    blind_key_rows = [
        json.loads(line)
        for path in args.blind_key
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result = evaluate_three_way(capture, blind_rows, blind_key_rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"decision": result["decision"], "output": str(args.output)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
