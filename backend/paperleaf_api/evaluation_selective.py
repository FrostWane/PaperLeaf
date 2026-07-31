"""只在开发集选择拒答策略，并显式报告覆盖率与选择性风险。"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from itertools import product
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .evaluation import EvaluationCase
from .evaluation_dataset import read_frozen_cases, read_manifest, validate_dataset
from .evaluation_offline import OfflineRetrievalIndex, QueryRanking


class SelectiveGatePolicy(BaseModel):
    min_confidence: float = Field(ge=0)
    min_lexical_coverage: float = Field(ge=0)
    require_channel_agreement: bool = False


class SelectiveCalibrationConstraints(BaseModel):
    max_unanswerable_wrong_rate: float = Field(default=0.05, ge=0, le=1)
    max_selective_risk: float = Field(default=0.20, ge=0, le=1)
    min_correctly_cited_answerable_rate: float = Field(default=0.05, ge=0, le=1)


def accepts_ranking(ranking: QueryRanking, policy: SelectiveGatePolicy) -> bool:
    quality = ranking.quality
    if quality is None:
        return False
    agreed = "keyword" in quality.channels and "vector" in quality.channels
    return (
        ranking.confidence >= policy.min_confidence
        and quality.lexical_coverage >= policy.min_lexical_coverage
        and (not policy.require_channel_agreement or agreed)
    )


def _top_hit_is_expected(case: EvaluationCase, ranking: QueryRanking) -> bool:
    if not ranking.hits:
        return False
    top = ranking.hits[0].chunk
    expected = {
        (item.paper_id, item.physical_page)
        for group in case.acceptable_evidence_groups
        for item in group.items
    }
    if not expected:
        expected = {(paper_id, page) for paper_id in case.paper_ids for page in case.expected_pages}
    return (top.paper_id, top.physical_page) in expected


def score_selective_policy(
    cases: list[EvaluationCase],
    rankings: dict[str, QueryRanking],
    policy: SelectiveGatePolicy,
    constraints: SelectiveCalibrationConstraints,
) -> dict[str, Any]:
    answerable = [case for case in cases if case.answerable]
    unanswerable = [case for case in cases if not case.answerable]
    answered_answerable = sum(accepts_ranking(rankings[case.id], policy) for case in answerable)
    correctly_cited = sum(
        accepts_ranking(rankings[case.id], policy)
        and _top_hit_is_expected(case, rankings[case.id])
        for case in answerable
    )
    wrong_unanswerable = sum(
        accepts_ranking(rankings[case.id], policy) for case in unanswerable
    )
    answered = answered_answerable + wrong_unanswerable
    unsafe_answered = answered - correctly_cited
    unanswerable_wrong_rate = wrong_unanswerable / len(unanswerable)
    correctly_cited_rate = correctly_cited / len(answerable)
    selective_risk = unsafe_answered / answered if answered else 0.0
    constraint_checks = {
        "unanswerable_wrong_rate": (
            unanswerable_wrong_rate <= constraints.max_unanswerable_wrong_rate
        ),
        "selective_risk": selective_risk <= constraints.max_selective_risk,
        "correctly_cited_answerable_rate": (
            correctly_cited_rate >= constraints.min_correctly_cited_answerable_rate
        ),
    }
    return {
        "answerable_total": len(answerable),
        "unanswerable_total": len(unanswerable),
        "answered_count": answered,
        "answered_answerable_count": answered_answerable,
        "correctly_cited_answerable_count": correctly_cited,
        "wrong_unanswerable_count": wrong_unanswerable,
        "unsafe_answered_count": unsafe_answered,
        "answerable_response_rate": answered_answerable / len(answerable),
        "correctly_cited_answerable_rate": correctly_cited_rate,
        "unanswerable_wrong_rate": unanswerable_wrong_rate,
        "selective_citation_precision": correctly_cited / answered if answered else None,
        "selective_risk": selective_risk,
        "constraint_checks": constraint_checks,
        "constraint_satisfied": all(constraint_checks.values()),
    }


def _candidate_values(values: set[float]) -> list[float]:
    return sorted({0.0, *(value + 1e-12 for value in values), 1.0 + 1e-12})


def calibrate_selective_gate(
    cases: list[EvaluationCase],
    rankings: dict[str, QueryRanking],
    *,
    constraints: SelectiveCalibrationConstraints | None = None,
) -> dict[str, Any]:
    """网格只读取 dev oracle；不可用策略也会留下诚实的诊断结果。"""

    constraints = constraints or SelectiveCalibrationConstraints()
    dev_cases = [case for case in cases if case.split == "dev"]
    if not dev_cases or not any(case.answerable for case in dev_cases):
        raise ValueError("选择性校准需要包含可回答问题的 dev 集")
    if not any(not case.answerable for case in dev_cases):
        raise ValueError("选择性校准需要包含不可回答问题的 dev 集")
    missing = sorted({case.id for case in dev_cases} - set(rankings))
    if missing:
        raise ValueError(f"缺少 dev ranking：{missing}")

    confidence_values = {rankings[case.id].confidence for case in dev_cases}
    lexical_values = {
        rankings[case.id].quality.lexical_coverage
        for case in dev_cases
        if rankings[case.id].quality is not None
    }
    candidates: list[tuple[SelectiveGatePolicy, dict[str, Any]]] = []
    for min_confidence, min_lexical, require_agreement in product(
        _candidate_values(confidence_values),
        _candidate_values(lexical_values),
        (False, True),
    ):
        policy = SelectiveGatePolicy(
            min_confidence=min_confidence,
            min_lexical_coverage=min_lexical,
            require_channel_agreement=require_agreement,
        )
        metrics = score_selective_policy(dev_cases, rankings, policy, constraints)
        candidates.append((policy, metrics))

    def rank(item: tuple[SelectiveGatePolicy, dict[str, Any]]) -> tuple[object, ...]:
        policy, metrics = item
        total_violation = (
            max(
                0.0,
                float(metrics["unanswerable_wrong_rate"])
                - constraints.max_unanswerable_wrong_rate,
            )
            + max(
                0.0,
                float(metrics["selective_risk"]) - constraints.max_selective_risk,
            )
            + max(
                0.0,
                constraints.min_correctly_cited_answerable_rate
                - float(metrics["correctly_cited_answerable_rate"]),
            )
        )
        return (
            bool(metrics["constraint_satisfied"]),
            -total_violation,
            float(metrics["correctly_cited_answerable_rate"]),
            -float(metrics["selective_risk"]),
            -float(metrics["unanswerable_wrong_rate"]),
            float(metrics["answerable_response_rate"]),
            not policy.require_channel_agreement,
            -policy.min_confidence,
            -policy.min_lexical_coverage,
        )

    selected_policy, selected_metrics = max(candidates, key=rank)
    satisfied = bool(selected_metrics["constraint_satisfied"])
    return {
        "schema_version": 1,
        "fit_split": "dev",
        "objective": "maximise correctly cited answerable coverage under safety constraints",
        "constraints": constraints.model_dump(),
        "candidate_count": len(candidates),
        "constraint_satisfied": satisfied,
        "recommended_policy": selected_policy.model_dump() if satisfied else None,
        "least_violating_policy": selected_policy.model_dump(),
        "least_violating_metrics": selected_metrics,
        "warning": (
            None
            if satisfied
            else "没有候选同时满足风险、不可回答错误率和正确引用覆盖约束"
        ),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_selective_calibration(
    *,
    manifest_path: Path,
    cases_path: Path,
    pdf_dir: Path,
    k: int,
    constraints: SelectiveCalibrationConstraints | None = None,
) -> dict[str, Any]:
    if k <= 0:
        raise ValueError("k 必须为正数")
    manifest = read_manifest(manifest_path)
    frozen_cases = read_frozen_cases(cases_path)
    validation = validate_dataset(manifest, frozen_cases, pdf_dir=pdf_dir)
    cases = [EvaluationCase.model_validate(case.model_dump()) for case in frozen_cases]
    started = time.perf_counter()
    index = OfflineRetrievalIndex.from_pdf_dir(
        manifest_path,
        pdf_dir,
        target_tokens=manifest.chunking.target_tokens,
        overlap_tokens=manifest.chunking.overlap_tokens,
    )
    index_ms = round((time.perf_counter() - started) * 1000)
    rankings = {
        case.id: index.fused(case.query, case.paper_ids, limit=k, page_dedup=True)
        for case in cases
    }
    calibration = calibrate_selective_gate(
        cases,
        rankings,
        constraints=constraints,
    )
    return {
        "schema_version": 1,
        "dataset": validation,
        "protocol": {
            "retrieval_variant": "rrf_page",
            "k": k,
            "fit_split": "dev",
            "hash_dimensions": index.dimensions,
            "chunk_count": len(index.chunks),
            "index_build_ms": index_ms,
            "manifest_sha256": _sha256(manifest_path),
            "cases_sha256": _sha256(cases_path),
            "retrieval_implementation_sha256": _sha256(
                Path(__file__).with_name("evaluation_offline.py")
            ),
            "selective_implementation_sha256": _sha256(Path(__file__)),
        },
        "calibration": calibration,
    }


def render_selective_report(result: dict[str, Any]) -> str:
    calibration = result["calibration"]
    metrics = calibration["least_violating_metrics"]
    recommended = calibration["recommended_policy"]
    status = "满足全部预设约束" if recommended else "没有候选满足全部预设约束"
    policy = recommended or calibration["least_violating_policy"]
    precision = metrics["selective_citation_precision"]
    precision_text = "—" if precision is None else f"{precision * 100:.1f}%"
    return "\n".join(
        (
            "# PaperLeaf 选择性回答校准报告",
            "",
            f"- 数据集：`{result['dataset']['dataset_id']}`（仅 dev）",
            f"- 结论：**{status}**。",
            f"- 搜索候选：{calibration['candidate_count']} 组确定性门禁。",
            f"- 置信度阈值：`{policy['min_confidence']:.6f}`。",
            f"- 词面覆盖阈值：`{policy['min_lexical_coverage']:.6f}`。",
            f"- 要求通道一致：`{str(policy['require_channel_agreement']).lower()}`。",
            "",
            "## 原始结果",
            "",
            f"- 正确引用后作答：{metrics['correctly_cited_answerable_count']}/"
            f"{metrics['answerable_total']}（"
            f"{metrics['correctly_cited_answerable_rate'] * 100:.1f}%）。",
            f"- 可回答题实际作答：{metrics['answered_answerable_count']}/"
            f"{metrics['answerable_total']}（"
            f"{metrics['answerable_response_rate'] * 100:.1f}%）。",
            f"- 不可回答题错误作答：{metrics['wrong_unanswerable_count']}/"
            f"{metrics['unanswerable_total']}（"
            f"{metrics['unanswerable_wrong_rate'] * 100:.1f}%）。",
            f"- 已作答样本中的正确引用精度：{precision_text}。",
            f"- 选择性风险：{metrics['unsafe_answered_count']}/"
            f"{metrics['answered_count']}（{metrics['selective_risk'] * 100:.1f}%）。",
            "",
            "## 解释边界",
            "",
            "- 策略只在开发集选择，不能把本报告当作隐藏集效果。",
            "- 正确引用覆盖、拒答率和风险同时报告，不能用全拒答制造安全性高分。",
            "- 若没有推荐策略，表示现有确定性特征不足，不会上线该候选。",
            "",
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="校准 PaperLeaf 选择性回答门禁")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--pdf-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("-k", type=int, default=5)
    parser.add_argument("--max-unanswerable-wrong-rate", type=float, default=0.05)
    parser.add_argument("--max-selective-risk", type=float, default=0.20)
    parser.add_argument("--min-correctly-cited-answerable-rate", type=float, default=0.05)
    args = parser.parse_args()
    result = run_selective_calibration(
        manifest_path=args.manifest,
        cases_path=args.cases,
        pdf_dir=args.pdf_dir,
        k=args.k,
        constraints=SelectiveCalibrationConstraints(
            max_unanswerable_wrong_rate=args.max_unanswerable_wrong_rate,
            max_selective_risk=args.max_selective_risk,
            min_correctly_cited_answerable_rate=(
                args.min_correctly_cited_answerable_rate
            ),
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.report.write_text(render_selective_report(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "report": str(args.report),
                "constraint_satisfied": result["calibration"][
                    "constraint_satisfied"
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
