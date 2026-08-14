"""在已揭盲的 10 个不可回答题上做冻结后端到端诊断回归。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evaluation_dataset import FrozenEvaluationCase, read_manifest
from .evaluation_formal_answers import (
    _run_case,
    aggregate_answer_metrics,
    build_evaluation_repository,
)
from .evaluation_production import preflight_production_corpus


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_revealed_unanswerable(path: Path) -> list[FrozenEvaluationCase]:
    cases: list[FrozenEvaluationCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if bool(row.get("answerable", True)):
            continue
        cases.append(
            FrozenEvaluationCase(
                id=f"revealed-diagnostic-{row['case_id']}",
                query=str(row["query"]),
                paper_ids=[str(item) for item in row["scope_paper_ids"]],
                answerable=False,
                category="unanswerable",
                split="dev",
            )
        )
    if len(cases) != 10:
        raise ValueError(f"已揭盲诊断必须恰好包含 10 个不可回答题，实际 {len(cases)}")
    return cases


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.output_dir.exists():
        raise FileExistsError("诊断结果目录已存在，禁止覆盖证据")
    args.output_dir.mkdir(parents=True)
    cases = _read_revealed_unanswerable(args.source_answers)
    required = {paper_id for case in cases for paper_id in case.paper_ids}
    preflight = await preflight_production_corpus(
        read_manifest(args.manifest),
        user_email=args.user_email,
        required_paper_ids=required,
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "not_executed",
        "evaluation_role": "diagnostic_not_blind",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": args.git_sha,
        "source_answers_sha256": _sha256(args.source_answers),
        "answerability_dev_result_sha256": _sha256(args.answerability_dev_result),
        "case_count": 10,
        "threshold_was_frozen_before_run": True,
        "threshold_tuning_after_run_allowed": False,
        "preflight": {
            key: value
            for key, value in preflight.items()
            if key not in {"user_id", "paper_id_map"}
        },
    }
    if preflight["status"] != "ready":
        (args.output_dir / "run_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return manifest
    repository = build_evaluation_repository()
    rows = []
    for case in cases:
        rows.append(
            await _run_case(
                repository=repository,
                case=case,
                owner_id=str(preflight["user_id"]),
                paper_id_map=dict(preflight["paper_id_map"]),
                timeout_seconds=args.timeout_seconds,
                title_prefix="[诊断] 不可回答门禁",
                idempotency_prefix="unanswerable-diagnostic",
            )
        )
    metrics = aggregate_answer_metrics(rows)
    wrong = int(metrics["unanswerable_wrong_answer_rate"]["numerator"])
    status = "completed" if wrong == 0 else "failed_wrong_answers"
    per_query = args.output_dir / "per_query_results.jsonl"
    per_query.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    metrics_path = args.output_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest.update(
        {
            "status": status,
            "artifacts": {
                "per_query_results.jsonl": _sha256(per_query),
                "metrics.json": _sha256(metrics_path),
            },
        }
    )
    (args.output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="运行已揭盲不可回答题诊断回归")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-answers", required=True, type=Path)
    parser.add_argument("--answerability-dev-result", required=True, type=Path)
    parser.add_argument("--user-email", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps({"status": result["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
