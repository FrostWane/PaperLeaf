from pathlib import Path

import pytest

from paperleaf_api.evaluation_formal_protocol import (
    FORMAL_VARIANTS,
    FormalEvaluationLock,
    verify_public_formal_inputs,
)
from paperleaf_api.evaluation_holdout import read_questions


def test_formal_lock_requires_exact_preregistered_variants() -> None:
    payload = {
        "dataset_id": "formal",
        "manifest_sha256": "a" * 64,
        "questions_sha256": "b" * 64,
        "oracle_sha256": "c" * 64,
        "excluded_datasets": [
            {
                "dataset_id": "dev",
                "manifest_sha256": "d" * 64,
                "paper_ids_sha256": "e" * 64,
                "paper_count": 20,
            }
        ],
        "candidate_variants": list(FORMAL_VARIANTS),
        "protocol": {"k": 5, "expected_case_count": 100, "bootstrap_samples": 10_000},
        "locked_at": "2026-08-14T00:00:00Z",
    }
    assert FormalEvaluationLock.model_validate(payload).status == "frozen_before_first_run"
    payload["candidate_variants"] = payload["candidate_variants"][:-1]
    with pytest.raises(ValueError, match="消融方案"):
        FormalEvaluationLock.model_validate(payload)


def test_formal_lock_refuses_weak_bootstrap_protocol(tmp_path: Path) -> None:
    del tmp_path
    payload = {
        "dataset_id": "formal",
        "manifest_sha256": "a" * 64,
        "questions_sha256": "b" * 64,
        "oracle_sha256": "c" * 64,
        "excluded_datasets": [
            {
                "dataset_id": "dev",
                "manifest_sha256": "d" * 64,
                "paper_ids_sha256": "e" * 64,
                "paper_count": 20,
            }
        ],
        "candidate_variants": list(FORMAL_VARIANTS),
        "protocol": {"k": 5, "expected_case_count": 100, "bootstrap_samples": 9999},
        "locked_at": "2026-08-14T00:00:00Z",
    }
    with pytest.raises(ValueError, match="Bootstrap"):
        FormalEvaluationLock.model_validate(payload)


def test_repository_formal_hidden_public_inputs_are_frozen() -> None:
    backend = Path(__file__).resolve().parents[1]
    dataset = backend / "evaluation" / "datasets" / "paperleaf-formal-hidden-v1"
    lock = FormalEvaluationLock.model_validate_json(
        (dataset / "lock.json").read_text(encoding="utf-8")
    )
    exclusions = [
        backend / "evaluation" / "datasets" / name / "manifest.json"
        for name in (
            "paperleaf-rag-v1",
            "paperleaf-qasper-calibration-v1",
            "paperleaf-qasper-holdout-v1",
            "paperleaf-qasper-selective-holdout-v2",
        )
    ]
    result = verify_public_formal_inputs(
        lock,
        manifest_path=dataset / "manifest.json",
        questions_path=dataset / "questions.jsonl",
        exclusion_manifest_paths=exclusions,
    )
    assert result == {
        "status": "verified_without_private_oracle",
        "dataset_id": "paperleaf-formal-hidden-v1",
        "paper_count": 70,
        "case_count": 100,
        "oracle_sha256": lock.oracle_sha256,
    }


def test_formal_hidden_contains_frozen_chinese_and_english_buckets() -> None:
    backend = Path(__file__).resolve().parents[1]
    dataset = backend / "evaluation" / "datasets" / "paperleaf-formal-hidden-v1"
    questions = read_questions(dataset / "questions.jsonl")
    chinese = sum(
        any("\u4e00" <= char <= "\u9fff" for char in item.query) for item in questions
    )
    assert chinese == 50
    assert len(questions) - chinese == 50
