import json
from pathlib import Path

import pytest

from paperleaf_api.evaluation_holdout import (
    HoldoutOracleRecord,
    HoldoutQuestion,
    create_lock,
    evaluate_locked_holdout,
    merge_questions_and_oracle,
    verify_lock,
    verify_public_holdout_inputs,
    write_first_reveal_receipt,
)


def test_holdout_merge_keeps_public_questions_separate_from_oracle() -> None:
    questions = [
        HoldoutQuestion(
            id="q1",
            query="What is the method?",
            paper_ids=["arxiv:1706.03762v7"],
            source_dataset="qasper",
            source_question_id="source-q1",
        )
    ]
    oracle = [
        HoldoutOracleRecord(
            id="q1",
            answerable=True,
            acceptable_evidence_groups=[
                {
                    "items": [
                        {
                            "paper_id": "arxiv:1706.03762v7",
                            "physical_page": 3,
                            "anchor": "scaled dot product attention",
                        }
                    ]
                }
            ],
            acceptable_answer_keyword_groups=[["attention"]],
            category="extractive",
        )
    ]

    merged = merge_questions_and_oracle(questions, oracle)

    assert merged[0].split == "holdout"
    assert merged[0].answerable is True
    assert merged[0].acceptable_evidence_groups[0].items[0].physical_page == 3


def test_holdout_merge_rejects_oracle_id_drift() -> None:
    question = HoldoutQuestion(
        id="q1",
        query="Question",
        paper_ids=["arxiv:1706.03762v7"],
        source_dataset="qasper",
        source_question_id="source-q1",
    )
    oracle = HoldoutOracleRecord(id="q2", answerable=False, category="unanswerable")

    with pytest.raises(ValueError, match="ID 不匹配"):
        merge_questions_and_oracle([question], [oracle])


def test_holdout_lock_detects_any_input_change(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    questions = tmp_path / "questions.jsonl"
    oracle = tmp_path / "oracle.jsonl"
    manifest.write_text("{}\n", encoding="utf-8")
    questions.write_text("{}\n", encoding="utf-8")
    oracle.write_text("{}\n", encoding="utf-8")
    lock = create_lock(
        dataset_id="holdout",
        manifest_path=manifest,
        questions_path=questions,
        oracle_path=oracle,
        candidate_variants=["rrf_page", "rrf_page_multigranular"],
        protocol={"k": 5},
        locked_at="2026-07-31T00:00:00+00:00",
    )

    verify_lock(
        lock,
        manifest_path=manifest,
        questions_path=questions,
        oracle_path=oracle,
    )
    oracle.write_text('{"changed":true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="oracle_sha256"):
        verify_lock(
            lock,
            manifest_path=manifest,
            questions_path=questions,
            oracle_path=oracle,
        )


def test_holdout_lock_is_portable_across_line_endings(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    questions = tmp_path / "questions.jsonl"
    oracle = tmp_path / "oracle.jsonl"
    for path in (manifest, questions, oracle):
        path.write_bytes(b'{"first":true}\n{"second":true}\n')

    lock = create_lock(
        dataset_id="holdout",
        manifest_path=manifest,
        questions_path=questions,
        oracle_path=oracle,
        candidate_variants=["rrf_page"],
        protocol={"k": 5},
        locked_at="2026-07-31T00:00:00+00:00",
    )

    for path in (manifest, questions, oracle):
        path.write_bytes(b'{"first":true}\r\n{"second":true}\r\n')

    verify_lock(
        lock,
        manifest_path=manifest,
        questions_path=questions,
        oracle_path=oracle,
    )


def test_first_reveal_receipt_is_single_use(tmp_path: Path) -> None:
    lock = tmp_path / "lock.json"
    result = tmp_path / "result.json"
    receipt = tmp_path / "receipt.json"
    lock.write_text(json.dumps({"lock": True}), encoding="utf-8")
    result.write_text(json.dumps({"metric": 1}), encoding="utf-8")

    created = write_first_reveal_receipt(
        receipt_path=receipt,
        lock_path=lock,
        result_path=result,
        dataset_id="holdout",
    )

    assert created.evaluation_status == "blind_holdout_first_run"
    with pytest.raises(FileExistsError, match="已经揭盲"):
        write_first_reveal_receipt(
            receipt_path=receipt,
            lock_path=lock,
            result_path=result,
            dataset_id="holdout",
        )


def test_blind_run_refuses_existing_result_before_reading_oracle(tmp_path: Path) -> None:
    result = tmp_path / "result.json"
    result.write_text("{}\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="拒绝重复运行"):
        evaluate_locked_holdout(
            lock_path=tmp_path / "missing-lock.json",
            manifest_path=tmp_path / "missing-manifest.json",
            questions_path=tmp_path / "missing-questions.jsonl",
            oracle_path=tmp_path / "missing-oracle.jsonl",
            pdf_dir=tmp_path / "pdfs",
            result_path=result,
            receipt_path=tmp_path / "receipt.json",
            mode="blind-first-run",
        )


def test_diagnostic_run_cannot_overwrite_first_result(tmp_path: Path) -> None:
    result = tmp_path / "first-result.json"
    receipt = tmp_path / "receipt.json"
    result.write_text("{}\n", encoding="utf-8")
    receipt.write_text("{}\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="不能覆盖首次结果"):
        evaluate_locked_holdout(
            lock_path=tmp_path / "missing-lock.json",
            manifest_path=tmp_path / "missing-manifest.json",
            questions_path=tmp_path / "missing-questions.jsonl",
            oracle_path=tmp_path / "missing-oracle.jsonl",
            pdf_dir=tmp_path / "pdfs",
            result_path=result,
            receipt_path=receipt,
            mode="diagnostic-after-reveal",
        )


def test_public_holdout_verification_needs_no_oracle(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    questions = tmp_path / "questions.jsonl"
    oracle = tmp_path / "oracle.jsonl"
    lock_path = tmp_path / "lock.json"
    manifest.write_text(
        json.dumps(
            {
                "dataset_id": "holdout",
                "version": "1",
                "created_at": "2026-07-31",
                "annotation_license": "CC-BY-4.0",
                "paper_count": 1,
                "case_count": 1,
                "answerable_count": 1,
                "unanswerable_count": 0,
                "category_counts": {"extractive": 1},
                "papers": [
                    {
                        "id": "arxiv:1234.56789v1",
                        "title": "Fixture",
                        "arxiv_id": "1234.56789v1",
                        "source_url": "https://arxiv.org/abs/1234.56789v1",
                        "pdf_url": "https://arxiv.org/pdf/1234.56789v1",
                        "filename": "1234.56789v1.pdf",
                        "sha256": "a" * 64,
                        "page_count": 1,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    questions.write_text(
        HoldoutQuestion(
            id="q1",
            query="Question",
            paper_ids=["arxiv:1234.56789v1"],
            source_dataset="qasper:test",
            source_question_id="source-q1",
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )
    oracle.write_text("private\n", encoding="utf-8")
    lock = create_lock(
        dataset_id="holdout",
        manifest_path=manifest,
        questions_path=questions,
        oracle_path=oracle,
        candidate_variants=["rrf_page"],
        protocol={"k": 5},
        locked_at="2026-07-31T00:00:00+00:00",
    )
    lock_path.write_text(lock.model_dump_json(), encoding="utf-8")

    result = verify_public_holdout_inputs(
        lock_path=lock_path,
        manifest_path=manifest,
        questions_path=questions,
    )

    assert result["question_count"] == 1
    assert result["oracle_sha256"] == lock.oracle_sha256
