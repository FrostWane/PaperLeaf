import json
from pathlib import Path

import pytest

from paperleaf_api.evaluation_multi_agent import (
    FaultCheck,
    MultiAgentCase,
    MultiAgentManifest,
    PairedRun,
    VariantRun,
    ceiling_aware_improvement,
    evaluate_ab,
    expected_ab_order,
    query_hash,
    read_jsonl,
    scope_hash,
    sha256_file,
    validate_dataset,
    validate_source_hashes,
)

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "evaluation" / "multi-agent-compare-v1"
SOURCE_CASES = ROOT / "evaluation" / "datasets" / "paperleaf-rag-v1" / "cases.jsonl"
SOURCE_MANIFEST = ROOT / "evaluation" / "datasets" / "paperleaf-rag-v1" / "manifest.json"
HEX = "a" * 64


def _manifest() -> MultiAgentManifest:
    return MultiAgentManifest.model_validate_json(
        (DATASET / "manifest.json").read_text(encoding="utf-8")
    )


def _cases() -> list[MultiAgentCase]:
    return read_jsonl(DATASET / "cases.jsonl", MultiAgentCase)


def _run(
    case: MultiAgentCase,
    version: str,
    *,
    complete: bool,
    illegal: int = 0,
    model_hash: str = HEX,
) -> VariantRun:
    sources = case.source_case_ids if complete else case.source_case_ids[:1]
    papers = case.scope_paper_ids if complete else case.scope_paper_ids[:1]
    dimensions = case.dimensions if complete else case.dimensions[:1]
    is_rejected = case.expected_path == "pregraph_reject"
    citations = 0 if is_rejected or not case.answerable else max(1, len(sources))
    return VariantRun(
        run_id=f"{case.id}-{version}",
        orchestration_version=version,
        executed_path=(case.expected_path if version == "v2" else "v1"),
        status="rejected" if is_rejected else "completed",
        query_hash=query_hash(case),
        scope_hash=scope_hash(case),
        input_hash=HEX,
        collection_snapshot_hash=HEX,
        model_config_hash=model_hash,
        covered_source_case_ids=sources,
        cited_paper_ids=papers,
        covered_dimensions=dimensions,
        presented_conflicts=case.expected_conflicts if complete else [],
        supported_output_claims=0 if is_rejected else max(1, len(sources)),
        total_output_claims=0 if is_rejected else max(1, len(sources)),
        correct_page_citations=citations,
        total_citations=citations,
        illegal_citation_count=illegal,
        duration_ms=200 if version == "v1" else 300,
        first_verified_delta_ms=100,
        estimated_input_tokens=100 if version == "v1" else 160,
        estimated_output_tokens=50 if version == "v1" else 80,
        model_call_count=1 if version == "v1" else 3,
        tool_call_count=1 if version == "v1" else 3,
        branches_planned=(
            min(3, len(case.scope_paper_ids))
            if version == "v2" and case.expected_path == "v2"
            else 0
        ),
        branches_succeeded=(
            min(3, len(case.scope_paper_ids))
            if version == "v2" and case.expected_path == "v2"
            else 0
        ),
    )


def _pair(case: MultiAgentCase, *, illegal: int = 0) -> PairedRun:
    return PairedRun(
        case_id=case.id,
        order=expected_ab_order(case.id),
        v1=_run(case, "v1", complete=False),
        v2=_run(case, "v2", complete=True, illegal=illegal),
    )


def _fault_checks() -> list[FaultCheck]:
    return [
        FaultCheck(name=name, passed=True, numerator=1, denominator=1)
        for name in (
            "all_failed_fallback",
            "partial_failure_notice",
            "cancel_propagation",
            "lease_loss_fencing",
            "checkpoint_resume",
        )
    ]


def test_draft_dataset_has_declared_mix_and_only_reuses_source_cases() -> None:
    source_cases = [
        json.loads(line)
        for line in SOURCE_CASES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    report = validate_dataset(_manifest(), _cases(), source_cases)

    assert report["annotation_status"] == "draft"
    assert report["quality_claims_allowed"] is False
    assert report["case_count"] == 48
    assert report["complex_case_count"] == 36
    assert report["control_case_count"] == 12
    assert report["split_counts"] == {"dev": 8, "test": 40}
    assert all(
        3 <= len(case.scope_paper_ids) <= 10
        for case in _cases()
        if case.expected_path == "v2"
        and case.category.endswith(("compare", "verification", "trajectory"))
    )


def test_draft_manifest_cannot_claim_frozen_quality() -> None:
    payload = _manifest().model_dump()
    payload["frozen"] = True

    with pytest.raises(ValueError, match="draft 数据集不得"):
        MultiAgentManifest.model_validate(payload)


def test_draft_dataset_locks_source_manifest_and_cases_hashes(tmp_path: Path) -> None:
    validate_source_hashes(
        _manifest(),
        source_manifest_path=SOURCE_MANIFEST,
        source_cases_path=SOURCE_CASES,
    )
    drifted = tmp_path / "cases.jsonl"
    drifted.write_text(SOURCE_CASES.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="源 cases SHA-256 已漂移"):
        validate_source_hashes(
            _manifest(),
            source_manifest_path=SOURCE_MANIFEST,
            source_cases_path=drifted,
        )


def test_source_hash_is_stable_across_lf_and_crlf_checkouts(tmp_path: Path) -> None:
    lf = tmp_path / "lf.jsonl"
    crlf = tmp_path / "crlf.jsonl"
    lf.write_bytes(b'{"id":"one"}\n{"id":"two"}\n')
    crlf.write_bytes(b'{"id":"one"}\r\n{"id":"two"}\r\n')

    assert sha256_file(lf) == sha256_file(crlf)


def test_ceiling_aware_gate_uses_non_regression_near_ceiling() -> None:
    assert ceiling_aware_improvement(0.70, 0.76, minimum_delta=0.05)["passed"] is True
    assert ceiling_aware_improvement(0.96, 0.96, minimum_delta=0.05) == {
        "passed": True,
        "reason": "ceiling_non_regression",
        "delta": 0.0,
    }
    assert ceiling_aware_improvement(0.96, 0.95, minimum_delta=0.05)["passed"] is False


def test_paired_ab_is_quality_pending_without_frozen_annotations_and_human_review() -> None:
    cases = _cases()
    test_cases = [case for case in cases if case.split == "test"]

    report = evaluate_ab(
        _manifest(),
        cases,
        [_pair(case) for case in test_cases],
        fault_checks=_fault_checks(),
    )

    assert report["protocol"]["errors"] == []
    assert report["protocol"]["paired_case_count"] == 40
    assert report["protocol"]["token_measurement"] == "estimated_not_provider_billed_usage"
    assert len(report["pairs"]) == 40
    assert report["pairs"][0]["delta"]["duration_ms"] == 100
    assert report["gates_frozen_before_run"]["maximum_cost_ratio"] == 2.0
    assert report["gates"]["hard"]["passed"] is True
    assert report["gates"]["engineering"]["passed"] is True
    assert report["gates"]["cost"]["passed"] is True
    assert report["decision"] == "quality_pending"
    assert "不允许声称" in report["decision_note"]


def test_pair_with_different_model_config_is_protocol_invalid() -> None:
    case = next(case for case in _cases() if case.split == "test")
    pair = _pair(case)
    pair.v2.model_config_hash = "b" * 64

    report = evaluate_ab(
        _manifest(),
        [case],
        [pair],
        fault_checks=_fault_checks(),
        require_complete=False,
    )

    assert report["decision"] == "protocol_invalid"
    assert report["protocol"]["errors"] == [
        {"case_id": case.id, "errors": ["paired_model_config_hash"]}
    ]


def test_pair_order_is_deterministic_and_wrong_order_is_protocol_invalid() -> None:
    case = next(case for case in _cases() if case.split == "test")
    pair = _pair(case)
    pair.order = "v2_v1" if pair.order == "v1_v2" else "v1_v2"

    report = evaluate_ab(
        _manifest(),
        [case],
        [pair],
        fault_checks=_fault_checks(),
        require_complete=False,
    )

    assert report["decision"] == "protocol_invalid"
    assert report["protocol"]["errors"] == [{"case_id": case.id, "errors": ["ab_order"]}]


def test_any_illegal_citation_causes_hard_no_go() -> None:
    case = next(
        case for case in _cases() if case.split == "test" and case.category == "systematic_compare"
    )

    report = evaluate_ab(
        _manifest(),
        [case],
        [_pair(case, illegal=1)],
        fault_checks=_fault_checks(),
        require_complete=False,
    )

    assert report["gates"]["hard"]["passed"] is False
    assert report["gates"]["hard"]["counts"]["illegal_citation"] == 1
    assert report["decision"] == "no_go"


def test_missing_failure_injection_checks_blocks_engineering_gate() -> None:
    case = next(
        case for case in _cases() if case.split == "test" and case.category == "systematic_compare"
    )

    report = evaluate_ab(
        _manifest(),
        [case],
        [_pair(case)],
        fault_checks=[],
        require_complete=False,
    )

    assert report["gates"]["engineering"]["passed"] is False
    assert set(report["gates"]["engineering"]["missing_fault_checks"]) == {
        "all_failed_fallback",
        "partial_failure_notice",
        "cancel_propagation",
        "lease_loss_fencing",
        "checkpoint_resume",
    }
    assert report["decision"] == "no_go"


def test_more_than_three_branches_causes_hard_no_go() -> None:
    case = next(
        case for case in _cases() if case.split == "test" and len(case.scope_paper_ids) >= 4
    )
    pair = _pair(case)
    pair.v2.branches_planned = 4

    report = evaluate_ab(
        _manifest(),
        [case],
        [pair],
        fault_checks=_fault_checks(),
        require_complete=False,
    )

    assert report["variants"]["v2"]["max_branches"] == 4
    assert report["gates"]["hard"]["passed"] is False
    assert report["decision"] == "no_go"


def test_latency_or_estimated_tokens_over_twice_v1_causes_cost_no_go() -> None:
    case = next(
        case for case in _cases() if case.split == "test" and case.category == "systematic_compare"
    )
    pair = _pair(case)
    pair.v2.duration_ms = pair.v1.duration_ms * 2 + 1
    pair.v2.estimated_input_tokens = 1000

    report = evaluate_ab(
        _manifest(),
        [case],
        [pair],
        fault_checks=_fault_checks(),
        require_complete=False,
    )

    assert report["gates"]["cost"]["passed"] is False
    assert report["gates"]["cost"]["latency_p95_ratio"] > 2
    assert report["gates"]["cost"]["estimated_token_ratio"] > 2
    assert report["decision"] == "no_go"
