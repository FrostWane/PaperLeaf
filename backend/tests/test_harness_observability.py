from __future__ import annotations

from types import SimpleNamespace

import pytest

from paperleaf_api.harness_observability import aggregate_harness_metrics


def _report(*traces: dict) -> dict:
    runs = [
        SimpleNamespace(
            status="completed",
            context_snapshot={},
            reference_confidence=None,
            selected_skill="compare_papers_v2",
            harness_trace=trace,
            result_summary={},
            error_code=None,
        )
        for trace in traces
    ]
    return aggregate_harness_metrics(
        runs,
        [],
        {},
        [],
        window_hours=24,
        limit_reached=False,
    )


def test_parallel_compare_aggregates_only_versioned_low_cardinality_trace() -> None:
    report = _report(
        {
            "orchestration_version": "compare_map_reduce_v2",
            "planned_subtasks": 3,
            "succeeded_subtasks": 2,
            "failed_subtasks": 1,
            "timeout_subtasks": 1,
            "partial_failure": True,
            "fallback_to_v1": False,
            "subtask_durations_ms": [100, 300, 200],
            "merge_duration_ms": 80,
            "merge_dedup_count": 4,
            "merge_conflict_count": 2,
            "finding_count": 9,
            "private_objective": "不得出现在聚合结果",
            "paper_ids": ["private-paper-id"],
        },
        {
            "orchestration_version": "compare_map_reduce_v2",
            "planned_subtasks": 2,
            "succeeded_subtasks": 0,
            "failed_subtasks": 2,
            "fallback_to_v1": True,
            "fallback_reason": "all_subtasks_failed",
            "subtask_durations_ms": [400, 500],
            "merge_duration_ms": 120,
        },
        {
            "orchestration_version": "legacy_agent",
            "planned_subtasks": 99,
            "succeeded_subtasks": 99,
        },
    )

    metrics = report["parallel_compare"]
    assert metrics == {
        "runs": 2,
        "planned_subtasks": 5,
        "succeeded_subtasks": 2,
        "failed_subtasks": 3,
        "timeout_subtasks": 1,
        "success_rate": pytest.approx(0.4),
        "partial_runs": 1,
        "partial_rate": pytest.approx(0.5),
        "fallback_runs": 1,
        "fallback_rate": pytest.approx(0.5),
        "fallback_reasons": {"all_subtasks_failed": 1},
        "subtask_p50_ms": 300,
        "subtask_p95_ms": 500,
        "merge_p50_ms": 80,
        "merge_p95_ms": 120,
        "finding_count": 9,
        "dedup_count": 4,
        "conflict_count": 2,
        "paper_coverage_count": 0,
        "branch_evidence_count": 0,
        "branch_claim_count": 0,
        "estimated_branch_input_tokens": 0,
        "estimated_branch_output_tokens": 0,
        "provider_branch_input_tokens": None,
        "provider_branch_output_tokens": None,
        "provider_token_samples": 0,
        "branch_error_categories": {},
        "validation_p50_ms": None,
        "validation_p95_ms": None,
        "versions": {"compare_map_reduce_v2": 2},
    }
    serialized = str(report)
    assert "不得出现在聚合结果" not in serialized
    assert "private-paper-id" not in serialized


def test_parallel_compare_normalizes_unknown_reason_and_untrusted_counts() -> None:
    metrics = _report(
        {
            "orchestration_version": "compare_map_reduce_v2",
            "planned_subtasks": 999,
            "succeeded_subtasks": -3,
            "failed_subtasks": "not-a-number",
            "fallback_to_v1": True,
            "fallback_reason": "contains-private-provider-detail",
            "subtask_durations_ms": [10, 20, 30, 999999],
        }
    )["parallel_compare"]

    assert metrics["planned_subtasks"] == 3
    assert metrics["succeeded_subtasks"] == 0
    assert metrics["failed_subtasks"] == 0
    assert metrics["fallback_reasons"] == {"other": 1}
    assert metrics["subtask_p95_ms"] == 30


def test_specialist_branch_tokens_errors_and_validation_are_aggregated_without_content() -> None:
    metrics = _report(
        {
            "orchestration_version": "specialist_subgraph_v3",
            "planned_subtasks": 3,
            "succeeded_subtasks": 2,
            "failed_subtasks": 1,
            "paper_coverage_count": 3,
            "branch_metrics": [
                {
                    "subtask_id": "s1",
                    "status": "succeeded",
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "provider_input_tokens": 118,
                    "provider_output_tokens": 28,
                    "evidence_count": 4,
                    "claim_count": 3,
                },
                {
                    "subtask_id": "s2",
                    "status": "failed",
                    "input_tokens": 110,
                    "output_tokens": 0,
                    "evidence_count": 0,
                    "claim_count": 0,
                    "error_category": "schema",
                },
            ],
        }
    )["parallel_compare"]

    assert metrics["versions"] == {"specialist_subgraph_v3": 1}
    assert metrics["paper_coverage_count"] == 3
    assert metrics["branch_evidence_count"] == 4
    assert metrics["branch_claim_count"] == 3
    assert metrics["estimated_branch_input_tokens"] == 230
    assert metrics["estimated_branch_output_tokens"] == 30
    assert metrics["provider_branch_input_tokens"] == 118
    assert metrics["provider_branch_output_tokens"] == 28
    assert metrics["branch_error_categories"] == {"schema": 1}
