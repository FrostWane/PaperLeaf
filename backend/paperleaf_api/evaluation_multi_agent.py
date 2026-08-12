"""PaperLeaf 并行 Map-Reduce RAG 的冻结数据与配对 A/B 评测协议。

本模块只负责评测数据校验和指标聚合，不调用生产 Agent，也不把 draft 标注
包装成质量结论。包含回答或论文片段的逐题结果应保存在私有测试目录。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

COMPLEX_CATEGORIES = {
    "systematic_compare",
    "composite_claim_verification",
    "research_trajectory",
}
CONTROL_CATEGORIES = {
    "single_paper_control",
    "unanswerable_control",
    "prompt_injection_control",
    "scope_violation_control",
}
REQUIRED_FAULT_CHECKS = {
    "all_failed_fallback",
    "partial_failure_notice",
    "cancel_propagation",
    "lease_loss_fencing",
    "checkpoint_resume",
}


class EvaluationThresholds(BaseModel):
    minimum_quality_delta: float = Field(ge=0, le=1)
    minimum_paper_coverage_delta: float = Field(ge=0, le=1)
    maximum_quality_regression: float = Field(ge=0, le=1)
    minimum_human_net_win: float = Field(ge=-1, le=1)
    minimum_citation_page_accuracy: float = Field(ge=0, le=1)
    maximum_unanswerable_wrong_rate: float = Field(ge=0, le=1)
    minimum_complex_completion_rate: float = Field(ge=0, le=1)
    maximum_cost_ratio: float = Field(ge=1)


class MultiAgentManifest(BaseModel):
    schema_version: int = 1
    dataset_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    annotation_status: Literal["draft", "reviewed", "frozen"]
    frozen: bool
    quality_claims_allowed: bool
    source_dataset_id: str
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_cases_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_count: int = Field(gt=0)
    complex_case_count: int = Field(ge=0)
    control_case_count: int = Field(ge=0)
    category_counts: dict[str, int]
    split_counts: dict[str, int]
    thresholds: EvaluationThresholds

    @model_validator(mode="after")
    def status_is_honest(self) -> MultiAgentManifest:
        if self.annotation_status == "draft" and (self.frozen or self.quality_claims_allowed):
            raise ValueError("draft 数据集不得标记 frozen 或允许质量宣传")
        if self.annotation_status == "frozen" and not self.frozen:
            raise ValueError("frozen annotation_status 必须同时设置 frozen=true")
        return self


class SafetyExpectation(BaseModel):
    forbidden_tools: list[str] = Field(default_factory=list)
    forbid_write: bool = True
    expected_rejection_reason: str | None = None


class MultiAgentCase(BaseModel):
    id: str = Field(min_length=1)
    category: str
    split: Literal["dev", "test"]
    query: str = Field(min_length=1)
    scope_paper_ids: list[str] = Field(min_length=1)
    source_case_ids: list[str] = Field(default_factory=list)
    answerable: bool
    expected_path: Literal["v1", "v2", "pregraph_reject"]
    dimensions: list[str] = Field(default_factory=list)
    expected_conflicts: list[str] = Field(default_factory=list)
    minimum_distinct_papers: int = Field(default=0, ge=0)
    safety: SafetyExpectation = Field(default_factory=SafetyExpectation)

    @model_validator(mode="after")
    def validate_shape(self) -> MultiAgentCase:
        if len(set(self.scope_paper_ids)) != len(self.scope_paper_ids):
            raise ValueError("scope_paper_ids 不得重复")
        if len(set(self.source_case_ids)) != len(self.source_case_ids):
            raise ValueError("source_case_ids 不得重复")
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError("dimensions 不得重复")
        if self.category in COMPLEX_CATEGORIES:
            if not 3 <= len(self.scope_paper_ids) <= 10:
                raise ValueError("复杂任务必须冻结 3 至 10 篇论文")
            if self.expected_path != "v2":
                raise ValueError("复杂任务必须期望进入 v2")
            if not self.source_case_ids or not self.dimensions:
                raise ValueError("复杂任务必须声明来源题和比较维度")
        if self.category == "single_paper_control":
            if len(self.scope_paper_ids) != 1 or self.expected_path != "v1":
                raise ValueError("单篇对照必须继续走 v1")
        if self.category == "scope_violation_control" and self.expected_path != "pregraph_reject":
            raise ValueError("越权对照必须在进入图前拒绝")
        if self.minimum_distinct_papers > len(self.scope_paper_ids):
            raise ValueError("minimum_distinct_papers 超过冻结 scope")
        return self


class VariantRun(BaseModel):
    run_id: str = Field(min_length=1)
    orchestration_version: Literal["v1", "v2"]
    executed_path: Literal["v1", "v2", "pregraph_reject"]
    status: Literal["completed", "failed", "cancelled", "rejected"]
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    covered_source_case_ids: list[str] = Field(default_factory=list)
    cited_paper_ids: list[str] = Field(default_factory=list)
    covered_dimensions: list[str] = Field(default_factory=list)
    presented_conflicts: list[str] = Field(default_factory=list)
    supported_output_claims: int = Field(default=0, ge=0)
    total_output_claims: int = Field(default=0, ge=0)
    correct_page_citations: int = Field(default=0, ge=0)
    total_citations: int = Field(default=0, ge=0)
    illegal_citation_count: int = Field(default=0, ge=0)
    scope_violation_count: int = Field(default=0, ge=0)
    cross_user_leak_count: int = Field(default=0, ge=0)
    unapproved_write_count: int = Field(default=0, ge=0)
    prompt_injection_success_count: int = Field(default=0, ge=0)
    context_budget_exceeded_count: int = Field(default=0, ge=0)
    wrong_answer_on_unanswerable: bool = False
    duration_ms: int = Field(ge=0)
    first_verified_delta_ms: int | None = Field(default=None, ge=0)
    estimated_input_tokens: int = Field(default=0, ge=0)
    estimated_output_tokens: int = Field(default=0, ge=0)
    model_call_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    branches_planned: int = Field(default=0, ge=0)
    branches_succeeded: int = Field(default=0, ge=0)
    branches_failed: int = Field(default=0, ge=0)
    branches_timed_out: int = Field(default=0, ge=0)
    fallback_to_v1: bool = False
    partial_failure_notice: bool = False

    @model_validator(mode="after")
    def counts_are_consistent(self) -> VariantRun:
        if self.supported_output_claims > self.total_output_claims:
            raise ValueError("supported_output_claims 不得超过总主张数")
        if self.correct_page_citations > self.total_citations:
            raise ValueError("correct_page_citations 不得超过总引用数")
        terminal_branches = self.branches_succeeded + self.branches_failed + self.branches_timed_out
        if terminal_branches > self.branches_planned:
            raise ValueError("终态分支数不得超过计划分支数")
        return self


class PairedRun(BaseModel):
    case_id: str
    order: Literal["v1_v2", "v2_v1"]
    v1: VariantRun
    v2: VariantRun


class FaultCheck(BaseModel):
    name: str
    passed: bool
    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)


class HumanPairRating(BaseModel):
    case_id: str
    preferred: Literal["v1", "v2", "tie"]
    v1_factuality: float = Field(ge=1, le=5)
    v2_factuality: float = Field(ge=1, le=5)
    v1_usefulness: float = Field(ge=1, le=5)
    v2_usefulness: float = Field(ge=1, le=5)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_source_hashes(
    manifest: MultiAgentManifest,
    *,
    source_manifest_path: Path,
    source_cases_path: Path,
) -> None:
    errors: list[str] = []
    if sha256_file(source_manifest_path) != manifest.source_manifest_sha256:
        errors.append("源 manifest SHA-256 已漂移")
    if sha256_file(source_cases_path) != manifest.source_cases_sha256:
        errors.append("源 cases SHA-256 已漂移")
    if errors:
        raise ValueError("；".join(errors))


def query_hash(case: MultiAgentCase) -> str:
    return hashlib.sha256(case.query.encode("utf-8")).hexdigest()


def scope_hash(case: MultiAgentCase) -> str:
    return _sha256_json(sorted(case.scope_paper_ids))


def expected_ab_order(case_id: str) -> Literal["v1_v2", "v2_v1"]:
    return "v1_v2" if int(hashlib.sha256(case_id.encode("utf-8")).hexdigest(), 16) % 2 else "v2_v1"


def read_jsonl(path: Path, model: type[BaseModel]) -> list[Any]:
    return [
        model.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def validate_dataset(
    manifest: MultiAgentManifest,
    cases: list[MultiAgentCase],
    source_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    errors: list[str] = []
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        errors.append("case id 存在重复")
    source_by_id = {str(item.get("id")): item for item in source_cases}
    for case in cases:
        for source_id in case.source_case_ids:
            source = source_by_id.get(source_id)
            if source is None:
                errors.append(f"{case.id} 引用了不存在的源题 {source_id}")
                continue
            source_papers = {str(value) for value in source.get("paper_ids", [])}
            if not source_papers.issubset(set(case.scope_paper_ids)):
                errors.append(f"{case.id} 的源题 {source_id} 超出冻结 scope")

    categories = Counter(case.category for case in cases)
    splits = Counter(case.split for case in cases)
    complex_count = sum(case.category in COMPLEX_CATEGORIES for case in cases)
    control_count = sum(case.category in CONTROL_CATEGORIES for case in cases)
    expected = {
        "case_count": len(cases),
        "complex_case_count": complex_count,
        "control_case_count": control_count,
        "category_counts": dict(sorted(categories.items())),
        "split_counts": dict(sorted(splits.items())),
    }
    for key, actual in expected.items():
        declared = getattr(manifest, key)
        if declared != actual:
            errors.append(f"manifest {key}={declared!r}，实际为 {actual!r}")
    if complex_count < 36 or control_count < 12:
        errors.append("draft 数据集至少需要 36 个复杂任务和 12 个对照")
    if set(categories) - (COMPLEX_CATEGORIES | CONTROL_CATEGORIES):
        errors.append("数据集包含未知类别")
    if errors:
        raise ValueError("；".join(errors))
    return {
        "dataset_id": manifest.dataset_id,
        "annotation_status": manifest.annotation_status,
        "quality_claims_allowed": manifest.quality_claims_allowed,
        **expected,
        "source_claim_count": sum(len(case.source_case_ids) for case in cases),
    }


def _ratio(numerator: int | float, denominator: int | float) -> dict[str, Any]:
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


def ceiling_aware_improvement(
    baseline: float | None,
    candidate: float | None,
    *,
    minimum_delta: float,
    ceiling: float = 0.95,
) -> dict[str, Any]:
    if baseline is None or candidate is None:
        return {"passed": False, "reason": "missing_metric", "delta": None}
    delta = candidate - baseline
    if baseline >= ceiling:
        return {
            "passed": candidate >= baseline,
            "reason": "ceiling_non_regression",
            "delta": delta,
        }
    return {
        "passed": delta >= minimum_delta,
        "reason": "minimum_absolute_improvement",
        "delta": delta,
    }


def _variant_metrics(
    selected: list[tuple[MultiAgentCase, VariantRun]],
) -> dict[str, Any]:
    claim_hit = claim_total = 0
    supported = output_claims = 0
    cited_papers = required_papers = 0
    covered_dimensions = required_dimensions = 0
    conflicts = required_conflicts = 0
    correct_pages = citations = 0
    wrong_unanswerable = unanswerable = 0
    durations: list[int] = []
    first_deltas: list[int] = []
    estimated_tokens = model_calls = tool_calls = 0
    safety = Counter()
    max_branches = 0
    completed_complex = complex_total = 0

    for case, run in selected:
        if case.category in COMPLEX_CATEGORIES:
            complex_total += 1
            completed_complex += int(run.status == "completed")
            claim_total += len(case.source_case_ids)
            claim_hit += len(set(run.covered_source_case_ids) & set(case.source_case_ids))
            required_papers += len(case.scope_paper_ids)
            cited_papers += len(set(run.cited_paper_ids) & set(case.scope_paper_ids))
            required_dimensions += len(case.dimensions)
            covered_dimensions += len(set(run.covered_dimensions) & set(case.dimensions))
            required_conflicts += len(case.expected_conflicts)
            conflicts += len(set(run.presented_conflicts) & set(case.expected_conflicts))
        supported += run.supported_output_claims
        output_claims += run.total_output_claims
        correct_pages += run.correct_page_citations
        citations += run.total_citations
        if not case.answerable:
            unanswerable += 1
            wrong_unanswerable += int(run.wrong_answer_on_unanswerable)
        durations.append(run.duration_ms)
        if run.first_verified_delta_ms is not None:
            first_deltas.append(run.first_verified_delta_ms)
        estimated_tokens += run.estimated_input_tokens + run.estimated_output_tokens
        model_calls += run.model_call_count
        tool_calls += run.tool_call_count
        max_branches = max(max_branches, run.branches_planned)
        safety.update(
            illegal_citation=run.illegal_citation_count,
            scope_violation=run.scope_violation_count,
            cross_user_leak=run.cross_user_leak_count,
            unapproved_write=run.unapproved_write_count,
            prompt_injection_success=run.prompt_injection_success_count,
            context_budget_exceeded=run.context_budget_exceeded_count,
        )

    return {
        "expected_claim_evidence_coverage": _ratio(claim_hit, claim_total),
        "output_claim_support_precision": _ratio(supported, output_claims),
        "required_paper_coverage": _ratio(cited_papers, required_papers),
        "dimension_coverage": _ratio(covered_dimensions, required_dimensions),
        "conflict_recall": _ratio(conflicts, required_conflicts),
        "citation_page_accuracy": _ratio(correct_pages, citations),
        "unanswerable_wrong_answer_rate": _ratio(wrong_unanswerable, unanswerable),
        "complex_completion_rate": _ratio(completed_complex, complex_total),
        "duration_ms": {"p95": _p95(durations)},
        "first_verified_delta_ms": {"p95": _p95(first_deltas)},
        "estimated_tokens": estimated_tokens,
        "model_call_count": model_calls,
        "tool_call_count": tool_calls,
        "max_branches": max_branches,
        "safety": dict(safety),
    }


def _valid_pair(case: MultiAgentCase, pair: PairedRun) -> list[str]:
    errors: list[str] = []
    if pair.order != expected_ab_order(case.id):
        errors.append("ab_order")
    if pair.v1.orchestration_version != "v1" or pair.v2.orchestration_version != "v2":
        errors.append("orchestration_version")
    if pair.v1.run_id == pair.v2.run_id:
        errors.append("run_id_reused")
    expected_query_hash = query_hash(case)
    expected_scope_hash = scope_hash(case)
    for label, run in (("v1", pair.v1), ("v2", pair.v2)):
        if run.query_hash != expected_query_hash:
            errors.append(f"{label}_query_hash")
        if run.scope_hash != expected_scope_hash:
            errors.append(f"{label}_scope_hash")
        if not set(run.covered_source_case_ids).issubset(set(case.source_case_ids)):
            errors.append(f"{label}_unknown_source_claim")
        if not set(run.covered_dimensions).issubset(set(case.dimensions)):
            errors.append(f"{label}_unknown_dimension")
    for field in ("input_hash", "collection_snapshot_hash", "model_config_hash"):
        if getattr(pair.v1, field) != getattr(pair.v2, field):
            errors.append(f"paired_{field}")
    if pair.v2.executed_path != case.expected_path:
        errors.append("v2_routing")
    return errors


def _pair_summary(case: MultiAgentCase, pair: PairedRun) -> dict[str, Any]:
    def variant(run: VariantRun) -> dict[str, Any]:
        expected_claims = len(case.source_case_ids)
        required_papers = len(case.scope_paper_ids) if case.category in COMPLEX_CATEGORIES else 0
        return {
            "run_id": run.run_id,
            "orchestration_version": run.orchestration_version,
            "executed_path": run.executed_path,
            "status": run.status,
            "expected_claim_evidence_coverage": (
                len(set(run.covered_source_case_ids) & set(case.source_case_ids)) / expected_claims
                if expected_claims
                else None
            ),
            "required_paper_coverage": (
                len(set(run.cited_paper_ids) & set(case.scope_paper_ids)) / required_papers
                if required_papers
                else None
            ),
            "duration_ms": run.duration_ms,
            "estimated_tokens": run.estimated_input_tokens + run.estimated_output_tokens,
            "branches": {
                "planned": run.branches_planned,
                "succeeded": run.branches_succeeded,
                "failed": run.branches_failed,
                "timed_out": run.branches_timed_out,
            },
            "fallback_to_v1": run.fallback_to_v1,
        }

    v1 = variant(pair.v1)
    v2 = variant(pair.v2)
    return {
        "case_id": case.id,
        "order": pair.order,
        "input_hash": pair.v1.input_hash,
        "scope_hash": pair.v1.scope_hash,
        "collection_snapshot_hash": pair.v1.collection_snapshot_hash,
        "model_config_hash": pair.v1.model_config_hash,
        "v1": v1,
        "v2": v2,
        "delta": {
            "expected_claim_evidence_coverage": (
                v2["expected_claim_evidence_coverage"] - v1["expected_claim_evidence_coverage"]
                if v1["expected_claim_evidence_coverage"] is not None
                and v2["expected_claim_evidence_coverage"] is not None
                else None
            ),
            "required_paper_coverage": (
                v2["required_paper_coverage"] - v1["required_paper_coverage"]
                if v1["required_paper_coverage"] is not None
                and v2["required_paper_coverage"] is not None
                else None
            ),
            "duration_ms": v2["duration_ms"] - v1["duration_ms"],
            "estimated_tokens": v2["estimated_tokens"] - v1["estimated_tokens"],
        },
    }


def evaluate_ab(
    manifest: MultiAgentManifest,
    cases: list[MultiAgentCase],
    pairs: list[PairedRun],
    *,
    fault_checks: list[FaultCheck] | None = None,
    human_ratings: list[HumanPairRating] | None = None,
    split: Literal["dev", "test"] = "test",
    require_complete: bool = True,
) -> dict[str, Any]:
    selected_cases = {case.id: case for case in cases if case.split == split}
    pair_by_id: dict[str, PairedRun] = {}
    protocol_errors: list[dict[str, Any]] = []
    for pair in pairs:
        if pair.case_id in pair_by_id:
            protocol_errors.append({"case_id": pair.case_id, "errors": ["duplicate_pair"]})
            continue
        case = selected_cases.get(pair.case_id)
        if case is None:
            protocol_errors.append({"case_id": pair.case_id, "errors": ["unknown_case"]})
            continue
        errors = _valid_pair(case, pair)
        if errors:
            protocol_errors.append({"case_id": pair.case_id, "errors": errors})
        else:
            pair_by_id[pair.case_id] = pair
    missing = sorted(set(selected_cases) - set(pair_by_id)) if require_complete else []
    if missing:
        protocol_errors.append({"case_id": None, "errors": ["missing_pairs"], "items": missing})

    selected = [(selected_cases[case_id], pair_by_id[case_id]) for case_id in sorted(pair_by_id)]
    v1_metrics = _variant_metrics([(case, pair.v1) for case, pair in selected])
    v2_metrics = _variant_metrics([(case, pair.v2) for case, pair in selected])
    thresholds = manifest.thresholds.model_dump()
    claim_gate = ceiling_aware_improvement(
        v1_metrics["expected_claim_evidence_coverage"]["value"],
        v2_metrics["expected_claim_evidence_coverage"]["value"],
        minimum_delta=thresholds["minimum_quality_delta"],
    )
    paper_gate = ceiling_aware_improvement(
        v1_metrics["required_paper_coverage"]["value"],
        v2_metrics["required_paper_coverage"]["value"],
        minimum_delta=thresholds["minimum_paper_coverage_delta"],
    )
    v1_support = v1_metrics["output_claim_support_precision"]["value"]
    v2_support = v2_metrics["output_claim_support_precision"]["value"]
    support_non_regression = (
        v1_support is not None
        and v2_support is not None
        and v2_support >= v1_support - thresholds["maximum_quality_regression"]
    )

    ratings = human_ratings or []
    complex_test_ids = {
        case.id for case in selected_cases.values() if case.category in COMPLEX_CATEGORIES
    }
    rated_ids = {item.case_id for item in ratings if item.case_id in complex_test_ids}
    human_complete = rated_ids == complex_test_ids and bool(complex_test_ids)
    v2_wins = sum(item.preferred == "v2" for item in ratings if item.case_id in complex_test_ids)
    v1_wins = sum(item.preferred == "v1" for item in ratings if item.case_id in complex_test_ids)
    human_net_win = (v2_wins - v1_wins) / len(complex_test_ids) if human_complete else None
    human_quality_gate = (
        human_complete
        and human_net_win is not None
        and human_net_win >= thresholds["minimum_human_net_win"]
    )

    safety_counts = v2_metrics["safety"]
    hard_gate_passed = not any(safety_counts.values()) and v2_metrics["max_branches"] <= 3
    checks = {item.name: item for item in fault_checks or []}
    missing_checks = sorted(REQUIRED_FAULT_CHECKS - set(checks))
    fault_gate_passed = not missing_checks and all(
        checks[name].passed and checks[name].numerator == checks[name].denominator
        for name in REQUIRED_FAULT_CHECKS
    )
    completion = v2_metrics["complex_completion_rate"]["value"]
    engineering_gate_passed = (
        completion is not None
        and completion >= thresholds["minimum_complex_completion_rate"]
        and fault_gate_passed
    )

    v1_p95 = v1_metrics["duration_ms"]["p95"]
    v2_p95 = v2_metrics["duration_ms"]["p95"]
    latency_ratio = v2_p95 / v1_p95 if v1_p95 and v2_p95 is not None else None
    v1_tokens = v1_metrics["estimated_tokens"]
    v2_tokens = v2_metrics["estimated_tokens"]
    token_ratio = v2_tokens / v1_tokens if v1_tokens else None
    cost_gate_passed = (
        latency_ratio is not None
        and token_ratio is not None
        and latency_ratio <= thresholds["maximum_cost_ratio"]
        and token_ratio <= thresholds["maximum_cost_ratio"]
    )
    citation_accuracy = v2_metrics["citation_page_accuracy"]["value"]
    wrong_answer_rate = v2_metrics["unanswerable_wrong_answer_rate"]["value"]
    quality_gate_passed = (
        (claim_gate["passed"] or human_quality_gate)
        and paper_gate["passed"]
        and support_non_regression
        and citation_accuracy is not None
        and citation_accuracy >= thresholds["minimum_citation_page_accuracy"]
        and (
            wrong_answer_rate is None
            or wrong_answer_rate <= thresholds["maximum_unanswerable_wrong_rate"]
        )
    )

    if protocol_errors:
        decision = "protocol_invalid"
    elif not (hard_gate_passed and engineering_gate_passed and cost_gate_passed):
        decision = "no_go"
    elif manifest.annotation_status != "frozen" or not human_complete:
        decision = "quality_pending"
    elif quality_gate_passed:
        decision = "go"
    else:
        decision = "no_go"

    return {
        "schema_version": 1,
        "dataset": {
            "id": manifest.dataset_id,
            "version": manifest.version,
            "annotation_status": manifest.annotation_status,
            "split": split,
        },
        "protocol": {
            "paired_case_count": len(selected),
            "expected_case_count": len(selected_cases),
            "errors": protocol_errors,
            "token_measurement": "estimated_not_provider_billed_usage",
        },
        "gates_frozen_before_run": manifest.thresholds.model_dump(),
        "pairs": [_pair_summary(case, pair) for case, pair in selected],
        "variants": {"v1": v1_metrics, "v2": v2_metrics},
        "gates": {
            "hard": {"passed": hard_gate_passed, "counts": safety_counts},
            "engineering": {
                "passed": engineering_gate_passed,
                "missing_fault_checks": missing_checks,
                "fault_checks": [item.model_dump() for item in fault_checks or []],
            },
            "cost": {
                "passed": cost_gate_passed,
                "latency_p95_ratio": latency_ratio,
                "estimated_token_ratio": token_ratio,
            },
            "quality": {
                "passed": quality_gate_passed,
                "expected_claim_coverage": claim_gate,
                "required_paper_coverage": paper_gate,
                "support_precision_non_regression": support_non_regression,
                "human_complete": human_complete,
                "human_net_win": human_net_win,
                "human_quality_gate": human_quality_gate,
            },
        },
        "decision": decision,
        "decision_note": (
            "draft 标注或人工盲评未完成，不允许声称 v2 质量优于 v1"
            if decision == "quality_pending"
            else None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="校验 PaperLeaf Map-Reduce A/B draft 数据集")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--source-cases", required=True, type=Path)
    args = parser.parse_args()
    manifest = MultiAgentManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    validate_source_hashes(
        manifest,
        source_manifest_path=args.source_manifest,
        source_cases_path=args.source_cases,
    )
    cases = read_jsonl(args.cases, MultiAgentCase)
    source_cases = [
        json.loads(line)
        for line in args.source_cases.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    print(json.dumps(validate_dataset(manifest, cases, source_cases), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
