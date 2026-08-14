"""独立开发集上的问题—证据可回答性评测。

该模块只允许用开发集选择阈值；不会读取正式隐藏集，也不会生成最终答案。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent.answerability import (
    AnswerabilityDecision,
    AnswerabilityGrader,
    build_configured_answerability_grader,
)
from .agent.tools import LibrarySearchInput, SQLLibrarySearch
from .config import settings
from .evaluation_dataset import (
    FrozenEvaluationCase,
    read_frozen_cases,
    read_manifest,
    validate_dataset,
)
from .evaluation_production import preflight_production_corpus
from .model_runtime import build_model_router
from .rag.retrieval_config import freeze_retrieval_config


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def score_answerability(
    cases: Sequence[FrozenEvaluationCase],
    decisions: Sequence[AnswerabilityDecision],
    *,
    threshold: float,
) -> dict[str, Any]:
    if len(cases) != len(decisions) or not cases:
        raise ValueError("用例与可回答性决策必须一一对应且不能为空")
    tp = tn = fp = fn = not_checked = 0
    for case, decision in zip(cases, decisions, strict=True):
        if decision.answerable is None:
            not_checked += 1
            continue
        predicted = bool(decision.answerable and (decision.confidence or 0.0) >= threshold)
        if case.answerable and predicted:
            tp += 1
        elif case.answerable:
            fn += 1
        elif predicted:
            fp += 1
        else:
            tn += 1
    positive_total = tp + fn
    negative_total = tn + fp
    tpr = tp / positive_total if positive_total else 0.0
    tnr = tn / negative_total if negative_total else 0.0
    return {
        "threshold": threshold,
        "true_positive": tp,
        "true_negative": tn,
        "false_positive": fp,
        "false_negative": fn,
        "not_checked": not_checked,
        "answerable_recall": tpr,
        "unanswerable_false_answer_rate": fp / negative_total if negative_total else 0.0,
        "balanced_accuracy": (tpr + tnr) / 2,
    }


def select_development_threshold(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("阈值候选不能为空")
    fully_measured = [row for row in rows if int(row["not_checked"]) == 0]
    candidates = [row for row in fully_measured if int(row["false_positive"]) == 0]
    if candidates:
        selected = min(
            candidates,
            key=lambda row: (-float(row["answerable_recall"]), float(row["threshold"])),
        )
        reason = "zero_false_answer_then_max_answerable_recall"
    else:
        pool = fully_measured or list(rows)
        selected = min(
            pool,
            key=lambda row: (
                int(row["false_positive"]),
                -float(row["answerable_recall"]),
                float(row["threshold"]),
            ),
        )
        reason = "minimum_false_answer_then_max_answerable_recall"
    return {**selected, "selection_reason": reason}


async def evaluate_answerability_cases(
    cases: Sequence[FrozenEvaluationCase],
    *,
    user_id: str,
    paper_id_map: dict[str, str],
    grader: AnswerabilityGrader,
    retriever: SQLLibrarySearch,
) -> dict[str, Any]:
    frozen_config = freeze_retrieval_config(settings)
    decisions: list[AnswerabilityDecision] = []
    case_results: list[dict[str, Any]] = []
    for case in cases:
        evidence = await retriever(
            LibrarySearchInput(
                user_id=user_id,
                query=case.query,
                paper_ids=[paper_id_map[paper_id] for paper_id in case.paper_ids],
                limit=5,
                retrieval_config=frozen_config,
            )
        )
        result = grader(case.query, evidence)
        decision = await result if inspect.isawaitable(result) else result
        decision = AnswerabilityDecision.model_validate(decision)
        decisions.append(decision)
        case_results.append(
            {
                "case_id": case.id,
                "answerable": case.answerable,
                "decision": decision.model_dump(),
                "retrieved_chunk_ids": [item.chunk_id for item in evidence[:5]],
                "retrieved_pages": [item.physical_page for item in evidence[:5]],
            }
        )
    thresholds = [round(value / 100, 2) for value in range(50, 96, 5)]
    threshold_rows = [
        score_answerability(cases, decisions, threshold=threshold) for threshold in thresholds
    ]
    return {
        "case_results": case_results,
        "threshold_sweep": threshold_rows,
        "selected_threshold": select_development_threshold(threshold_rows),
        "retrieval_config": frozen_config,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = read_manifest(args.manifest)
    cases = read_frozen_cases(args.cases)
    validate_dataset(manifest, cases)
    if any(case.split != "dev" for case in cases):
        raise ValueError("answerability 阈值选择只允许使用 dev 用例")
    required = {paper_id for case in cases for paper_id in case.paper_ids}
    preflight = await preflight_production_corpus(
        manifest,
        user_email=args.user_email,
        required_paper_ids=required,
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": preflight["status"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol": {
            "dataset_id": manifest.dataset_id,
            "manifest_sha256": _sha256(args.manifest),
            "cases_sha256": _sha256(args.cases),
            "case_count": len(cases),
            "purpose": "development_threshold_selection_only",
            "formal_hidden_cases_used": 0,
        },
        "preflight": {
            key: value
            for key, value in preflight.items()
            if key not in {"user_id", "paper_id_map"}
        },
    }
    if preflight["status"] == "ready":
        result.update(
            await evaluate_answerability_cases(
                cases,
                user_id=str(preflight["user_id"]),
                paper_id_map=dict(preflight["paper_id_map"]),
                grader=build_configured_answerability_grader(
                    settings, build_model_router(settings)
                ),
                retriever=SQLLibrarySearch(),
            )
        )
        result["status"] = "completed"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="运行独立 answerability 开发集")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--user-email", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": result["status"], "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
