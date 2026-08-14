"""对已落盘的 PaperLeaf 正式评测证据做确定性完整性审计。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .evaluation_formal_protocol import matches_locked_text_sha

VARIANTS = (
    "production_baseline",
    "plain_embedding_control",
    "contextual_embedding",
    "per_paper_retrieval",
    "weak_query_rewrite",
    "multigranular_page_reranker",
    "final_combined",
)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha(path: Path) -> str:
    """返回仓库文本对象的规范 LF SHA，避免 checkout 换行改变证据地址。"""

    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def audit_formal_evidence(root: Path) -> dict[str, Any]:
    evaluation = root / "backend" / "evaluation"
    dataset = evaluation / "datasets" / "paperleaf-formal-hidden-v1"
    hidden = evaluation / "results" / "paperleaf-formal-hidden-v1-first-run"
    diagnostic = evaluation / "results" / "paperleaf-rag-production-diagnostic-20260814"
    multi = evaluation / "results" / "paperleaf-multi-agent-three-way-20260814"
    lock = _load(dataset / "lock.json")
    manifest = _load(dataset / "manifest.json")
    questions = _jsonl(dataset / "questions.jsonl")
    oracle_path = hidden / "ground_truth_oracle.jsonl"
    oracle = _jsonl(oracle_path)

    assert len(manifest["papers"]) == 70
    assert len(questions) == len(oracle) == 100
    assert matches_locked_text_sha(oracle_path, lock["oracle_sha256"])
    assert Counter(item["category"] for item in oracle) == {
        "single_paper": 50,
        "cross_paper": 30,
        "multi_evidence": 10,
        "unanswerable": 10,
    }
    assert lock["protocol"]["legacy_minilm_reranker"] == "disabled"

    layer_summary: dict[str, Any] = {}
    for name, directory, expected_cases, mode in (
        ("diagnostic", diagnostic, 90, "diagnostic_not_blind"),
        ("hidden", hidden, 100, "hidden_first_formal_batch"),
    ):
        snapshots: set[str] = set()
        fingerprints: set[str] = set()
        for variant in VARIANTS:
            variant_dir = directory / variant
            run_manifest = _load(variant_dir / "run_manifest.json")
            assert run_manifest["dataset"]["case_count"] == expected_cases
            assert run_manifest["protocol"]["evaluation_status"] == mode
            assert run_manifest["preflight"]["status"] == "ready"
            assert run_manifest["corpus"]["chunking_strategies"] == ["structure_aware_v2"]
            assert run_manifest["configuration"]["legacy_minilm_enabled"] is False
            assert len(_jsonl(variant_dir / "per_query_results.jsonl")) == expected_cases
            snapshots.add(run_manifest["corpus"]["chunk_snapshot_sha256"])
            fingerprints.update(run_manifest["corpus"]["embedding_fingerprints"])
        assert len(snapshots) == 1
        assert fingerprints
        aggregate_metrics = _load(directory / "metrics.json")
        for variant in VARIANTS:
            language = aggregate_metrics["variants"][variant]["by_query_language"]
            assert language["cjk_query"]["case_count"] + language["latin_query"][
                "case_count"
            ] == expected_cases
        assert len(_jsonl(directory / "per_query_results.jsonl")) == expected_cases * 7
        layer_summary[name] = {
            "variant_count": 7,
            "case_count_per_variant": expected_cases,
            "chunk_snapshot_sha256": snapshots.pop(),
            "embedding_fingerprints": sorted(fingerprints),
        }

    answers = hidden / "end_to_end_answers"
    answer_rows = _jsonl(answers / "per_query_answers.jsonl")
    answer_metrics = _load(answers / "metrics.json")
    assert len(answer_rows) == 100
    assert answer_metrics["case_completion_rate"] == {
        "numerator": 100,
        "denominator": 100,
        "value": 1.0,
    }
    assert answer_metrics["human_review_status"] == "human_review_pending"
    assert len(_jsonl(answers / "human_blind_review.jsonl")) == 30

    test_capture = _load(multi / "test-capture.json")
    dev_capture = _load(multi / "dev-capture.json")
    multi_metrics = _load(multi / "metrics.json")
    multi_manifest = _load(multi / "run_manifest.json")
    worker_recovery = _load(multi / "worker-recovery.json")
    assert len(test_capture["pairs"]) == 28
    assert len(dev_capture["pairs"]) == 2
    assert test_capture["executed_run_count"] == 84
    assert dev_capture["executed_run_count"] == 6
    for version in ("v1", "v2", "v3"):
        assert multi_metrics["variants"][version]["executed_runs"] == 30
    assert multi_metrics["human_blind_review"]["completed_reviews"] == 0
    assert multi_metrics["decision"] == "quality_pending"
    assert multi_manifest["dataset"]["executed_task_count"] == 30
    assert multi_manifest["dataset"]["executed_run_count"] == 90
    assert worker_recovery["status"] == "passed"
    assert worker_recovery["job"]["attempts"] == 2
    assert all(worker_recovery["checks"].values())
    assert worker_recovery["claim_token_policy"]["tokens_exported"] is False

    evidence_files = [
        hidden / "run_manifest.json",
        hidden / "metrics.json",
        hidden / "per_query_results.jsonl",
        oracle_path,
        answers / "run_manifest.json",
        answers / "metrics.json",
        answers / "per_query_answers.jsonl",
        answers / "human_blind_review.jsonl",
        diagnostic / "run_manifest.json",
        diagnostic / "metrics.json",
        diagnostic / "per_query_results.jsonl",
        multi / "test-capture.json",
        multi / "dev-capture.json",
        multi / "metrics.json",
        multi / "run_manifest.json",
        multi / "test-blind-review.jsonl",
        multi / "dev-blind-review.jsonl",
        multi / "worker-recovery-capture.json",
        multi / "worker-recovery.json",
    ]
    return {
        "schema_version": 1,
        "status": "automatic_evidence_complete_human_review_pending",
        "dataset": {
            "paper_count": 70,
            "case_count": 100,
            "oracle_frozen_sha256": lock["oracle_sha256"],
            "oracle_repository_sha256": _sha(oracle_path),
            "category_counts": dict(Counter(item["category"] for item in oracle)),
        },
        "retrieval_layers": layer_summary,
        "end_to_end": {
            "executed_cases": 100,
            "human_review_status": "pending",
        },
        "multi_agent": {
            "task_count": 30,
            "run_count": 90,
            "human_review_status": "pending",
            "worker_recovery_status": "passed",
        },
        "artifacts": {
            str(path.relative_to(root)).replace("\\", "/"): _sha(path)
            for path in evidence_files
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="审计 PaperLeaf 正式评测证据")
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[2])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_formal_evidence(args.root.resolve())
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
