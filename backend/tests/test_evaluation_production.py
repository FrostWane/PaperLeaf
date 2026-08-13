import asyncio

from paperleaf_api.evaluation_dataset import FrozenEvaluationCase
from paperleaf_api.evaluation_production import evaluate_production_cases
from paperleaf_api.rag.citations import Evidence


def _case(case_id: str, *, papers: list[str], pages: list[tuple[str, int]], category="method"):
    return FrozenEvaluationCase.model_validate(
        {
            "id": case_id,
            "query": "Compare BERT and GPT-3 methods" if len(papers) > 1 else "What is BERT?",
            "paper_ids": papers,
            "answerable": True,
            "expected_evidence": [
                {"paper_id": paper, "physical_page": page, "anchor": "long enough anchor"}
                for paper, page in pages
            ],
            "category": category,
            "split": "test",
        }
    )


def test_production_evaluator_uses_frozen_scope_and_reports_cross_paper_metrics() -> None:
    requests = []

    async def retriever(request):
        requests.append(request)
        return [
            Evidence(
                "local-1:p1:c0",
                "local-1",
                "BERT",
                1,
                "BERT evidence",
                0.9,
                retrieval_channels=("keyword", "vector"),
                retrieval_query="BERT method",
                retrieval_processors=("per_paper_balance",),
                query_rewrite_reasons=("broad_or_comparison_intent",),
            ),
            Evidence("local-2:p2:c0", "local-2", "GPT-3", 2, "GPT evidence", 0.8),
        ]

    result = asyncio.run(
        evaluate_production_cases(
            [_case("cross", papers=["p1", "p2"], pages=[("p1", 1), ("p2", 2)])],
            user_id="u1",
            paper_id_map={"p1": "local-1", "p2": "local-2"},
            retriever=retriever,
            k=5,
            retrieval_mode="per_paper_specific",
        )
    )

    assert requests[0].ensure_paper_coverage is True
    assert requests[0].per_paper_query_mode == "paper_specific"
    assert requests[0].paper_ids == ["local-1", "local-2"]
    assert result["retrieval_recall_at_k"]["value"] == 1.0
    assert result["evidence_group_recall_at_k"]["value"] == 1.0
    assert (
        result["by_retrieval_dimension"]["cross_paper"]["evidence_group_recall_at_k"]["value"]
        == 1.0
    )
    assert result["invalid_retrieval_count"] == 0
    assert result["answer_and_citation_metrics"] == {
        "status": "not_measured",
        "reason": "retrieval_only_protocol",
    }
    assert "selective_answering" not in result
    assert result["retrieval_processors"] == {"per_paper_balance": 1}
    assert result["query_rewrite_reasons"] == {"broad_or_comparison_intent": 1}
    assert result["case_results"][0]["retrieved"][0] == {
        "paper_id": "p1",
        "physical_page": 1,
        "chunk_id": "local-1:p1:c0",
        "score": 0.9,
        "channels": ["keyword", "vector"],
        "matched_query": "BERT method",
    }
    assert result["by_retrieval_dimension"]["long_evidence"] == {
        "status": "not_measured",
        "case_count": 0,
    }


def test_production_evaluator_drops_out_of_scope_retrieval_and_counts_it() -> None:
    async def retriever(_request):
        return [Evidence("other:p1:c0", "other", "Other", 1, "wrong scope", 0.9)]

    result = asyncio.run(
        evaluate_production_cases(
            [_case("single", papers=["p1"], pages=[("p1", 1)])],
            user_id="u1",
            paper_id_map={"p1": "local-1"},
            retriever=retriever,
            k=5,
        )
    )

    assert result["invalid_retrieval_count"] == 1
    assert result["retrieval_recall_at_k"]["value"] == 0.0
