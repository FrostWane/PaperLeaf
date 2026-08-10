from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from paperleaf_api.agent.function_tools import (
    FunctionToolHarness,
    PlannerDecision,
    ToolCallRequest,
)
from paperleaf_api.agent_execution import execute_agent_run
from paperleaf_api.config import settings
from paperleaf_api.model_runtime import ModelRuntimeError
from paperleaf_api.models import UserRole
from paperleaf_api.rag.answer_quality import AnswerQualityPolicy
from paperleaf_api.rag.citations import CitationClaim, Evidence
from paperleaf_api.repository import (
    ChatActiveRunError,
    ChatIdempotencyConflictError,
    MemoryRepository,
    PaperRecord,
    UserRecord,
)


def _valid_result() -> dict:
    evidence = [
        Evidence("c1", "p1", "论文一", 2, "方法使用页级混合检索。"),
        Evidence("c2", "p1", "论文一", 5, "实验显示引用页准确率提高。"),
    ]
    return {
        "status": "completed",
        "answer": ("方法使用页级混合检索 [chunk:c1]。\n\n- 实验显示引用页准确率提高 [chunk:c2]。"),
        "retrieved_evidence": evidence,
        "citations": [
            CitationClaim("c1", "p1", 2, evidence[0].text),
            CitationClaim("c2", "p1", 5, evidence[1].text),
        ],
        "evidence_quality": {
            "grade": "sufficient",
            "answer_support_grade": "supported",
            "answer_support_confidence": 0.99,
            "reason_code": "answer_supported",
        },
        "tool_steps": 1,
    }


class ResultGraph:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result or _valid_result()

    async def ainvoke(self, _initial: dict, _config: dict) -> dict:
        return self.result


class TimedOutGraph:
    async def ainvoke(self, _initial: dict, _config: dict) -> dict:
        raise ModelRuntimeError("MODEL_TIMEOUT", [])


class CapturingResultGraph(ResultGraph):
    def __init__(self, result: dict | None = None) -> None:
        super().__init__(result)
        self.initial: dict | None = None

    async def ainvoke(self, initial: dict, _config: dict) -> dict:
        self.initial = initial
        return self.result


class RejectingToolPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, **_kwargs) -> PlannerDecision:
        self.calls += 1
        if self.calls == 1:
            return PlannerDecision((ToolCallRequest("rejected-1", "unknown_tool", {}),))
        return PlannerDecision()


class EmptyToolRetriever:
    async def __call__(self, _payload):
        return []


async def _submitted_run(repository: MemoryRepository, user_id: str = "u1"):
    chat_session = await repository.create_chat_session(user_id, "新会话", "library", None, None)
    submission = await repository.submit_chat_message(
        chat_session.id,
        user_id,
        "比较方法和实验",
        "client-1",
        "hash-1",
        {"type": "library", "paper_ids": ["p1"], "web_enabled": False},
    )
    assert submission is not None
    claim_token = await repository.claim_agent_run_job(submission.run.id)
    assert claim_token is not None
    return chat_session, submission, claim_token


def test_chat_submission_is_idempotent_and_user_isolated() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        chat_session, submission, _claim = await _submitted_run(repository)

        replay = await repository.submit_chat_message(
            chat_session.id,
            "u1",
            "比较方法和实验",
            "client-1",
            "hash-1",
            {"type": "library", "paper_ids": ["p1"]},
        )
        assert replay is not None and replay.replayed is True
        assert replay.run.id == submission.run.id
        assert await repository.get_owned_chat_session(chat_session.id, "u2") is None
        assert await repository.list_chat_messages(chat_session.id, "u2") is None

        with pytest.raises(ChatIdempotencyConflictError):
            await repository.submit_chat_message(
                chat_session.id,
                "u1",
                "不同请求",
                "client-1",
                "hash-2",
                {"type": "library", "paper_ids": ["p1"]},
            )
        with pytest.raises(ChatActiveRunError):
            await repository.submit_chat_message(
                chat_session.id,
                "u1",
                "第二个并发请求",
                "client-2",
                "hash-3",
                {"type": "library", "paper_ids": ["p1"]},
            )

    asyncio.run(scenario())


def test_rejected_function_tool_is_visible_then_legacy_retrieval_runs() -> None:
    """确定性集成测试：不使用真实模型，只验证 Harness→SSE→legacy 降级。"""

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        chat_session = await repository.create_chat_session(
            "u1", "工具降级", "library", None, None
        )
        submission = await repository.submit_chat_message(
            chat_session.id,
            "u1",
            "解释论文的方法",
            "tool-fallback-client",
            "tool-fallback-hash",
            {
                "type": "library",
                "paper_ids": ["p1"],
                "web_enabled": False,
                "harness": {
                    "context_engine_enabled": True,
                    "memory_enabled": False,
                    "skills_enabled": True,
                    "function_tools_enabled": True,
                    "mcp_enabled": False,
                },
            },
        )
        assert submission is not None
        claim_token = await repository.claim_agent_run_job(submission.run.id)
        assert claim_token is not None
        harness = FunctionToolHarness(
            repository,
            EmptyToolRetriever(),
            object(),
            planner=RejectingToolPlanner(),
        )

        await execute_agent_run(
            repository,
            ResultGraph(),
            submission.run.id,
            claim_token,
            answer_quality_policy=AnswerQualityPolicy(),
            function_tool_harness=harness,
        )

        run = await repository.get_agent_run(submission.run.id)
        assert run is not None and run.status == "completed"
        assert run.harness_trace["tool_mode_active"] is False
        assert run.harness_trace["function_fallback_reason"] == "tool_outputs_not_usable"
        assert run.harness_trace["tool_calls"] == [
            {"tool": "unknown_tool", "status": "rejected"}
        ]
        events = await repository.list_owned_agent_run_events(run.id, "u1")
        assert events is not None
        event_pairs = [(item.event, item.data.get("tool")) for item in events]
        assert ("tool_finished", "unknown_tool") in event_pairs
        assert ("tool_started", "search_library") in event_pairs
        assert ("tool_finished", "search_library") in event_pairs

    asyncio.run(scenario())


def test_delete_rechecks_active_run_after_route_precheck() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        chat_session = await repository.create_chat_session("u1", "删除竞态", "library", None, None)
        prechecked = await repository.get_owned_chat_session(chat_session.id, "u1")
        assert prechecked is not None and prechecked.current_run_id is None
        submission = await repository.submit_chat_message(
            chat_session.id,
            "u1",
            "在前置检查后提交",
            "delete-race-message-1",
            "delete-race-hash-1",
            {"type": "library", "paper_ids": []},
        )
        assert submission is not None

        with pytest.raises(ChatActiveRunError):
            await repository.delete_owned_chat_session(chat_session.id, "u1")

        assert await repository.get_owned_chat_session(chat_session.id, "u1")
        assert await repository.list_chat_messages(chat_session.id, "u1")
        assert submission.run.id in repository.agent_runs
        assert any(item.agent_run_id == submission.run.id for item in repository.jobs.values())

    asyncio.run(scenario())


def test_verified_markdown_blocks_replay_to_exact_persisted_message() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        _chat_session, submission, claim_token = await _submitted_run(repository)

        await execute_agent_run(
            repository,
            ResultGraph(),
            submission.run.id,
            claim_token,
            answer_quality_policy=AnswerQualityPolicy(),
        )

        run = await repository.get_agent_run(submission.run.id)
        assert run is not None and run.status == "completed"
        messages = await repository.list_chat_messages(run.session_id, run.user_id)
        assert messages is not None
        assistant = next(item for item in messages if item.role == "assistant")
        events = await repository.list_owned_agent_run_events(run.id, run.user_id)
        assert events is not None
        deltas = [item.data["delta"] for item in events if item.event == "message_delta"]
        assert "".join(deltas) == assistant.content
        assert deltas[1].startswith("\n\n- ")
        assert [item.sequence for item in events] == list(range(1, len(events) + 1))
        assert any(item.event_key == "stage:generate:start" for item in events)
        assert any(item.event_key == "stage:generate:finish" for item in events)
        assert all(item.data.get("citations") for item in events if item.event == "message_delta")
        trace = run.result_summary["rag_trace"]
        assert trace["intent"] == "comparison"
        assert trace["scope"] == "library"
        assert trace["outcome"] == "cited_answer"
        assert trace["citation_count"] == 2

    asyncio.run(scenario())


def test_completed_harness_run_extracts_only_explicit_user_memory() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        repository.users["u1"] = UserRecord(
            id="u1",
            email="reader@example.com",
            password_hash="unused",
            preferences={"memory_enabled": True},
            role=UserRole.user,
        )
        chat_session = await repository.create_chat_session(
            "u1", "记忆测试", "library", None, None
        )
        submission = await repository.submit_chat_message(
            chat_session.id,
            "u1",
            "请记住：以后回答默认使用中文",
            "memory-client-1",
            "memory-hash-1",
            {
                "type": "library",
                "paper_ids": ["p1"],
                "web_enabled": False,
                "harness": {
                    "context_engine_enabled": True,
                    "memory_enabled": True,
                    "skills_enabled": True,
                },
            },
        )
        assert submission is not None
        token = await repository.claim_agent_run_job(submission.run.id)
        assert token is not None
        config = replace(
            settings,
            memory_enabled=True,
            context_engine_enabled=True,
            skills_enabled=True,
            embedding_enabled=False,
            fallback_embedding_enabled=False,
        )
        await execute_agent_run(
            repository,
            ResultGraph(),
            submission.run.id,
            token,
            answer_quality_policy=AnswerQualityPolicy(),
            harness_config=config,
        )

        memories = await repository.list_memories("u1")
        assert len(memories) == 1
        assert memories[0].value == "以后回答默认使用中文"
        assert memories[0].pinned is True
        run = await repository.get_agent_run(submission.run.id)
        assert run is not None
        assert run.context_snapshot["budget"]["model_window"] == config.model_context_tokens
        assert run.selected_skill == "paper_qa"
        assert run.skill_version == 1
        assert run.harness_trace["skill_route_source"] == "deterministic_fallback"

    asyncio.run(scenario())


def test_model_timeout_is_reported_instead_of_publishing_raw_extract() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        _chat_session, submission, claim_token = await _submitted_run(repository)

        await execute_agent_run(
            repository,
            TimedOutGraph(),
            submission.run.id,
            claim_token,
            answer_quality_policy=AnswerQualityPolicy(),
        )

        run = await repository.get_agent_run(submission.run.id)
        assert run is not None and run.status == "failed"
        assert run.error_code == "MODEL_TIMEOUT"
        assert run.result_summary["answer"] == ""
        assert run.result_summary["rag_trace"]["failure_category"] == "model_timeout"
        messages = await repository.list_chat_messages(run.session_id, run.user_id)
        assert messages is not None
        assistant = next(item for item in messages if item.role == "assistant")
        assert assistant.content == ""

    asyncio.run(scenario())


def test_completed_discovery_task_is_inherited_by_recent_year_followup() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        session = await repository.create_chat_session(
            "u1", "多轮联网发现", "library", None, None
        )
        config = replace(
            settings,
            context_engine_enabled=True,
            skills_enabled=True,
            function_tools_enabled=False,
            memory_enabled=False,
        )
        first = await repository.submit_chat_message(
            session.id,
            "u1",
            "联网推荐 5 篇尚未在文献库中的相关论文",
            "discovery-message-1",
            "discovery-hash-1",
            {
                "type": "library",
                "paper_ids": ["p1"],
                "web_enabled": True,
                "harness": {
                    "context_engine_enabled": True,
                    "skills_enabled": True,
                    "function_tools_enabled": False,
                },
            },
        )
        assert first is not None
        first_token = await repository.claim_agent_run_job(first.run.id)
        assert first_token
        await execute_agent_run(
            repository,
            ResultGraph(),
            first.run.id,
            first_token,
            answer_quality_policy=AnswerQualityPolicy(),
            harness_config=config,
        )
        updated_session = await repository.get_owned_chat_session(session.id, "u1")
        assert updated_session is not None
        assert updated_session.entity_state["active_task"]["name"] == "find_related_papers"
        assert updated_session.entity_state["active_task"]["requested_count"] == 5

        followup = await repository.submit_chat_message(
            session.id,
            "u1",
            "有没有更近的论文，如2026年的",
            "discovery-message-2",
            "discovery-hash-2",
            {
                "type": "library",
                "paper_ids": ["p1"],
                "web_enabled": True,
                "harness": {
                    "context_engine_enabled": True,
                    "skills_enabled": True,
                    "function_tools_enabled": False,
                },
            },
        )
        assert followup is not None
        followup_token = await repository.claim_agent_run_job(followup.run.id)
        assert followup_token
        graph = CapturingResultGraph()
        await execute_agent_run(
            repository,
            graph,
            followup.run.id,
            followup_token,
            answer_quality_policy=AnswerQualityPolicy(),
            harness_config=config,
        )

        run = await repository.get_agent_run(followup.run.id)
        assert run is not None
        assert run.selected_skill == "find_related_papers"
        assert run.harness_trace["skill_route_source"] == "context_task_inheritance"
        assert run.context_snapshot["resolved_references"]["active_task"]["year_from"] == 2026
        assert graph.initial is not None
        assert graph.initial["intent"] == "literature_discovery"
        assert "继续联网推荐 5 篇" in graph.initial["query"]

    asyncio.run(scenario())


def test_discovery_run_loads_all_authorized_scope_titles_not_only_first_eight() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        paper_ids = [f"p{index}" for index in range(1, 11)]
        for index, paper_id in enumerate(paper_ids, start=1):
            repository.papers[paper_id] = PaperRecord(
                id=paper_id,
                owner_id="u1",
                title=f"Scope paper {index}",
                authors=[],
                year=2020 + index % 5,
                abstract=None,
                doi=f"10.1000/scope.{index}",
                arxiv_id=None,
                filename=f"{paper_id}.pdf",
                storage_key=f"u1/{paper_id}.pdf",
                mime_type="application/pdf",
                size_bytes=100,
                sha256=f"scope-{index}",
                page_count=1,
            )
        session = await repository.create_chat_session(
            "u1", "完整集合去重", "library", None, None
        )
        submission = await repository.submit_chat_message(
            session.id,
            "u1",
            "联网推荐五篇尚未入库的相关论文",
            "full-scope-message",
            "full-scope-hash",
            {
                "type": "library",
                "paper_ids": paper_ids,
                "web_enabled": True,
                "harness": {
                    "context_engine_enabled": True,
                    "skills_enabled": True,
                    "function_tools_enabled": False,
                },
            },
        )
        assert submission is not None
        token = await repository.claim_agent_run_job(submission.run.id)
        assert token
        graph = CapturingResultGraph()
        await execute_agent_run(
            repository,
            graph,
            submission.run.id,
            token,
            answer_quality_policy=AnswerQualityPolicy(),
            harness_config=replace(
                settings,
                context_engine_enabled=True,
                skills_enabled=True,
                function_tools_enabled=False,
                memory_enabled=False,
            ),
        )

        assert graph.initial is not None
        assert graph.initial["selected_skill"] == "find_related_papers"
        assert graph.initial["scope_paper_titles"] == sorted(
            (f"Scope paper {index}" for index in range(1, 11)),
            key=str.casefold,
        )

    asyncio.run(scenario())


def test_verified_paragraph_allows_heading_and_paragraph_end_citation() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        _chat_session, submission, claim_token = await _submitted_run(repository)
        result = _valid_result()
        result["answer"] = (
            "## 研究概览\n\n"
            "该方法使用页级混合检索。它先召回候选页面，再组织回答 [chunk:c1]。\n\n"
            "> 证据说明：当前检索片段与问题的匹配度有限，结论仅供初步参考。"
        )
        result["citations"] = [CitationClaim("c1", "p1", 2, "方法使用页级混合检索。")]

        await execute_agent_run(
            repository,
            ResultGraph(result),
            submission.run.id,
            claim_token,
            answer_quality_policy=AnswerQualityPolicy(),
        )

        run = await repository.get_agent_run(submission.run.id)
        assert run is not None and run.status == "completed"
        messages = await repository.list_chat_messages(run.session_id, run.user_id)
        assert messages is not None
        assistant = next(item for item in messages if item.role == "assistant")
        assert assistant.content == result["answer"]

    asyncio.run(scenario())


def test_invalid_paragraph_is_dropped_without_losing_verified_content() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        _chat_session, submission, claim_token = await _submitted_run(repository)
        result = _valid_result()
        result["answer"] = "方法使用页级混合检索 [chunk:c1]。\n\n第二段包含没有引用的事实。"

        await execute_agent_run(
            repository,
            ResultGraph(result),
            submission.run.id,
            claim_token,
            answer_quality_policy=AnswerQualityPolicy(),
        )

        run = await repository.get_agent_run(submission.run.id)
        assert run is not None and run.status == "completed"
        assert run.result_summary["dropped_paragraph_count"] == 1
        messages = await repository.list_chat_messages(run.session_id, run.user_id)
        assert messages is not None
        assistant = next(item for item in messages if item.role == "assistant")
        assert assistant.content == "方法使用页级混合检索 [chunk:c1]。"
        events = await repository.list_owned_agent_run_events(run.id, run.user_id)
        assert events is not None
        assert len([item for item in events if item.event == "message_delta"]) == 1

    asyncio.run(scenario())


def test_out_of_scope_evidence_is_never_published() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        _chat_session, submission, claim_token = await _submitted_run(repository)
        result = _valid_result()
        result["retrieved_evidence"] = [
            Evidence("foreign", "paper-of-u2", "他人论文", 1, "越权证据")
        ]
        result["citations"] = [CitationClaim("foreign", "paper-of-u2", 1, "越权证据")]
        result["answer"] = "越权证据 [chunk:foreign]。"

        await execute_agent_run(
            repository,
            ResultGraph(result),
            submission.run.id,
            claim_token,
            answer_quality_policy=AnswerQualityPolicy(),
        )

        run = await repository.get_agent_run(submission.run.id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "EVIDENCE_SCOPE_VIOLATION"
        events = await repository.list_owned_agent_run_events(run.id, run.user_id)
        assert events is not None
        assert not [item for item in events if item.event == "message_delta"]

    asyncio.run(scenario())


def test_stale_worker_cancellation_cannot_cancel_new_lease_owner() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        _chat_session, submission, old_token = await _submitted_run(repository)
        entered = asyncio.Event()

        class SlowGraph:
            async def ainvoke(self, _initial: dict, _config: dict) -> dict:
                entered.set()
                await asyncio.Event().wait()
                return _valid_result()

        old_execution = asyncio.create_task(
            execute_agent_run(
                repository,
                SlowGraph(),
                submission.run.id,
                old_token,
                answer_quality_policy=AnswerQualityPolicy(),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        job = next(
            item for item in repository.jobs.values() if item.agent_run_id == submission.run.id
        )
        new_token = "new-worker-token"
        job.claim_token = new_token
        old_execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old_execution

        run = await repository.get_agent_run(submission.run.id)
        assert run is not None
        assert run.status == "running"
        assert run.cancel_requested is False

        await execute_agent_run(
            repository,
            ResultGraph(),
            submission.run.id,
            new_token,
            answer_quality_policy=AnswerQualityPolicy(),
        )
        run = await repository.get_agent_run(submission.run.id)
        assert run is not None and run.status == "completed"

    asyncio.run(scenario())


def test_cancelling_interrupted_run_appends_a_new_terminal_event() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        _chat_session, submission, claim_token = await _submitted_run(repository)
        await repository.start_agent_run(submission.run.id, claim_token)
        interrupted = await repository.finish_agent_run(
            submission.run.id,
            status="interrupted",
            result_summary={"answer": "", "citations": []},
            pending_action={"action_id": "approve-1"},
            claim_token=claim_token,
        )
        assert interrupted is not None
        before = await repository.list_owned_agent_run_events(submission.run.id, "u1")
        assert before is not None
        interrupt_sequence = before[-1].sequence
        assert before[-1].event == "interrupt"

        cancelled = await repository.cancel_owned_agent_run(submission.run.id, "u1")
        assert cancelled is not None and cancelled.status == "cancelled"
        after = await repository.list_owned_agent_run_events(
            submission.run.id, "u1", interrupt_sequence
        )
        assert after is not None
        assert len(after) == 1
        assert after[0].event == "run_finished"
        assert after[0].data["status"] == "cancelled"

    asyncio.run(scenario())
