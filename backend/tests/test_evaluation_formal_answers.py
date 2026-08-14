from __future__ import annotations

import json
from pathlib import Path

from paperleaf_api.evaluation_formal_answers import (
    _evaluation_client_message_id,
    aggregate_answer_metrics,
    audit_answer_citations,
    build_evaluation_repository,
    build_human_review_packet,
    validate_answer_protocol,
)
from paperleaf_api.repository import SQLAlchemyRepository


def test_evaluation_client_message_id_is_bounded_and_unique() -> None:
    first = _evaluation_client_message_id("prefix-" * 40, "case-" * 80)
    second = _evaluation_client_message_id("prefix-" * 40, "case-" * 80)

    assert len(first) <= 100
    assert first.startswith("eval-")
    assert first != second


def test_audit_answer_citations_checks_scope_page_and_gold() -> None:
    chunks = {
        "c1": {
            "paper_id": "local-a",
            "physical_page": 3,
            "owner_id": "u1",
            "paper_title": "A",
            "quote": "evidence",
        },
        "c2": {
            "paper_id": "local-b",
            "physical_page": 4,
            "owner_id": "u2",
            "paper_title": "B",
            "quote": "foreign",
        },
    }
    result = audit_answer_citations(
        citations=[
            {"chunk_id": "c1", "paper_id": "local-a", "physical_page": 3},
            {"chunk_id": "c2", "paper_id": "local-b", "physical_page": 5},
            {"chunk_id": "missing", "physical_page": 1},
        ],
        chunks=chunks,
        owner_id="u1",
        local_scope=["local-a"],
        local_to_logical={"local-a": "arxiv:a"},
        gold_groups=[{("arxiv:a", 3)}],
    )
    assert result["citation_count"] == 3
    assert result["legal_count"] == 1
    assert result["physical_page_legal_count"] == 1
    assert result["gold_useful_count"] == 1
    assert "cross_user_chunk" in result["citations"][1]["reasons"]
    assert "chunk_not_found" in result["citations"][2]["reasons"]


def _row(*, answerable: bool, abstained: bool, claims: int = 2, supported: int = 1):
    return {
        "case_id": "x",
        "query": "q",
        "answer": "a",
        "run_status": "completed",
        "answerable": answerable,
        "abstained": abstained,
        "duration_ms": 100,
        "model_call_count": 2,
        "tool_call_count": 1,
        "claim_count": claims,
        "cited_claim_count": claims,
        "supported_claim_count": supported,
        "citation_audit": {
            "citation_count": 2,
            "legal_count": 2,
            "physical_page_legal_count": 2,
            "gold_useful_count": 1,
            "citations": [],
        },
    }


def test_aggregate_answer_metrics_preserves_denominators() -> None:
    result = aggregate_answer_metrics(
        [_row(answerable=True, abstained=True), _row(answerable=False, abstained=False)]
    )
    assert result["citation_legality_rate"] == {"numerator": 4, "denominator": 4, "value": 1}
    assert result["claim_support_rate"]["value"] == 0.5
    assert result["unsupported_claim_rate"]["value"] == 0.5
    assert result["answerable_over_refusal_rate"]["value"] == 1
    assert result["unanswerable_wrong_answer_rate"]["value"] == 1
    assert result["human_review_status"] == "human_review_pending"


def test_human_review_packet_is_deterministic_and_unscored() -> None:
    rows = []
    for index in range(35):
        row = _row(answerable=True, abstained=False)
        row["case_id"] = f"case-{index}"
        row["citation_audit"]["citations"] = [
            {
                "legal": True,
                "paper_title": "Paper",
                "physical_page": 2,
                "quote": "quote",
            }
        ]
        rows.append(row)
    first = build_human_review_packet(rows, minimum_cases=30)
    second = build_human_review_packet(list(reversed(rows)), minimum_cases=30)
    assert first == second
    assert len(first) == 30
    assert all(item["ratings"]["human_annotator"] == "" for item in first)
    assert all(item["ratings"]["factuality_1_to_5"] is None for item in first)


def test_repository_answer_protocol_is_frozen_against_dataset_lock() -> None:
    backend = Path(__file__).resolve().parents[1]
    dataset = backend / "evaluation" / "datasets" / "paperleaf-formal-hidden-v1"
    protocol = json.loads((dataset / "answer-protocol.json").read_text(encoding="utf-8"))
    validate_answer_protocol(protocol, lock_path=dataset / "lock.json")


def test_formal_answer_repository_uses_configured_session_secret() -> None:
    repository = build_evaluation_repository()
    assert isinstance(repository, SQLAlchemyRepository)
    assert repository.session_secret
