from paperleaf_api.evaluation import EvaluationCase
from paperleaf_api.evaluation_offline import QueryRanking, ScoredChunk
from paperleaf_api.evaluation_selective import (
    SelectiveCalibrationConstraints,
    SelectiveGatePolicy,
    calibrate_selective_gate,
    score_selective_policy,
)
from paperleaf_api.rag.chunking import PageChunk
from paperleaf_api.rag.retrieval_quality import EvidenceQuality


def _ranking(
    *, page: int, confidence: float, lexical: float, agreed: bool = True
) -> QueryRanking:
    channels = ("keyword", "vector") if agreed else ("keyword",)
    quality = EvidenceQuality(
        grade="sufficient",
        confidence=confidence,
        reason_code="fixture",
        summary="fixture",
        evidence_count=1,
        page_count=1,
        paper_count=1,
        channels=channels,
        lexical_coverage=lexical,
        vector_score=confidence if agreed else 0,
        retrieval_grade="sufficient",
        answer_support_grade="not_checked",
        answer_support_confidence=None,
    )
    return QueryRanking(
        hits=[
            ScoredChunk(
                PageChunk(
                    id=f"p1:{page}:0",
                    paper_id="p1",
                    physical_page=page,
                    chunk_index=0,
                    text="fixture",
                    token_count=1,
                ),
                confidence,
            )
        ],
        confidence=confidence,
        quality=quality,
    )


def _cases() -> list[EvaluationCase]:
    return [
        EvaluationCase(
            id="answerable",
            query="fact",
            paper_ids=["p1"],
            answerable=True,
            expected_pages=[2],
            category="fact",
            split="dev",
        ),
        EvaluationCase(
            id="unanswerable",
            query="missing",
            paper_ids=["p1"],
            answerable=False,
            category="unanswerable",
            split="dev",
        ),
    ]


def test_selective_policy_counts_wrong_citation_as_unsafe_answer() -> None:
    rankings = {
        "answerable": _ranking(page=9, confidence=0.9, lexical=0.9),
        "unanswerable": _ranking(page=4, confidence=0.2, lexical=0.2),
    }
    metrics = score_selective_policy(
        _cases(),
        rankings,
        SelectiveGatePolicy(min_confidence=0.5, min_lexical_coverage=0.5),
        SelectiveCalibrationConstraints(
            max_unanswerable_wrong_rate=0,
            max_selective_risk=0,
            min_correctly_cited_answerable_rate=0,
        ),
    )

    assert metrics["wrong_unanswerable_count"] == 0
    assert metrics["unsafe_answered_count"] == 1
    assert metrics["selective_risk"] == 1
    assert metrics["constraint_satisfied"] is False


def test_calibration_reports_no_recommendation_when_only_safe_option_over_refuses() -> None:
    rankings = {
        "answerable": _ranking(page=9, confidence=0.9, lexical=0.9),
        "unanswerable": _ranking(page=4, confidence=0.8, lexical=0.8),
    }

    calibration = calibrate_selective_gate(
        _cases(),
        rankings,
        constraints=SelectiveCalibrationConstraints(
            max_unanswerable_wrong_rate=0,
            max_selective_risk=0,
            min_correctly_cited_answerable_rate=0.5,
        ),
    )

    assert calibration["constraint_satisfied"] is False
    assert calibration["recommended_policy"] is None
    assert calibration["warning"]
    assert calibration["least_violating_metrics"]["answered_count"] == 0
    assert calibration["least_violating_metrics"]["correctly_cited_answerable_rate"] == 0


def test_calibration_selects_dev_policy_that_keeps_correct_citation() -> None:
    rankings = {
        "answerable": _ranking(page=2, confidence=0.9, lexical=0.9),
        "unanswerable": _ranking(page=4, confidence=0.2, lexical=0.2),
    }

    calibration = calibrate_selective_gate(
        _cases(),
        rankings,
        constraints=SelectiveCalibrationConstraints(
            max_unanswerable_wrong_rate=0,
            max_selective_risk=0,
            min_correctly_cited_answerable_rate=0.5,
        ),
    )

    assert calibration["constraint_satisfied"] is True
    assert calibration["recommended_policy"] is not None
    assert calibration["least_violating_metrics"]["correctly_cited_answerable_count"] == 1
    assert calibration["least_violating_metrics"]["wrong_unanswerable_count"] == 0
