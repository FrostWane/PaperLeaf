from datetime import datetime, timezone
from types import SimpleNamespace

from paperleaf_api.evaluation_worker_recovery import build_recovery_report
from paperleaf_api.models import JobStatus


def _event(sequence: int, epoch: str, subtask: str, status: str, *, recovered=False):
    return SimpleNamespace(
        sequence=sequence,
        event="node_finished",
        event_key=f"stage:compare:{epoch}:subtask:{subtask}:finish",
        data={
            "node": "compare_subtask",
            "subtask_id": subtask,
            "status": status,
            "recovered": recovered,
        },
        created_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )


def _started(sequence: int, epoch: str, subtask: str):
    item = _event(sequence, epoch, subtask, "")
    item.event = "node_started"
    item.event_key = f"stage:compare:{epoch}:subtask:{subtask}:start"
    return item


def test_recovery_report_proves_checkpoint_resume_without_repeating_success():
    report = build_recovery_report(
        run=SimpleNamespace(
            id="run-1",
            status="completed",
            orchestration_version="specialist_subgraph_v3",
            error_code=None,
            duration_ms=120000,
        ),
        job=SimpleNamespace(
            id="job-1", status=JobStatus.completed, attempts=2, max_attempts=3
        ),
        events=[
            _started(1, "epoch1", "s1"),
            _started(2, "epoch1", "s2"),
            _started(3, "epoch1", "s3"),
            _event(4, "epoch1", "s1", "succeeded"),
            _event(5, "epoch1", "s3", "succeeded"),
            _started(6, "epoch2", "s2"),
            _event(7, "epoch2", "s2", "succeeded", recovered=True),
        ],
        stale_claim_probe_rejected=True,
    )

    assert report["status"] == "passed"
    assert report["checkpoint_recovery"]["pre_kill_succeeded_subtasks"] == ["s1", "s3"]
    assert report["checkpoint_recovery"]["later_success_count_for_pre_kill_subtasks"] == {
        "s1": 0,
        "s3": 0,
    }
    assert report["claim_token_policy"]["tokens_exported"] is False


def test_recovery_report_fails_when_completed_branch_is_reexecuted():
    report = build_recovery_report(
        run=SimpleNamespace(
            id="run-1",
            status="completed",
            orchestration_version="specialist_subgraph_v3",
            error_code=None,
            duration_ms=120000,
        ),
        job=SimpleNamespace(
            id="job-1", status=JobStatus.completed, attempts=2, max_attempts=3
        ),
        events=[
            _started(1, "epoch1", "s1"),
            _started(2, "epoch1", "s2"),
            _event(3, "epoch1", "s1", "succeeded"),
            _started(4, "epoch2", "s1"),
            _event(5, "epoch2", "s1", "succeeded", recovered=True),
        ],
        stale_claim_probe_rejected=True,
    )

    assert report["status"] == "failed"
    assert report["checks"]["successful_pre_kill_branches_not_reexecuted"] is False
