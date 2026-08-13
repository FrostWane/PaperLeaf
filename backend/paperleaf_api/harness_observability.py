"""Agent Harness 聚合观测。

只处理低基数状态、计数和耗时；不得返回问题、上下文、记忆正文、论文 ID、用户 ID
或工具参数。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percentile(values: Iterable[int | float], fraction: float) -> int | None:
    ordered = sorted(max(0, float(value)) for value in values)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index])


_COMPARE_ORCHESTRATION_VERSION = "compare_map_reduce_v2"
_SPECIALIST_ORCHESTRATION_VERSION = "specialist_subgraph_v3"
_COMPARE_FALLBACK_REASONS = {
    "disabled",
    "skill_not_supported",
    "scope_too_small",
    "scope_too_large",
    "invalid_plan",
    "budget_exceeded",
    "all_subtasks_failed",
    "all_specialists_failed",
    "no_merged_evidence",
    "cancelled",
    "timeout",
    "lease_lost",
    "internal_error",
}


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):  # noqa: UP038
        return max(0, int(value))
    return 0


def _compare_fallback_reason(value: Any) -> str | None:
    reason = str(value or "").strip().lower()
    if not reason:
        return None
    return reason if reason in _COMPARE_FALLBACK_REASONS else "other"


def aggregate_harness_metrics(
    runs: list[Any],
    tool_calls: list[Any],
    memory: dict[str, object],
    mcp_servers: list[Any],
    embedding: dict[str, object] | None = None,
    *,
    window_hours: int,
    limit_reached: bool,
) -> dict[str, Any]:
    context_runs = [item for item in runs if _dict(getattr(item, "context_snapshot", {}))]
    before_tokens: list[int] = []
    after_tokens: list[int] = []
    context_latency: list[int] = []
    confidences: list[float] = []
    compacted = 0
    clarification = 0
    context_limit_errors = 0
    skill_counts: Counter[str] = Counter()
    route_sources: Counter[str] = Counter()
    skill_completed: Counter[str] = Counter()
    skill_terminal: Counter[str] = Counter()
    vector_fallback_reasons: Counter[str] = Counter()
    compare_runs = 0
    compare_planned_subtasks = 0
    compare_succeeded_subtasks = 0
    compare_failed_subtasks = 0
    compare_timeout_subtasks = 0
    compare_partial_runs = 0
    compare_fallback_runs = 0
    compare_fallback_reasons: Counter[str] = Counter()
    compare_subtask_durations: list[int] = []
    compare_merge_durations: list[int] = []
    compare_dedup_count = 0
    compare_conflict_count = 0
    compare_finding_count = 0
    compare_paper_coverage_count = 0
    compare_branch_input_tokens = 0
    compare_branch_output_tokens = 0
    compare_provider_input_tokens = 0
    compare_provider_output_tokens = 0
    compare_provider_token_samples = 0
    compare_branch_evidence_count = 0
    compare_branch_claim_count = 0
    compare_branch_errors: Counter[str] = Counter()
    compare_validation_durations: list[int] = []
    compare_versions: Counter[str] = Counter()

    for run in runs:
        snapshot = _dict(getattr(run, "context_snapshot", {}))
        usage = _dict(snapshot.get("usage"))
        before = usage.get("conversation_before_tokens")
        after = usage.get("conversation_after_tokens")
        if isinstance(before, int):
            before_tokens.append(max(0, before))
        if isinstance(after, int):
            after_tokens.append(max(0, after))
        if usage.get("compacted") is True:
            compacted += 1
        confidence = getattr(run, "reference_confidence", None)
        if isinstance(confidence, (int, float)):  # noqa: UP038
            normalized = min(1.0, max(0.0, float(confidence)))
            confidences.append(normalized)
            if normalized < 0.55:
                clarification += 1
        summary = _dict(getattr(run, "result_summary", {}))
        trace = _dict(summary.get("rag_trace"))
        for reason in trace.get("vector_fallback_reasons", []):
            normalized_reason = str(reason)[:64]
            if normalized_reason:
                vector_fallback_reasons[normalized_reason] += 1
        timing = _dict(trace.get("stage_timings_ms")).get("context")
        if isinstance(timing, (int, float)):  # noqa: UP038
            context_latency.append(max(0, round(timing)))
        if "CONTEXT" in str(getattr(run, "error_code", "") or "").upper():
            context_limit_errors += 1
        skill = str(getattr(run, "selected_skill", None) or "legacy_agent")[:64]
        skill_counts[skill] += 1
        harness_trace = _dict(getattr(run, "harness_trace", {}))
        route_sources[str(harness_trace.get("skill_route_source") or "unknown")[:64]] += 1
        orchestration_version = str(harness_trace.get("orchestration_version") or "")
        if orchestration_version in {
            _COMPARE_ORCHESTRATION_VERSION,
            _SPECIALIST_ORCHESTRATION_VERSION,
        }:
            compare_runs += 1
            compare_versions[orchestration_version] += 1
            compare_planned_subtasks += min(
                3, _non_negative_int(harness_trace.get("planned_subtasks"))
            )
            compare_succeeded_subtasks += min(
                3, _non_negative_int(harness_trace.get("succeeded_subtasks"))
            )
            compare_failed_subtasks += min(
                3, _non_negative_int(harness_trace.get("failed_subtasks"))
            )
            compare_timeout_subtasks += min(
                3, _non_negative_int(harness_trace.get("timeout_subtasks"))
            )
            compare_dedup_count += _non_negative_int(harness_trace.get("merge_dedup_count"))
            compare_conflict_count += _non_negative_int(harness_trace.get("merge_conflict_count"))
            compare_finding_count += _non_negative_int(harness_trace.get("finding_count"))
            compare_paper_coverage_count += _non_negative_int(
                harness_trace.get("paper_coverage_count")
            )
            if harness_trace.get("partial_failure") is True:
                compare_partial_runs += 1
            if harness_trace.get("fallback_to_v1") is True:
                compare_fallback_runs += 1
            fallback_reason = _compare_fallback_reason(harness_trace.get("fallback_reason"))
            if fallback_reason:
                compare_fallback_reasons[fallback_reason] += 1
            raw_durations = harness_trace.get("subtask_durations_ms")
            if isinstance(raw_durations, list):
                compare_subtask_durations.extend(
                    _non_negative_int(value)
                    for value in raw_durations[:3]
                    if isinstance(value, (int, float)) and not isinstance(value, bool)  # noqa: UP038
                )
            merge_duration = harness_trace.get("merge_duration_ms")
            if isinstance(merge_duration, (int, float)) and not isinstance(  # noqa: UP038
                merge_duration, bool
            ):
                compare_merge_durations.append(_non_negative_int(merge_duration))
            raw_branch_metrics = harness_trace.get("branch_metrics")
            if isinstance(raw_branch_metrics, list):
                for item in raw_branch_metrics[:3]:
                    if not isinstance(item, dict):
                        continue
                    compare_branch_input_tokens += _non_negative_int(item.get("input_tokens"))
                    compare_branch_output_tokens += _non_negative_int(item.get("output_tokens"))
                    compare_branch_evidence_count += _non_negative_int(item.get("evidence_count"))
                    compare_branch_claim_count += _non_negative_int(item.get("claim_count"))
                    provider_input = item.get("provider_input_tokens")
                    provider_output = item.get("provider_output_tokens")
                    if isinstance(provider_input, int) and isinstance(provider_output, int):
                        compare_provider_input_tokens += max(0, provider_input)
                        compare_provider_output_tokens += max(0, provider_output)
                        compare_provider_token_samples += 1
                    error_category = str(item.get("error_category") or "").strip()[:32]
                    if error_category:
                        compare_branch_errors[error_category] += 1
            stage_timings = _dict(trace.get("stage_timings_ms"))
            validation_ms = sum(
                _non_negative_int(stage_timings.get(key))
                for key in ("citation_validation", "answer_support")
            )
            if validation_ms:
                compare_validation_durations.append(validation_ms)
        run_status = str(getattr(run, "status", ""))
        if run_status in {"completed", "failed", "cancelled"}:
            skill_terminal[skill] += 1
        if run_status == "completed":
            skill_completed[skill] += 1

    token_before = sum(before_tokens)
    token_after = sum(after_tokens)
    compression_rate = max(0.0, min(1.0, 1 - token_after / token_before)) if token_before else 0.0
    confidence_bands = {
        "high": sum(1 for value in confidences if value >= 0.8),
        "medium": sum(1 for value in confidences if 0.55 <= value < 0.8),
        "clarify": sum(1 for value in confidences if value < 0.55),
    }

    tool_statuses: Counter[str] = Counter()
    tool_names: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    durations: list[int] = []
    retries = 0
    mcp_calls = 0
    mcp_success = 0
    for call in tool_calls:
        status = str(getattr(call, "status", "unknown"))[:32]
        name = str(getattr(call, "tool_name", "unknown"))[:160]
        error = str(getattr(call, "error_code", None) or "none")[:80]
        tool_statuses[status] += 1
        tool_names[name] += 1
        tool_errors[error] += 1
        duration = getattr(call, "duration_ms", None)
        if isinstance(duration, int):
            durations.append(max(0, duration))
        if int(getattr(call, "attempt", 1) or 1) > 1:
            retries += 1
        if name.startswith("mcp__"):
            mcp_calls += 1
            if status == "succeeded":
                mcp_success += 1

    server_metrics = [
        {
            "id": str(getattr(item, "id", "unknown"))[:64],
            "display_name": str(getattr(item, "display_name", "MCP"))[:100],
            "enabled": bool(getattr(item, "enabled", False)),
            "health_status": str(getattr(item, "health_status", "unknown"))[:32],
            "consecutive_failures": max(0, int(getattr(item, "consecutive_failures", 0) or 0)),
            "circuit_open_until": getattr(item, "circuit_open_until", None),
            "last_checked_at": getattr(item, "last_checked_at", None),
            "last_error_code": str(getattr(item, "last_error_code", None) or "")[:80] or None,
        }
        for item in mcp_servers
    ]

    return {
        "window_hours": window_hours,
        "generated_at": datetime.now(timezone.utc),
        "limit_reached": limit_reached,
        "privacy": {
            "content_collected": False,
            "identifiers_collected": False,
        },
        "context": {
            "runs": len(context_runs),
            "compacted_runs": compacted,
            "compression_rate": compression_rate,
            "tokens_before": token_before,
            "tokens_after": token_after,
            "build_p50_ms": _percentile(context_latency, 0.5),
            "build_p95_ms": _percentile(context_latency, 0.95),
            "context_limit_errors": context_limit_errors,
            "context_limit_rate": _rate(context_limit_errors, len(context_runs)),
            "reference_confidence_average": (
                sum(confidences) / len(confidences) if confidences else None
            ),
            "reference_confidence_bands": confidence_bands,
            "clarification_rate": _rate(clarification, len(confidences)),
        },
        "memory": memory,
        "skills": {
            "runs": sum(skill_counts.values()),
            "distribution": [
                {
                    "skill": skill,
                    "runs": count,
                    "terminal_runs": skill_terminal[skill],
                    "completion_rate": _rate(skill_completed[skill], skill_terminal[skill]),
                }
                for skill, count in skill_counts.most_common()
            ],
            "route_sources": dict(route_sources),
            "fallback_runs": route_sources.get("deterministic_fallback", 0)
            + skill_counts.get("legacy_agent", 0),
        },
        "tools": {
            "calls": len(tool_calls),
            "successful": tool_statuses.get("succeeded", 0),
            "success_rate": _rate(tool_statuses.get("succeeded", 0), len(tool_calls)),
            "p50_ms": _percentile(durations, 0.5),
            "p95_ms": _percentile(durations, 0.95),
            "retried_calls": retries,
            "timeouts": sum(count for code, count in tool_errors.items() if "TIMEOUT" in code),
            "permission_denied": sum(
                count for code, count in tool_errors.items() if "PERMISSION" in code
            ),
            "statuses": dict(tool_statuses),
            "distribution": [
                {"tool": name, "calls": count} for name, count in tool_names.most_common()
            ],
            "error_categories": {
                code: count for code, count in tool_errors.items() if code != "none"
            },
        },
        "mcp": {
            "calls": mcp_calls,
            "successful": mcp_success,
            "success_rate": _rate(mcp_success, mcp_calls),
            "servers": server_metrics,
        },
        "embedding": {
            **dict(embedding or {}),
            "fallback_reasons": dict(vector_fallback_reasons),
            "fallback_runs": sum(vector_fallback_reasons.values()),
        },
        "parallel_compare": {
            "runs": compare_runs,
            "planned_subtasks": compare_planned_subtasks,
            "succeeded_subtasks": compare_succeeded_subtasks,
            "failed_subtasks": compare_failed_subtasks,
            "timeout_subtasks": compare_timeout_subtasks,
            "success_rate": _rate(compare_succeeded_subtasks, compare_planned_subtasks),
            "partial_runs": compare_partial_runs,
            "partial_rate": _rate(compare_partial_runs, compare_runs),
            "fallback_runs": compare_fallback_runs,
            "fallback_rate": _rate(compare_fallback_runs, compare_runs),
            "fallback_reasons": dict(compare_fallback_reasons),
            "subtask_p50_ms": _percentile(compare_subtask_durations, 0.5),
            "subtask_p95_ms": _percentile(compare_subtask_durations, 0.95),
            "merge_p50_ms": _percentile(compare_merge_durations, 0.5),
            "merge_p95_ms": _percentile(compare_merge_durations, 0.95),
            "finding_count": compare_finding_count,
            "dedup_count": compare_dedup_count,
            "conflict_count": compare_conflict_count,
            "paper_coverage_count": compare_paper_coverage_count,
            "branch_evidence_count": compare_branch_evidence_count,
            "branch_claim_count": compare_branch_claim_count,
            "estimated_branch_input_tokens": compare_branch_input_tokens,
            "estimated_branch_output_tokens": compare_branch_output_tokens,
            "provider_branch_input_tokens": (
                compare_provider_input_tokens if compare_provider_token_samples else None
            ),
            "provider_branch_output_tokens": (
                compare_provider_output_tokens if compare_provider_token_samples else None
            ),
            "provider_token_samples": compare_provider_token_samples,
            "branch_error_categories": dict(compare_branch_errors),
            "validation_p50_ms": _percentile(compare_validation_durations, 0.5),
            "validation_p95_ms": _percentile(compare_validation_durations, 0.95),
            "versions": dict(compare_versions),
        },
    }
