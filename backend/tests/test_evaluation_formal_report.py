from paperleaf_api.evaluation_formal_report import _case_scores


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
        "page_recall": 2 / 3,
        "mrr": 0.5,
        "complete_group_hit": 0.0,
        "required_paper_coverage": 2 / 3,
    }
