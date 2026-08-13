from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paperleaf_api.evaluation_dataset import (
    EvaluationDatasetManifest,
    EvaluationPaper,
    ExpectedEvidence,
    ExpectedEvidenceGroup,
)
from paperleaf_api.evaluation_formal_dataset import build_formal_hidden_dataset
from paperleaf_api.evaluation_holdout import (
    HoldoutOracleRecord,
    HoldoutQuestion,
    read_oracle,
    read_questions,
)


def _paper(index: int) -> EvaluationPaper:
    arxiv_id = f"24{index // 100:02d}.{index % 10000:04d}v1"
    return EvaluationPaper(
        id=f"arxiv:{arxiv_id}",
        title=f"Paper {index}",
        arxiv_id=arxiv_id,
        source_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        filename=f"{arxiv_id}.pdf",
        sha256=hashlib.sha256(arxiv_id.encode()).hexdigest(),
        page_count=10,
    )


def _write_inputs(
    root: Path, *, answerable: bool, count: int, start: int = 0
) -> tuple[Path, Path, Path]:
    papers = [_paper(start + index) for index in range(count)]
    questions: list[HoldoutQuestion] = []
    oracle: list[HoldoutOracleRecord] = []
    for index, paper in enumerate(papers):
        case_id = f"source-{start + index}"
        questions.append(
            HoldoutQuestion(
                id=case_id,
                query=f"Question {start + index}?",
                paper_ids=[paper.id],
                source_dataset="qasper:test",
                source_question_id=case_id,
            )
        )
        oracle.append(
            HoldoutOracleRecord(
                id=case_id,
                answerable=answerable,
                acceptable_evidence_groups=(
                    [
                        ExpectedEvidenceGroup(
                            items=[
                                ExpectedEvidence(
                                    paper_id=paper.id,
                                    physical_page=1 + index % 5,
                                    anchor=f"A sufficiently long evidence anchor {start + index}",
                                )
                            ]
                        )
                    ]
                    if answerable
                    else []
                ),
                category=("multi_page" if answerable and index < 10 else "extractive"),
            )
        )
    manifest = EvaluationDatasetManifest(
        dataset_id=f"candidate-{'answerable' if answerable else 'unanswerable'}",
        version="2026-08-13",
        created_at="2026-08-13",
        annotation_license="CC-BY-4.0",
        paper_count=count,
        case_count=count,
        answerable_count=count if answerable else 0,
        unanswerable_count=0 if answerable else count,
        category_counts={
            ("extractive" if answerable else "unanswerable"): count - (10 if answerable else 0),
            **({"multi_page": 10} if answerable else {}),
        },
        papers=papers,
    )
    prefix = root / ("answerable" if answerable else "unanswerable")
    prefix.mkdir()
    manifest_path = prefix / "manifest.json"
    questions_path = prefix / "questions.jsonl"
    oracle_path = prefix / "oracle.jsonl"
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    questions_path.write_text(
        "\n".join(item.model_dump_json(exclude_defaults=True) for item in questions) + "\n",
        encoding="utf-8",
    )
    oracle_path.write_text(
        "\n".join(item.model_dump_json(exclude_defaults=True) for item in oracle) + "\n",
        encoding="utf-8",
    )
    return manifest_path, questions_path, oracle_path


def test_build_formal_hidden_dataset_has_frozen_quotas(tmp_path: Path) -> None:
    answerable = _write_inputs(tmp_path, answerable=True, count=60)
    unanswerable = _write_inputs(tmp_path, answerable=False, count=10, start=100)
    output = tmp_path / "output"
    oracle_output = tmp_path / "private" / "oracle.jsonl"

    receipt = build_formal_hidden_dataset(
        answerable_manifest_path=answerable[0],
        answerable_questions_path=answerable[1],
        answerable_oracle_path=answerable[2],
        unanswerable_manifest_path=unanswerable[0],
        unanswerable_questions_path=unanswerable[1],
        unanswerable_oracle_path=unanswerable[2],
        output_dir=output,
        oracle_output=oracle_output,
        dataset_id="paperleaf-formal-hidden-v1",
        created_at="2026-08-13",
        selection_seed="frozen-seed",
    )

    questions = read_questions(output / "questions.jsonl")
    oracle = read_oracle(oracle_output)
    assert receipt["case_count"] == 100
    assert receipt["category_counts"] == {
        "cross_paper": 30,
        "multi_evidence": 10,
        "single_paper": 50,
        "unanswerable": 10,
    }
    assert len(questions) == len(oracle) == 100
    cross = [item for item in questions if item.id.startswith("formal-cross-")]
    assert len(cross) == 30
    assert all(len(item.paper_ids) == 3 for item in cross)
    oracle_by_id = {item.id: item for item in oracle}
    assert all(
        len(oracle_by_id[item.id].acceptable_evidence_groups[0].items) == 3
        for item in cross
    )


def test_build_formal_hidden_dataset_refuses_smaller_denominator(tmp_path: Path) -> None:
    answerable = _write_inputs(tmp_path, answerable=True, count=59)
    unanswerable = _write_inputs(tmp_path, answerable=False, count=10, start=100)
    with pytest.raises(RuntimeError, match="配额不足"):
        build_formal_hidden_dataset(
            answerable_manifest_path=answerable[0],
            answerable_questions_path=answerable[1],
            answerable_oracle_path=answerable[2],
            unanswerable_manifest_path=unanswerable[0],
            unanswerable_questions_path=unanswerable[1],
            unanswerable_oracle_path=unanswerable[2],
            output_dir=tmp_path / "out",
            oracle_output=tmp_path / "oracle.jsonl",
            dataset_id="paperleaf-formal-hidden-v1",
            created_at="2026-08-13",
            selection_seed="frozen-seed",
        )
