from paperleaf_api.evaluation import (
    CitationPrediction,
    EvaluationCase,
    EvaluationPrediction,
    RetrievedEvidencePrediction,
    evaluate,
)
from paperleaf_api.evaluation_dataset import ExpectedEvidence


def test_evaluation_reports_raw_counts_and_illegal_citations() -> None:
    cases = [
        EvaluationCase(
            id="answerable",
            query="结论？",
            paper_ids=["p1"],
            answerable=True,
            expected_pages=[3],
            expected_chunk_ids=["c1"],
            expected_answer_keywords=["提升"],
            category="事实",
        ),
        EvaluationCase(
            id="unanswerable",
            query="没有依据的问题",
            paper_ids=["p1"],
            answerable=False,
            category="不可回答",
        ),
    ]
    predictions = [
        EvaluationPrediction(
            case_id="answerable",
            answer="结果有所提升",
            abstained=False,
            retrieved_chunk_ids=["c1"],
            citations=[CitationPrediction(chunk_id="c1", physical_page=3)],
            latency_ms=100,
        ),
        EvaluationPrediction(
            case_id="unanswerable",
            answer="错误作答",
            abstained=False,
            retrieved_chunk_ids=[],
            citations=[CitationPrediction(chunk_id="forged", physical_page=9)],
            latency_ms=200,
        ),
    ]

    metrics = evaluate(cases, predictions, k=5)

    assert metrics["retrieval_recall_at_k"]["numerator"] == 1
    assert metrics["citation_page_accuracy"]["value"] == 1.0
    assert metrics["unanswerable_wrong_answer_rate"]["value"] == 1.0
    assert metrics["illegal_citation_count"] == 1
    selective = metrics["selective_answering"]
    assert selective["answered_count"] == 2
    assert selective["correctly_cited_answered_count"] == 1
    assert selective["unsafe_answered_count"] == 1
    assert selective["selective_citation_precision"]["value"] == 0.5
    assert selective["selective_risk"]["value"] == 0.5
    assert selective["balanced_safety_accuracy"]["value"] == 0.5


def test_selective_metrics_expose_over_refusal_instead_of_hiding_it() -> None:
    cases = [
        EvaluationCase(
            id="answerable",
            query="结论？",
            paper_ids=["p1"],
            answerable=True,
            expected_pages=[3],
            category="fact",
        ),
        EvaluationCase(
            id="unanswerable",
            query="没有依据的问题",
            paper_ids=["p1"],
            answerable=False,
            category="unanswerable",
        ),
    ]
    predictions = [
        EvaluationPrediction(
            case_id="answerable",
            answer="",
            abstained=True,
            latency_ms=1,
        ),
        EvaluationPrediction(
            case_id="unanswerable",
            answer="",
            abstained=True,
            latency_ms=1,
        ),
    ]

    metrics = evaluate(cases, predictions)
    selective = metrics["selective_answering"]

    assert metrics["unanswerable_wrong_answer_rate"]["value"] == 0
    assert selective["answerable_over_refusal_rate"]["value"] == 1
    assert selective["correctly_cited_answerable_rate"]["value"] == 0
    assert selective["unanswerable_abstention_rate"]["value"] == 1
    assert selective["balanced_safety_accuracy"]["value"] == 0.5


def test_evaluation_uses_paper_and_physical_page_for_frozen_evidence() -> None:
    case = EvaluationCase(
        id="cross-paper",
        query="对比",
        paper_ids=["p1", "p2"],
        answerable=True,
        expected_evidence=[
            ExpectedEvidence(paper_id="p1", physical_page=2, anchor="first paper anchor"),
            ExpectedEvidence(paper_id="p2", physical_page=2, anchor="second paper anchor"),
        ],
        category="cross_paper",
        split="test",
    )
    prediction = EvaluationPrediction(
        case_id=case.id,
        answer="回答",
        abstained=False,
        retrieved_evidence=[
            RetrievedEvidencePrediction(chunk_id="p1-c", paper_id="p1", physical_page=2),
            RetrievedEvidencePrediction(chunk_id="p2-wrong", paper_id="p2", physical_page=3),
        ],
        citations=[CitationPrediction(chunk_id="p1-c", paper_id="p1", physical_page=2)],
        latency_ms=1,
    )

    metrics = evaluate([case], [prediction], k=5)

    assert metrics["retrieval_recall_at_k"]["numerator"] == 1
    assert metrics["retrieval_recall_at_k"]["denominator"] == 2
    assert metrics["retrieval_mrr_at_k"]["value"] == 1.0
    assert metrics["citation_page_accuracy"]["value"] == 1.0


def test_evaluation_accepts_alternative_evidence_groups() -> None:
    cases = [
        EvaluationCase(
            id="alternative-pages",
            query="Which setup is used?",
            paper_ids=["paper"],
            answerable=True,
            acceptable_evidence_groups=[
                {
                    "items": [
                        {
                            "paper_id": "paper",
                            "physical_page": 2,
                            "anchor": "first acceptable evidence",
                        }
                    ]
                },
                {
                    "items": [
                        {
                            "paper_id": "paper",
                            "physical_page": 7,
                            "anchor": "second acceptable evidence",
                        }
                    ]
                },
            ],
            acceptable_answer_keyword_groups=[["alpha"], ["beta"]],
            category="setup",
        )
    ]
    predictions = [
        EvaluationPrediction(
            case_id="alternative-pages",
            answer="The answer uses beta.",
            abstained=False,
            retrieved_evidence=[
                RetrievedEvidencePrediction(
                    chunk_id="page-7", paper_id="paper", physical_page=7
                )
            ],
            citations=[
                CitationPrediction(
                    chunk_id="page-7", paper_id="paper", physical_page=7
                )
            ],
            latency_ms=1,
        )
    ]

    metrics = evaluate(cases, predictions, k=5)

    assert metrics["evidence_group_recall_at_k"]["value"] == 1
    assert metrics["evidence_page_recall_at_k"]["value"] == 1
    assert metrics["answer_keyword_accuracy"]["value"] == 1
