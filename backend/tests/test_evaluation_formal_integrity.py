from pathlib import Path

from paperleaf_api.evaluation_formal_integrity import audit_formal_evidence


def test_committed_formal_evidence_is_complete_and_same_protocol() -> None:
    root = Path(__file__).parents[2]
    report = audit_formal_evidence(root)

    assert report["status"] == "automatic_evidence_complete_human_review_pending"
    assert report["retrieval_layers"]["diagnostic"]["case_count_per_variant"] == 90
    assert report["retrieval_layers"]["hidden"]["case_count_per_variant"] == 100
    assert report["multi_agent"] == {
        "task_count": 30,
        "run_count": 90,
        "human_review_status": "pending",
        "worker_recovery_status": "passed",
    }
