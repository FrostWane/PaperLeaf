from paperleaf_api.evaluation import (
    CitationPrediction,
    EvaluationCase,
    EvaluationPrediction,
    evaluate,
)


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
