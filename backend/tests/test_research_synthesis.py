import asyncio

import pytest
from pydantic import ValidationError

from paperleaf_api.agent.research_synthesis import (
    FindingPacket,
    ResearchLeaseLostError,
    ResearchPlan,
    ResearchScopeViolationError,
    ResearchTask,
    ScoutResult,
    build_deterministic_research_plan,
    execute_research_plan,
    merge_findings,
)
from paperleaf_api.rag.citations import Evidence


def _evidence(
    chunk_id: str,
    paper_id: str,
    page: int,
    *,
    score: float = 1.0,
) -> Evidence:
    return Evidence(
        chunk_id=chunk_id,
        paper_id=paper_id,
        paper_title=f"论文 {paper_id}",
        physical_page=page,
        text=f"{paper_id} 第 {page} 页证据",
        retrieval_score=score,
        retrieval_channels=("test",),
    )


def test_deterministic_plan_is_stable_bounded_and_disjoint() -> None:
    first = build_deterministic_research_plan(
        "比较方法、实验与局限",
        ["p5", "p2", "p1", "p4", "p3"],
        ["局限", "方法", "实验"],
        total_token_budget=3073,
    )
    second = build_deterministic_research_plan(
        "  比较方法、实验与局限  ",
        ["p3", "p4", "p1", "p2", "p5"],
        ["实验", "方法", "局限"],
        total_token_budget=3073,
    )

    assert first == second
    assert len(first.tasks) == 3
    assert [task.paper_ids for task in first.tasks] == [
        ("p1", "p4"),
        ("p2", "p5"),
        ("p3",),
    ]
    assert sum(task.token_budget for task in first.tasks) == 3073
    assert len({paper for task in first.tasks for paper in task.paper_ids}) == 5
    assert all(task.role == "evidence_scout" and task.max_tool_steps == 2 for task in first.tasks)


def test_strong_schemas_reject_four_branches_duplicates_and_invalid_success() -> None:
    task = ResearchTask(
        subtask_id="r:s1",
        objective="比较方法",
        paper_ids=("p1",),
        dimensions=("方法",),
        token_budget=512,
    )
    with pytest.raises(ValidationError):
        ResearchPlan(task_id="r", objective="比较方法", tasks=(task, task, task, task))
    with pytest.raises(ValidationError):
        ResearchPlan(task_id="r", objective="比较方法", tasks=(task, task))
    with pytest.raises(ValidationError):
        FindingPacket(subtask_id="r:s1", status="succeeded")
    with pytest.raises(ValidationError):
        FindingPacket(
            subtask_id="r:s1",
            status="failed",
            error_code="FAILED",
            chunk_ids=("c1",),
        )


def test_parallel_scouts_are_bounded_and_events_are_structured() -> None:
    async def scenario() -> None:
        plan = build_deterministic_research_plan(
            "比较三篇论文",
            ["p1", "p2", "p3"],
            ["方法"],
            total_token_budget=1536,
        )
        active = 0
        max_active = 0
        entered = 0
        all_entered = asyncio.Event()
        release = asyncio.Event()
        events: list[tuple[str, dict]] = []

        async def scout(task: ResearchTask) -> ScoutResult:
            nonlocal active, max_active, entered
            active += 1
            entered += 1
            max_active = max(max_active, active)
            if entered == 3:
                all_entered.set()
            await release.wait()
            active -= 1
            paper_id = task.paper_ids[0]
            return ScoutResult((_evidence(f"{paper_id}:c1", paper_id, 1),), confidence=0.9)

        async def event_sink(event: str, data: dict) -> None:
            events.append((event, data))

        execution = asyncio.create_task(
            execute_research_plan(
                plan,
                scout,
                allowed_paper_ids=["p1", "p2", "p3"],
                branch_timeout_seconds=1,
                event_sink=event_sink,
            )
        )
        await asyncio.wait_for(all_entered.wait(), timeout=0.5)
        release.set()
        result = await execution

        assert max_active == 3
        assert result.report.status == "succeeded"
        assert [item.paper_id for item in result.evidence] == ["p1", "p2", "p3"]
        assert [event for event, _ in events].count("subtask_started") == 3
        assert [event for event, _ in events].count("subtask_finished") == 3
        assert events[-1][0] == "merge_finished"
        assert all("orchestration_version" in data for _, data in events)
        assert all("objective" not in data for _, data in events)

    asyncio.run(scenario())


def test_timeout_is_partial_and_all_failures_request_fallback() -> None:
    async def partial_scenario() -> None:
        plan = build_deterministic_research_plan(
            "比较论文",
            ["p1", "p2", "p3"],
            ["方法"],
            total_token_budget=1536,
        )
        slow_id = plan.tasks[-1].subtask_id

        async def scout(task: ResearchTask) -> ScoutResult:
            if task.subtask_id == slow_id:
                await asyncio.sleep(0.05)
            paper_id = task.paper_ids[0]
            return ScoutResult((_evidence(f"{paper_id}:c1", paper_id, 1),))

        result = await execute_research_plan(
            plan,
            scout,
            allowed_paper_ids=["p1", "p2", "p3"],
            branch_timeout_seconds=0.01,
        )
        assert result.report.status == "partial"
        assert result.report.failed_subtasks == (slow_id,)
        assert "1/3" in str(result.report.coverage_notice)
        assert result.fallback_required is False
        assert {item.paper_id for item in result.evidence} == {"p1", "p2"}

    async def failed_scenario() -> None:
        plan = build_deterministic_research_plan(
            "比较论文",
            ["p1", "p2", "p3"],
            ["方法"],
            total_token_budget=1536,
        )

        async def scout(_task: ResearchTask) -> ScoutResult:
            raise RuntimeError("provider failed")

        result = await execute_research_plan(
            plan,
            scout,
            allowed_paper_ids=["p1", "p2", "p3"],
            branch_timeout_seconds=1,
        )
        assert result.report.status == "failed"
        assert len(result.report.failed_subtasks) == 3
        assert result.evidence == ()
        assert result.fallback_required is True
        assert {item.error_code for item in result.report.findings} == {"SCOUT_FAILED"}

    asyncio.run(partial_scenario())
    asyncio.run(failed_scenario())


def test_merge_revalidates_scope_deduplicates_pages_and_preserves_paper_diversity() -> None:
    plan = build_deterministic_research_plan(
        "比较论文",
        ["p1", "p2", "p3"],
        ["方法"],
        total_token_budget=1536,
    )
    findings: list[FindingPacket] = []
    evidence_by_subtask: dict[str, tuple[Evidence, ...]] = {}
    for task in plan.tasks:
        paper_id = task.paper_ids[0]
        evidence = [
            _evidence(f"{paper_id}:c1", paper_id, 1, score=1.0),
            _evidence(f"{paper_id}:c2", paper_id, 1, score=0.9),
            _evidence(f"{paper_id}:c3", paper_id, 2, score=0.8),
        ]
        if paper_id == "p1":
            evidence.extend(
                [
                    _evidence("p1:c1", "p1", 1, score=0.7),
                    _evidence("foreign:c1", "foreign", 9, score=2.0),
                ]
            )
        findings.append(
            FindingPacket(
                subtask_id=task.subtask_id,
                status="succeeded",
                chunk_ids=tuple(dict.fromkeys(item.chunk_id for item in evidence)),
                claim="相同主张" if paper_id != "p3" else "另一主张",
                stance="support" if paper_id == "p1" else "contradict",
                confidence=0.8,
            )
        )
        evidence_by_subtask[task.subtask_id] = tuple(evidence)

    report, merged = merge_findings(
        plan,
        findings,
        evidence_by_subtask,
        allowed_paper_ids=["p1", "p2", "p3"],
        max_evidence=3,
        max_evidence_per_paper=2,
    )

    assert report.status == "succeeded"
    assert [item.paper_id for item in merged] == ["p1", "p2", "p3"]
    assert len({(item.paper_id, item.physical_page) for item in merged}) == 3
    assert report.dedup_count == 4  # 每篇同页一条 + p1 重复 Chunk 一条
    assert report.rejected_scope_count == 1
    assert report.conflict_count == 1
    assert "foreign:c1" not in report.evidence_chunk_ids


def test_branch_cannot_return_another_allowed_branch_paper() -> None:
    plan = build_deterministic_research_plan(
        "比较论文",
        ["p1", "p2"],
        ["方法"],
        total_token_budget=1024,
    )
    first, second = plan.tasks
    findings = [
        FindingPacket(
            subtask_id=first.subtask_id,
            status="succeeded",
            chunk_ids=("p2:c1",),
        ),
        FindingPacket(
            subtask_id=second.subtask_id,
            status="succeeded",
            chunk_ids=("p2:c2",),
        ),
    ]
    report, merged = merge_findings(
        plan,
        findings,
        {
            first.subtask_id: (_evidence("p2:c1", "p2", 1),),
            second.subtask_id: (_evidence("p2:c2", "p2", 2),),
        },
        allowed_paper_ids=["p1", "p2"],
    )

    assert report.status == "partial"
    assert report.failed_subtasks == (first.subtask_id,)
    assert report.rejected_scope_count == 1
    assert [item.chunk_id for item in merged] == ["p2:c2"]


def test_plan_scope_violation_and_lease_loss_stop_execution() -> None:
    async def scenario() -> None:
        plan = build_deterministic_research_plan(
            "比较论文",
            ["p1", "p2"],
            ["方法"],
            total_token_budget=1024,
        )
        calls = 0

        async def scout(_task: ResearchTask) -> ScoutResult:
            nonlocal calls
            calls += 1
            return ScoutResult((_evidence("c1", "p1", 1),))

        with pytest.raises(ResearchScopeViolationError):
            await execute_research_plan(
                plan,
                scout,
                allowed_paper_ids=["p1"],
                branch_timeout_seconds=1,
            )
        assert calls == 0

        with pytest.raises(ResearchLeaseLostError):
            await execute_research_plan(
                plan,
                scout,
                allowed_paper_ids=["p1", "p2"],
                branch_timeout_seconds=1,
                lease_guard=lambda: False,
            )
        assert calls == 0

    asyncio.run(scenario())


def test_lease_loss_after_scout_prevents_merge_event() -> None:
    async def scenario() -> None:
        plan = build_deterministic_research_plan(
            "比较论文",
            ["p1"],
            ["方法"],
            total_token_budget=512,
        )
        lease_checks = 0
        events: list[str] = []

        async def lease_guard() -> bool:
            nonlocal lease_checks
            lease_checks += 1
            return lease_checks < 3

        async def scout(task: ResearchTask) -> ScoutResult:
            return ScoutResult((_evidence("p1:c1", task.paper_ids[0], 1),))

        with pytest.raises(ResearchLeaseLostError):
            await execute_research_plan(
                plan,
                scout,
                allowed_paper_ids=["p1"],
                branch_timeout_seconds=1,
                lease_guard=lease_guard,
                event_sink=lambda event, _data: events.append(event),
            )
        assert events == ["subtask_started"]
        assert "merge_finished" not in events

    asyncio.run(scenario())
