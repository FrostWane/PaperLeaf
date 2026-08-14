"""导出并审计一次真实 Worker 强杀恢复实验。

该模块只读取持久化 Run、Job 和安全事件，不输出 claim token、问题正文或证据正文。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .db import get_session_factory
from .models import AgentRun, AgentRunEvent, Job

_EPOCH_RE = re.compile(r"^stage:compare:([^:]+):")


def _safe_event(record: AgentRunEvent) -> dict[str, Any] | None:
    data = dict(record.data or {})
    node = str(data.get("node") or "")
    if node not in {"plan_comparison", "compare_subtask", "merge_comparison"}:
        return None
    match = _EPOCH_RE.match(record.event_key or "")
    return {
        "sequence": record.sequence,
        "event": record.event,
        "node": node,
        "event_epoch": match.group(1) if match else None,
        "subtask_id": data.get("subtask_id"),
        "status": data.get("status"),
        "recovered": bool(data.get("recovered", False)),
        "error_category": data.get("error_category"),
        "created_at": record.created_at.isoformat(),
    }


def build_recovery_report(
    *,
    run: AgentRun,
    job: Job,
    events: list[AgentRunEvent],
    stale_claim_probe_rejected: bool | None,
) -> dict[str, Any]:
    safe_events = [item for event in events if (item := _safe_event(event)) is not None]
    epochs = list(dict.fromkeys(item["event_epoch"] for item in safe_events if item["event_epoch"]))
    first_epoch = epochs[0] if epochs else None
    first_successes = {
        str(item["subtask_id"])
        for item in safe_events
        if item["event_epoch"] == first_epoch
        and item["node"] == "compare_subtask"
        and item["event"] == "node_finished"
        and item["status"] == "succeeded"
        and item["subtask_id"]
    }
    first_started = {
        str(item["subtask_id"])
        for item in safe_events
        if item["event_epoch"] == first_epoch
        and item["node"] == "compare_subtask"
        and item["event"] == "node_started"
        and item["subtask_id"]
    }
    first_finished = {
        str(item["subtask_id"])
        for item in safe_events
        if item["event_epoch"] == first_epoch
        and item["node"] == "compare_subtask"
        and item["event"] == "node_finished"
        and item["subtask_id"]
    }
    later_started = {
        str(item["subtask_id"])
        for item in safe_events
        if item["event_epoch"] != first_epoch
        and item["node"] == "compare_subtask"
        and item["event"] == "node_started"
        and item["subtask_id"]
    }
    later_success_counts = {
        subtask_id: sum(
            item["event_epoch"] != first_epoch
            and item["node"] == "compare_subtask"
            and item["event"] == "node_finished"
            and item["status"] == "succeeded"
            and item["subtask_id"] == subtask_id
            for item in safe_events
        )
        for subtask_id in sorted(first_successes)
    }
    recovered_events = [item for item in safe_events if item["recovered"]]
    sequences = [item["sequence"] for item in safe_events]
    checks = {
        "run_reached_terminal_state": run.status in {"completed", "failed", "cancelled"},
        "job_reclaimed_after_kill": job.attempts >= 2,
        "multiple_claim_epochs_observed": len(epochs) >= 2,
        "successful_pre_kill_branches_not_reexecuted": bool(first_successes)
        and all(count == 0 for count in later_success_counts.values()),
        "checkpoint_resume_only_unfinished_branches": bool(later_started)
        and later_started.issubset(first_started - first_finished),
        "event_sequences_unique_and_ordered": sequences == sorted(set(sequences)),
        "stale_claim_write_rejected": stale_claim_probe_rejected,
    }
    required = [value for value in checks.values() if value is not None]
    status = "passed" if required and all(required) else "failed"
    return {
        "schema_version": 1,
        "evidence_level": "real_worker_kill_and_postgresql_checkpoint",
        "status": status,
        "run": {
            "run_id": run.id,
            "orchestration_version": run.orchestration_version,
            "status": run.status,
            "error_code": run.error_code,
            "duration_ms": run.duration_ms,
        },
        "job": {
            "job_id": job.id,
            "status": job.status.value if hasattr(job.status, "value") else str(job.status),
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
        },
        "claim_token_policy": {
            "tokens_exported": False,
            "event_epochs_are_truncated_sha256": True,
            "stale_claim_probe_rejected": stale_claim_probe_rejected,
        },
        "checkpoint_recovery": {
            "event_epochs": epochs,
            "pre_kill_succeeded_subtasks": sorted(first_successes),
            "pre_kill_unfinished_subtasks": sorted(first_started - first_finished),
            "post_reclaim_started_subtasks": sorted(later_started),
            "later_success_count_for_pre_kill_subtasks": later_success_counts,
            "recovered_event_count": len(recovered_events),
        },
        "checks": checks,
        "events": safe_events,
    }


async def export_recovery(
    run_id: str,
    *,
    stale_claim_probe_rejected: bool | None,
) -> dict[str, Any]:
    async with get_session_factory()() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id))
        job = await session.scalar(select(Job).where(Job.agent_run_id == run_id))
        events = list(
            await session.scalars(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run_id)
                .order_by(AgentRunEvent.sequence)
            )
        )
    if run is None or job is None:
        raise RuntimeError("未找到恢复实验对应的 Agent Run 或 Job")
    return build_recovery_report(
        run=run,
        job=job,
        events=events,
        stale_claim_probe_rejected=stale_claim_probe_rejected,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 PaperLeaf Worker 强杀恢复证据")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--stale-claim-probe",
        choices=("rejected", "not_run"),
        default="not_run",
    )
    args = parser.parse_args()
    result = asyncio.run(
        export_recovery(
            args.run_id,
            stale_claim_probe_rejected=(
                True if args.stale_claim_probe == "rejected" else None
            ),
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    args.output.write_text(payload, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": result["status"],
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
