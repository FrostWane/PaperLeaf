import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from paperleaf_api.agent.research_specialist_graph import (
    ResearchGraphContext,
    build_research_specialist_graph,
    merge_branch_results,
    research_checkpoint_namespace,
    specialist_branch_thread_id,
    specialist_result_from_state,
)
from paperleaf_api.agent.research_specialists import EvidenceSpecialist
from paperleaf_api.agent.research_synthesis import ResearchTask
from paperleaf_api.rag.citations import Evidence


def _evidence(task: ResearchTask) -> Evidence:
    paper_id = task.paper_ids[0]
    return Evidence(
        chunk_id=f"{paper_id}:chunk",
        paper_id=paper_id,
        paper_title=f"论文 {paper_id}",
        physical_page=1,
        text=f"{paper_id} 使用独立分支证据完成研究比较。",
        retrieval_score=1.0,
        retrieval_channels=("test",),
    )


def _input() -> dict:
    return {
        "run_id": "run-1",
        "parent_thread_id": "thread-1",
        "user_id": "user-1",
        "objective": "比较三篇论文的核心方法",
        "paper_ids": ["p1", "p2", "p3"],
        "dimensions": ["核心方法"],
        "max_branches": 3,
        "total_token_budget": 6144,
        "branch_results": {},
    }


def _successful_model():
    async def model(messages: tuple[dict[str, str], ...], *, max_output_tokens: int):
        assert len(messages) == 2
        assert max_output_tokens > 0
        return {
            "claims": [
                {
                    "dimension": "核心方法",
                    "claim": "该论文给出了有证据支持的方法。",
                    "evidence_aliases": ["E1"],
                    "stance": "support",
                    "confidence": 0.9,
                }
            ]
        }

    return model


def test_graph_runs_three_read_only_specialists_in_parallel_and_merges() -> None:
    async def scenario() -> None:
        entered = 0
        maximum = 0
        active = 0
        all_entered = asyncio.Event()
        release = asyncio.Event()

        async def retriever(task: ResearchTask):
            nonlocal entered, maximum, active
            entered += 1
            active += 1
            maximum = max(maximum, active)
            if entered == 3:
                all_entered.set()
            await release.wait()
            active -= 1
            return [_evidence(task)]

        graph = build_research_specialist_graph()
        invocation = asyncio.create_task(
            graph.ainvoke(
                _input(),
                context=ResearchGraphContext(
                    retriever=retriever,
                    specialist=EvidenceSpecialist(_successful_model(), timeout_seconds=1),
                    branch_timeout_seconds=1,
                ),
            )
        )
        await asyncio.wait_for(all_entered.wait(), timeout=1)
        release.set()
        state = await invocation
        result = specialist_result_from_state(state)

        assert maximum == 3
        assert state["status"] == "succeeded"
        assert sorted(state["branch_results"]) == sorted(
            task.subtask_id for task in result.plan.tasks
        )
        assert [item.paper_id for item in result.evidence] == ["p1", "p2", "p3"]
        assert all(item.status == "succeeded" for item in result.report.findings)
        assert all(item.duration_ms >= 1 for item in result.report.findings)
        assert result.report.merge_duration_ms >= 1
        assert all(envelope["claims"] for envelope in state["branch_results"].values())
        assert all(envelope["duration_ms"] >= 1 for envelope in state["branch_results"].values())

    asyncio.run(scenario())


def test_graph_preserves_success_when_one_specialist_is_invalid() -> None:
    async def scenario() -> None:
        async def retriever(task: ResearchTask):
            return [_evidence(task)]

        async def model(messages: tuple[dict[str, str], ...], *, max_output_tokens: int):
            assert max_output_tokens > 0
            alias = "E999" if "论文 p2" in messages[1]["content"] else "E1"
            return {
                "claims": [
                    {
                        "dimension": "核心方法",
                        "claim": "分支主张",
                        "evidence_aliases": [alias],
                        "stance": "support",
                        "confidence": 0.8,
                    }
                ]
            }

        graph = build_research_specialist_graph()
        state = await graph.ainvoke(
            _input(),
            context=ResearchGraphContext(
                retriever=retriever,
                specialist=EvidenceSpecialist(model, timeout_seconds=1),
                branch_timeout_seconds=1,
            ),
        )

        assert state["status"] == "succeeded"
        assert {item["status"] for item in state["branch_results"].values()} == {"succeeded"}
        fallback = [
            item
            for item in state["branch_results"].values()
            if item["usage"]["schema_fallback_used"]
        ]
        assert len(fallback) == 1
        assert fallback[0]["claims"] == []
        assert {item["paper_id"] for item in state["merged_evidence"]} == {"p1", "p2", "p3"}

    asyncio.run(scenario())


def test_graph_preserves_cross_paper_conflicts_and_branch_metrics() -> None:
    async def scenario() -> None:
        async def retriever(task: ResearchTask):
            return [_evidence(task)]

        async def model(messages: tuple[dict[str, str], ...], *, max_output_tokens: int):
            assert max_output_tokens > 0
            content = messages[1]["content"]
            paper_id = next(value for value in ("p1", "p2", "p3") if f"论文 {value}" in content)
            stance = "contradict" if paper_id == "p2" else "support"
            return {
                "claims": [
                    {
                        "dimension": "主要结果",
                        "claim_key": "迁移性能提升",
                        "claim": f"{paper_id} 对迁移性能给出{stance}证据。",
                        "evidence_aliases": ["E1"],
                        "stance": stance,
                        "confidence": 0.85,
                    }
                ]
            }

        graph = build_research_specialist_graph()
        initial = _input() | {"dimensions": ["主要结果"]}
        state = await graph.ainvoke(
            initial,
            context=ResearchGraphContext(
                retriever=retriever,
                specialist=EvidenceSpecialist(model, timeout_seconds=1),
                branch_timeout_seconds=1,
            ),
        )
        result = specialist_result_from_state(state)

        assert result.report.conflict_count == 1
        assert len(result.conflict_sets) == 1
        conflict = result.conflict_sets[0]
        assert conflict["claim_key"] == "迁移性能提升"
        assert {item["paper_ids"][0] for item in conflict["support"]} == {"p1", "p3"}
        assert {item["paper_ids"][0] for item in conflict["contradict"]} == {"p2"}
        assert len(result.branch_metrics) == 3
        assert all(item["evidence_count"] == 1 for item in result.branch_metrics)
        assert all(item["input_tokens"] > 0 for item in result.branch_metrics)
        assert all(item["output_tokens"] > 0 for item in result.branch_metrics)
        assert all(item["schema_repair_count"] == 0 for item in result.branch_metrics)

    asyncio.run(scenario())


def test_graph_turns_one_branch_timeout_into_partial_result() -> None:
    async def scenario() -> None:
        async def retriever(task: ResearchTask):
            if task.paper_ids == ("p2",):
                await asyncio.sleep(0.05)
            return [_evidence(task)]

        graph = build_research_specialist_graph()
        state = await graph.ainvoke(
            _input(),
            context=ResearchGraphContext(
                retriever=retriever,
                specialist=EvidenceSpecialist(_successful_model(), timeout_seconds=1),
                branch_timeout_seconds=0.01,
            ),
        )

        assert state["status"] == "partial"
        timeout = [item for item in state["branch_results"].values() if item["status"] == "timeout"]
        assert len(timeout) == 1
        assert timeout[0]["finding"]["error_code"] == "SPECIALIST_TIMEOUT"
        assert {item["paper_id"] for item in state["merged_evidence"]} == {"p1", "p3"}

    asyncio.run(scenario())


def test_graph_keeps_validated_evidence_when_only_specialist_model_times_out() -> None:
    async def scenario() -> None:
        async def retriever(task: ResearchTask):
            return [_evidence(task)]

        async def slow_model(
            _messages: tuple[dict[str, str], ...], *, max_output_tokens: int
        ):
            assert max_output_tokens > 0
            await asyncio.sleep(0.1)
            return {"claims": []}

        graph = build_research_specialist_graph()
        state = await graph.ainvoke(
            _input(),
            context=ResearchGraphContext(
                retriever=retriever,
                specialist=EvidenceSpecialist(slow_model, timeout_seconds=1),
                branch_timeout_seconds=0.02,
            ),
        )

        assert state["status"] == "succeeded"
        assert len(state["merged_evidence"]) == 3
        assert all(
            item["status"] == "succeeded"
            and item["usage"]["timeout_fallback_used"] is True
            and item["claims"] == []
            for item in state["branch_results"].values()
        )
        assert all(
            item["timeout_fallback_used"] is True for item in state["branch_metrics"]
        )

    asyncio.run(scenario())


def test_graph_all_failures_request_fallback_and_scope_is_revalidated() -> None:
    async def scenario() -> None:
        async def invalid_retriever(task: ResearchTask):
            evidence = _evidence(task)
            return [
                Evidence(
                    chunk_id=evidence.chunk_id,
                    paper_id="foreign",
                    paper_title="越权论文",
                    physical_page=1,
                    text="不应进入模型",
                    retrieval_channels=("test",),
                )
            ]

        graph = build_research_specialist_graph()
        state = await graph.ainvoke(
            _input(),
            context=ResearchGraphContext(
                retriever=invalid_retriever,
                specialist=EvidenceSpecialist(_successful_model(), timeout_seconds=1),
                branch_timeout_seconds=1,
            ),
        )

        assert state["status"] == "failed"
        assert state["fallback_reason"] == "all_specialists_failed"
        assert state["merged_evidence"] == []
        assert {item["finding"]["error_code"] for item in state["branch_results"].values()} == {
            "SPECIALIST_NO_SCOPED_EVIDENCE"
        }

    asyncio.run(scenario())


def test_branch_reducer_is_commutative_idempotent_and_prefers_success() -> None:
    failed = {
        "s1": {
            "generation": 1,
            "status": "failed",
            "finding": {"error_code": "FAILED"},
        }
    }
    succeeded = {
        "s1": {
            "generation": 1,
            "status": "succeeded",
            "finding": {"chunk_ids": ["c1"]},
        }
    }
    another = {"s2": {"generation": 1, "status": "timeout", "finding": {}}}

    forward = merge_branch_results(merge_branch_results({}, failed), succeeded | another)
    reverse = merge_branch_results(merge_branch_results({}, succeeded | another), failed)
    replayed = merge_branch_results(forward, succeeded | another)

    assert forward == reverse == replayed
    assert forward["s1"]["status"] == "succeeded"
    assert list(forward) == ["s1", "s2"]


def test_checkpoint_resume_does_not_repeat_completed_specialist_branch() -> None:
    async def scenario() -> None:
        checkpointer = InMemorySaver()
        graph = build_research_specialist_graph(checkpointer)
        release_slow = asyncio.Event()
        fast_finished = asyncio.Event()
        calls = {"p1": 0, "p2": 0, "p3": 0}

        async def retriever(task: ResearchTask):
            return [_evidence(task)]

        async def model(messages: tuple[dict[str, str], ...], *, max_output_tokens: int):
            assert max_output_tokens > 0
            paper_id = next(
                paper for paper in calls if f"论文:{'论文 ' + paper}" in messages[1]["content"]
            )
            calls[paper_id] += 1
            if paper_id == "p1":
                fast_finished.set()
            else:
                await release_slow.wait()
            return {
                "claims": [
                    {
                        "dimension": "核心方法",
                        "claim": f"{paper_id} 的主张",
                        "evidence_aliases": ["E1"],
                        "stance": "support",
                        "confidence": 0.8,
                    }
                ]
            }

        context = ResearchGraphContext(
            retriever=retriever,
            specialist=EvidenceSpecialist(model, timeout_seconds=5),
            branch_timeout_seconds=5,
        )
        config = {
            "configurable": {
                "thread_id": "parent-run-1",
                "checkpoint_ns": research_checkpoint_namespace(),
            }
        }
        first = asyncio.create_task(graph.ainvoke(_input(), config, context=context))
        await asyncio.wait_for(fast_finished.wait(), timeout=1)
        await asyncio.sleep(0.05)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        branch_checkpoints = [
            await checkpointer.aget_tuple(
                {
                    "configurable": {
                        "thread_id": specialist_branch_thread_id("thread-1", ordinal),
                        "checkpoint_ns": "",
                    }
                }
            )
            for ordinal in range(1, 4)
        ]
        terminal_count = sum(
            item is not None
            and item.checkpoint.get("channel_values", {}).get("status") == "terminal"
            for item in branch_checkpoints
        )
        assert terminal_count == 1

        release_slow.set()
        state = await graph.ainvoke(None, config, context=context)

        assert state["status"] == "succeeded"
        assert calls["p1"] == 1
        assert calls["p2"] >= 1
        assert calls["p3"] >= 1
        assert len(state["branch_results"]) == 3

        other_namespace = {
            "configurable": {
                "thread_id": "parent-run-1",
                "checkpoint_ns": "another-version/research",
            }
        }
        assert await checkpointer.aget_tuple(other_namespace) is None

    asyncio.run(scenario())
