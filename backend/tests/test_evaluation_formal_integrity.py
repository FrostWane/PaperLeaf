import hashlib
from pathlib import Path

from paperleaf_api.evaluation_formal_integrity import _sha, audit_formal_evidence
from paperleaf_api.evaluation_formal_protocol import matches_locked_text_sha


def test_formal_hash_contract_accepts_checkout_line_endings(tmp_path: Path) -> None:
    lf = tmp_path / "lf.jsonl"
    crlf = tmp_path / "crlf.jsonl"
    lf.write_bytes(b'{"id":1}\n{"id":2}\n')
    crlf.write_bytes(b'{"id":1}\r\n{"id":2}\r\n')

    assert _sha(lf) == _sha(crlf)
    frozen_windows_sha = hashlib.sha256(crlf.read_bytes()).hexdigest()
    assert matches_locked_text_sha(lf, frozen_windows_sha)


def test_committed_formal_evidence_is_complete_and_same_protocol() -> None:
    root = Path(__file__).parents[2]
    report = audit_formal_evidence(root)

    assert report["status"] == "automatic_evidence_complete_human_review_pending"
    assert report["dataset"]["oracle_frozen_sha256"] != report["dataset"][
        "oracle_repository_sha256"
    ]
    assert report["retrieval_layers"]["diagnostic"]["case_count_per_variant"] == 90
    assert report["retrieval_layers"]["hidden"]["case_count_per_variant"] == 100
    assert report["multi_agent"] == {
        "task_count": 30,
        "run_count": 90,
        "human_review_status": "pending",
        "worker_recovery_status": "passed",
    }
