from paperleaf_api.evaluation_formal_report import _case_scores, _language_bucket_metrics


def test_case_scores_use_best_group_and_cross_paper_coverage() -> None:
    scores = _case_scores(
        {
            "best_evidence_group": {
                "required_pages": 3,
                "retrieved_pages": 2,
                "complete_hit": False,
            },
            "gold_page_ranks": [{"rank": 2}, {"rank": None}, {"rank": 4}],
            "required_paper_coverage": {"numerator": 2, "denominator": 3},
        }
    )
    assert scores == {
        "page_macro_recall": 2 / 3,
        "mrr": 0.5,
        "complete_group_hit": 0.0,
        "required_paper_coverage": 2 / 3,
    }


def test_language_buckets_report_cjk_and_latin_with_exact_denominators() -> None:
    def row(query: str, *, answerable: bool, rank: int | None) -> dict:
        return {
            "query": query,
            "answerable": answerable,
            "latency_ms": 100 if "论文" in query else 200,
            "top_5": [{"chunk_id": "c1"}],
            "best_evidence_group": {
                "required_pages": 1 if answerable else 0,
                "retrieved_pages": int(rank is not None),
                "complete_hit": rank is not None,
            },
            "gold_page_ranks": [{"rank": rank}] if answerable else [],
            "required_paper_coverage": {"numerator": 0, "denominator": 0},
        }

    metrics = _language_bucket_metrics(
        [
            row("这篇论文的方法是什么？", answerable=True, rank=2),
            row("What method is used?", answerable=True, rank=None),
            row("What is not stated?", answerable=False, rank=None),
        ]
    )
    assert metrics["cjk_query"]["mrr_at_5"] == {
        "numerator": 0.5,
        "denominator": 1,
        "value": 0.5,
    }
    assert metrics["latin_query"]["page_micro_recall_at_5"]["value"] == 0
    assert metrics["latin_query"]["unanswerable_false_retrieval_rate"] == {
        "numerator": 1,
        "denominator": 1,
        "value": 1.0,
    }
