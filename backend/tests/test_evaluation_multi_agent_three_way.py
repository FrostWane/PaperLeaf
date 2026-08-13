import pytest

from paperleaf_api.evaluation_multi_agent_three_way import combine_captures, evaluate_three_way


def _variant(*, completed: bool, support: tuple[int, int], citations: tuple[int, int]):
    return {
        "execution_status": "executed",
        "run_status": "completed" if completed else "failed",
        "fallback_to_v1": False,
        "duration_ms": 100,
        "estimated_input_tokens": {"status": "measured", "value": 100},
        "estimated_output_tokens": {"status": "measured", "value": 20},
        "model_call_count": {"status": "measured", "value": 2},
        "tool_call_count": {"status": "measured", "value": 0},
        "measurements": {
            "citation_audit": {
                "status": "measured",
                "value": {
                    "correct_page_citations": citations[0],
                    "total_citations": citations[1],
                    "illegal_citation_count": 0,
                    "covered_source_case_ids": ["c1"],
                    "cited_paper_ids": ["p1"],
                },
            },
            "claim_support": {
                "status": "measured",
                "value": {"supported": support[0], "total": support[1]},
            },
            "branch_metrics": {"status": "not_measured", "value": None},
        },
    }


def test_three_way_report_keeps_human_review_separate_from_automatic_metrics() -> None:
    capture = {
        "capture_content_hash": "hash",
        "token_measurement": "estimated_not_provider_billed_usage",
        "pairs": [
            {
                "case_metrics": {"expected_claim_count": 1, "required_paper_count": 1},
                "v1": _variant(completed=True, support=(1, 2), citations=(1, 1)),
                "v2": _variant(completed=True, support=(2, 2), citations=(1, 1)),
                "v3": _variant(completed=True, support=(2, 2), citations=(1, 1)),
            }
        ],
    }
    pending = evaluate_three_way(capture, [])
    assert pending["decision"] == "quality_pending"
    assert pending["variants"]["v3"]["completion_rate"]["value"] == 1
    assert pending["variants"]["v1"]["output_claim_support_rate"]["value"] == 0.5
    assert pending["variants"]["v3"]["monetary_cost"]["status"] == "not_measured"

    blind = [
        {
            "case_id": "case-1",
            "input_hash": "hash-1",
            "rating": {
                "preferred": "C",
                "human_annotator": "reviewer-1",
                "factuality": {"A": 4, "B": 3, "C": 5},
                "usefulness": {"A": 4, "B": 3, "C": 5},
                "conflict_handling": {"A": 3, "B": 2, "C": 5},
            },
        }
    ]
    keys = [
        {
            "case_id": "case-1",
            "input_hash": "hash-1",
            "mapping": {"A": "v2", "B": "v1", "C": "v3"},
        }
    ]
    completed = evaluate_three_way(capture, blind, keys)
    assert completed["decision"] == "ready_for_engineering_decision"
    assert completed["human_blind_review"]["preferences"] == {"v3": 1}
    assert completed["human_blind_review"]["scores"]["v3"]["usefulness"]["mean"] == 5


def test_combine_captures_requires_same_frozen_protocol() -> None:
    first = {
        "dataset": {"id": "frozen-v1"},
        "token_measurement": "estimated",
        "capture_content_hash": "a",
        "pairs": [{"case_id": "c1"}],
    }
    second = {
        "dataset": {"id": "frozen-v1"},
        "token_measurement": "estimated",
        "capture_content_hash": "b",
        "pairs": [{"case_id": "c2"}],
    }
    combined = combine_captures([first, second])
    assert [item["case_id"] for item in combined["pairs"]] == ["c1", "c2"]
    assert combined["capture_content_hashes"] == ["a", "b"]
    with pytest.raises(ValueError, match="口径不一致"):
        combine_captures([first, {**second, "token_measurement": "provider_billed"}])
