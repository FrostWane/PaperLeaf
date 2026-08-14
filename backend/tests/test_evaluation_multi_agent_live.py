from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from paperleaf_api.evaluation_multi_agent import MultiAgentCase, MultiAgentManifest
from paperleaf_api.evaluation_multi_agent_live import (
    RAW_V1,
    RAW_V2,
    RAW_V3,
    audit_citation_records,
    build_blind_review_rows,
    build_case_readiness_matrix,
    build_not_executed_variant,
    expected_variant_order,
    normalize_arxiv_id,
    normalize_branch_counts,
    normalize_execution_path,
    normalize_production_version,
    parse_existing_run_specs,
    quality_decision,
    resolve_private_output_path,
    run_live_capture,
)

EVALUATION_ROOT = Path(__file__).parents[1] / "evaluation"
DATASET_ROOT = EVALUATION_ROOT / "multi-agent-compare-v1"
SOURCE_ROOT = EVALUATION_ROOT / "datasets" / "paperleaf-rag-v1"


def test_normalizes_production_version_and_actual_execution_path() -> None:
    assert normalize_production_version(RAW_V1) == "v1"
    assert normalize_production_version(RAW_V2) == "v2"
    assert normalize_production_version(RAW_V3) == "v3"
    assert normalize_production_version("future_v4") is None

    assert normalize_execution_path(RAW_V1, {}) == "v1"
    assert normalize_execution_path(RAW_V2, {"fallback_to_v1": True}) == "v1"
    assert (
        normalize_execution_path(
            RAW_V2,
            {
                "compare_mode": "parallel_map_reduce",
                "tool_output_used": True,
                "planned_subtasks": 3,
            },
        )
        == "v2"
    )
    assert normalize_execution_path(RAW_V2, {"planned_subtasks": 0}) == "v1"
    assert (
        normalize_execution_path(
            RAW_V3,
            {
                "compare_mode": "bounded_specialists",
                "tool_output_used": True,
                "planned_subtasks": 3,
            },
        )
        == "v3"
    )
    assert normalize_execution_path("future_v4", {}) == "not_measured"


def test_existing_run_specs_are_complete_and_unambiguous() -> None:
    assert parse_existing_run_specs(
        ["sys-01:v1:run-1", "sys-01:v2:run-2", "sys-01:v3:run-3"]
    ) == {
        ("sys-01", "v1"): "run-1",
        ("sys-01", "v2"): "run-2",
        ("sys-01", "v3"): "run-3",
    }
    with pytest.raises(ValueError, match="不允许"):
        parse_existing_run_specs(["sys-01:v1:run-1", "sys-01:v1:run-2"])
    with pytest.raises(ValueError, match="必须为"):
        parse_existing_run_specs(["sys-01:v4:run-4"])


def test_three_variant_order_and_blind_package_are_deterministic() -> None:
    order = expected_variant_order("case-1")
    assert set(order) == {"v1", "v2", "v3"}
    report = {
        "pairs": [
            {
                "case_id": "case-1",
                "input_hash": "abc",
                **{
                    label: {
                        "execution_status": "executed",
                        "answer": f"答案 {label}",
                        "citations": [],
                        "frozen_result_hash": f"hash-{label}",
                    }
                    for label in ("v1", "v2", "v3")
                },
            }
        ]
    }
    first = build_blind_review_rows(report)
    second = build_blind_review_rows(report)

    assert first == second
    assert {item["label"] for item in first[0]["options"]} == {"A", "B", "C"}
    assert set(first[0]["_private_mapping"].values()) == {"v1", "v2", "v3"}


def test_normalizes_timeout_not_as_a_second_failed_branch() -> None:
    counts = normalize_branch_counts(
        {
            "planned_subtasks": 3,
            "succeeded_subtasks": 1,
            "failed_subtasks": 2,
            "timeout_subtasks": 1,
        }
    )

    assert counts == {
        "planned": 3,
        "succeeded": 1,
        "failed": 1,
        "timed_out": 1,
        "raw_failed_including_timeout": 2,
    }
    assert counts["succeeded"] + counts["failed"] + counts["timed_out"] <= 3


def test_citation_audit_requires_scope_page_and_anchor() -> None:
    citations = [
        {"chunk_id": "chunk-ok", "paper_id": "local-resnet", "physical_page": 2},
        {"chunk_id": "chunk-wrong-page", "paper_id": "local-resnet", "physical_page": 3},
        {"chunk_id": "missing", "paper_id": "local-resnet", "physical_page": 2},
    ]
    chunks = {
        "chunk-ok": {
            "paper_id": "local-resnet",
            "physical_page": 2,
            "owner_id": "owner-1",
            "text": "The identity shortcuts are parameter-free and add no complexity.",
        },
        "chunk-wrong-page": {
            "paper_id": "local-resnet",
            "physical_page": 2,
            "owner_id": "owner-1",
            "text": "Other evidence.",
        },
    }
    source_cases = [
        {
            "id": "resnet-method",
            "expected_evidence": [
                {
                    "paper_id": "arxiv:1512.03385v1",
                    "physical_page": 2,
                    "anchor": "identity shortcuts are parameter-free",
                }
            ],
        }
    ]

    result = audit_citation_records(
        citations=citations,
        chunk_records=chunks,
        logical_to_local={"arxiv:1512.03385v1": "local-resnet"},
        owner_id="owner-1",
        source_case_ids=["resnet-method"],
        source_cases=source_cases,
    )

    assert result["total_citations"] == 3
    assert result["correct_page_citations"] == 1
    assert result["illegal_citation_count"] == 2
    assert result["covered_source_case_ids"] == ["resnet-method"]
    assert result["cited_paper_ids"] == ["arxiv:1512.03385v1"]


def test_citation_audit_rejects_same_page_without_anchor_and_cross_user_chunk() -> None:
    result = audit_citation_records(
        citations=[
            {"chunk_id": "same-page", "paper_id": "local", "physical_page": 1},
            {"chunk_id": "foreign", "paper_id": "foreign-paper", "physical_page": 1},
        ],
        chunk_records={
            "same-page": {
                "paper_id": "local",
                "physical_page": 1,
                "owner_id": "owner-1",
                "text": "同一页中与冻结主张无关的段落。",
            },
            "foreign": {
                "paper_id": "foreign-paper",
                "physical_page": 1,
                "owner_id": "owner-2",
                "text": "secret",
            },
        },
        logical_to_local={"arxiv:0000.00001v1": "local"},
        owner_id="owner-1",
        source_case_ids=["source-1"],
        source_cases=[
            {
                "id": "source-1",
                "expected_evidence": [
                    {
                        "paper_id": "arxiv:0000.00001v1",
                        "physical_page": 1,
                        "anchor": "冻结证据锚点",
                    }
                ],
            }
        ],
    )

    assert result["covered_source_case_ids"] == []
    assert result["cross_user_leak_count"] == 1
    assert result["illegal_citation_count"] == 1


def test_arxiv_normalization_keeps_dataset_alias_but_matches_local_base_id() -> None:
    assert normalize_arxiv_id("arxiv:1512.03385v1") == "1512.03385"
    assert normalize_arxiv_id("https://arxiv.org/pdf/1512.03385v4.pdf") == "1512.03385"


def test_output_is_restricted_to_private_outputs(tmp_path: Path) -> None:
    requested = tmp_path / "outputs" / "private"
    resolved = resolve_private_output_path(
        requested,
        now=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
    )
    assert resolved == requested / "multi-agent-live-20260812T120000Z.json"
    assert resolve_private_output_path(requested / "fixed.json") == requested / "fixed.json"
    with pytest.raises(ValueError, match="outputs/private"):
        resolve_private_output_path(tmp_path / "public" / "report.json")


def test_not_executed_variant_never_fabricates_run_id() -> None:
    result = build_not_executed_variant("v2", "missing_fixture")

    assert result["execution_status"] == "not_executed"
    assert result["run_id"] is None
    assert result["measurements"]["covered_dimensions"]["status"] == "not_measured"
    assert result["measurements"]["presented_conflicts"]["status"] == "not_measured"
    assert result["measurements"]["partial_failure_notice"]["status"] == "not_measured"


def test_case_readiness_matrix_separates_papers_flags_and_http_fixtures() -> None:
    cases = [
        MultiAgentCase(
            id="single",
            category="single_paper_control",
            split="test",
            query="单篇问题",
            scope_paper_ids=["paper-a"],
            answerable=True,
            expected_path="v1",
        ),
        MultiAgentCase(
            id="complex",
            category="systematic_compare",
            split="test",
            query="复杂问题",
            scope_paper_ids=["paper-a", "paper-b", "paper-c"],
            source_case_ids=["source-a"],
            answerable=True,
            expected_path="v2",
            dimensions=["方法"],
        ),
        MultiAgentCase(
            id="scope",
            category="scope_violation_control",
            split="test",
            query="越权问题",
            scope_paper_ids=["paper-a"],
            answerable=False,
            expected_path="pregraph_reject",
        ),
    ]

    matrix = build_case_readiness_matrix(
        cases,
        {"paper-a": "local-a", "paper-b": "local-b"},
        missing_paper_ids=["paper-c"],
        skills_enabled=True,
        multi_agent_enabled=False,
        answer_model_configured=True,
    )

    assert matrix["ready_case_count"] == 1
    assert matrix["ready_v1_case_count"] == 1
    assert matrix["ready_v2_case_count"] == 0
    assert matrix["fixture_required_case_count"] == 1
    assert matrix["cases"][1]["reasons"] == [
        "required_papers_missing",
        "multi_agent_feature_disabled",
    ]
    assert matrix["cases"][2]["reasons"] == ["requires_http_pregraph_fixture"]


def test_draft_manifest_is_always_quality_pending() -> None:
    manifest = MultiAgentManifest.model_validate_json(
        (DATASET_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.annotation_status == "draft"
    assert quality_decision(manifest) == "quality_pending"


def test_live_capture_rejects_empty_variant_selection(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="variants"):
        asyncio.run(
            run_live_capture(
                manifest_path=DATASET_ROOT / "manifest.json",
                cases_path=DATASET_ROOT / "cases.jsonl",
                source_manifest_path=SOURCE_ROOT / "manifest.json",
                source_cases_path=SOURCE_ROOT / "cases.jsonl",
                output_path=tmp_path / "outputs" / "private" / "empty.json",
                split="dev",
                limit=1,
                variants=(),
            )
        )


def test_failed_preflight_writes_not_executed_without_run_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_preflight(**_: object) -> tuple[dict[str, object], None, dict[str, str]]:
        return {"status": "failed", "reasons": ["required_papers_missing"]}, None, {}

    monkeypatch.setattr(
        "paperleaf_api.evaluation_multi_agent_live._preflight",
        failed_preflight,
    )
    output = tmp_path / "outputs" / "private" / "not-executed.json"

    report = asyncio.run(
        run_live_capture(
            manifest_path=DATASET_ROOT / "manifest.json",
            cases_path=DATASET_ROOT / "cases.jsonl",
            source_manifest_path=SOURCE_ROOT / "manifest.json",
            source_cases_path=SOURCE_ROOT / "cases.jsonl",
            output_path=output,
            split="dev",
            limit=1,
        )
    )

    assert report["execution_status"] == "not_executed"
    assert report["quality_decision"] == "quality_pending"
    assert report["pairs"][0]["v1"]["run_id"] is None
    assert report["pairs"][0]["v2"]["run_id"] is None
    assert report["pairs"][0]["v3"]["run_id"] is None
    assert json_from(output)["evidence_level"] == "not_executed"
    blind_path = output.with_name("not-executed-blind.jsonl")
    blind_key_path = output.with_name("not-executed-blind-key.jsonl")
    assert blind_path.exists()
    assert blind_key_path.exists()
    assert "_private_mapping" not in blind_path.read_text(encoding="utf-8")


def test_preflight_only_never_submits_model_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def passed_preflight(**_: object) -> tuple[dict[str, object], str, dict[str, str]]:
        return {"status": "passed", "reasons": []}, "owner-1", {}

    monkeypatch.setattr(
        "paperleaf_api.evaluation_multi_agent_live._preflight",
        passed_preflight,
    )
    output = tmp_path / "outputs" / "private" / "preflight.json"

    report = asyncio.run(
        run_live_capture(
            manifest_path=DATASET_ROOT / "manifest.json",
            cases_path=DATASET_ROOT / "cases.jsonl",
            source_manifest_path=SOURCE_ROOT / "manifest.json",
            source_cases_path=SOURCE_ROOT / "cases.jsonl",
            output_path=output,
            split="dev",
            limit=1,
            preflight_only=True,
        )
    )

    assert report["execution_status"] == "preflight_passed"
    assert report["evidence_level"] == "real_infrastructure_preflight"
    assert report["pairs"] == []
    assert "executed_run_count" not in report


def json_from(path: Path) -> dict[str, object]:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
