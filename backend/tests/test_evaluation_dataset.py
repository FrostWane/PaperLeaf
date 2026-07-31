import pytest

from paperleaf_api.evaluation_dataset import (
    ChunkingSpec,
    DatasetValidationError,
    EvaluationDatasetManifest,
    EvaluationPaper,
    ExpectedEvidence,
    FrozenEvaluationCase,
    validate_dataset,
)


def paper() -> EvaluationPaper:
    return EvaluationPaper(
        id="arxiv:1706.03762v7",
        title="Attention Is All You Need",
        arxiv_id="1706.03762v7",
        source_url="https://arxiv.org/abs/1706.03762v7",
        pdf_url="https://arxiv.org/pdf/1706.03762v7",
        filename="1706.03762v7.pdf",
        sha256="a" * 64,
        page_count=15,
    )


def manifest() -> EvaluationDatasetManifest:
    return EvaluationDatasetManifest(
        dataset_id="fixture",
        version="1",
        created_at="2026-07-29",
        paper_count=1,
        case_count=2,
        answerable_count=1,
        unanswerable_count=1,
        category_counts={"method": 1, "unanswerable": 1},
        chunking=ChunkingSpec(target_tokens=20, overlap_tokens=5),
        papers=[paper()],
    )


def test_dataset_validation_reports_exact_counts() -> None:
    cases = [
        FrozenEvaluationCase(
            id="q1",
            query="核心机制是什么？",
            paper_ids=[paper().id],
            answerable=True,
            expected_evidence=[
                ExpectedEvidence(
                    paper_id=paper().id,
                    physical_page=3,
                    anchor="attention mechanism evidence",
                )
            ],
            expected_answer_keywords=["attention"],
            category="method",
            split="test",
        ),
        FrozenEvaluationCase(
            id="q2",
            query="论文是否讨论火星土壤？",
            paper_ids=[paper().id],
            answerable=False,
            category="unanswerable",
            split="dev",
        ),
    ]

    report = validate_dataset(manifest(), cases)

    assert report["paper_count"] == 1
    assert report["split_counts"] == {"dev": 1, "test": 1}
    assert report["evidence_anchors_verified"] == 0


def test_dataset_validation_rejects_unknown_paper_and_count_drift() -> None:
    cases = [
        FrozenEvaluationCase(
            id="q1",
            query="问题",
            paper_ids=["arxiv:9999.99999v1"],
            answerable=False,
            category="unanswerable",
            split="test",
        )
    ]

    with pytest.raises(DatasetValidationError) as error:
        validate_dataset(manifest(), cases)

    assert "case_count" in str(error.value)
    assert "未知论文" in str(error.value)


def test_manifest_path_is_not_required_for_in_memory_validation() -> None:
    assert (
        validate_dataset(
            manifest(),
            [
                FrozenEvaluationCase(
                    id="q1",
                    query="核心机制是什么？",
                    paper_ids=[paper().id],
                    answerable=True,
                    expected_evidence=[
                        ExpectedEvidence(
                            paper_id=paper().id,
                            physical_page=3,
                            anchor="attention mechanism evidence",
                        )
                    ],
                    category="method",
                    split="test",
                ),
                FrozenEvaluationCase(
                    id="q2",
                    query="不存在的问题",
                    paper_ids=[paper().id],
                    answerable=False,
                    category="unanswerable",
                    split="dev",
                ),
            ],
            pdf_dir=None,
        )["pdf_files_verified"]
        == 0
    )


def test_anchor_normalization_tolerates_pdf_punctuation_and_line_breaks() -> None:
    from paperleaf_api.evaluation_dataset import _normalized, _normalized_ascii

    assert _normalized("cross-lingual pre-\ntraining (NMT)") == _normalized(
        "cross lingual pretraining NMT"
    )
    assert _normalized_ascii("reward r ˆy") == _normalized_ascii("reward r y")
