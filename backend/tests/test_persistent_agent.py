import asyncio

import pytest

from paperleaf_api.agent_execution import execute_agent_run
from paperleaf_api.rag.answer_quality import AnswerQualityPolicy
from paperleaf_api.rag.citations import CitationClaim, Evidence
from paperleaf_api.repository import (
    ChatActiveRunError,
    ChatIdempotencyConflictError,
    MemoryRepository,
)


def _valid_result() -> dict:
    evidence = [
        Evidence("c1", "p1", "论文一", 2, "方法使用页级混合检索。"),
        Evidence("c2", "p1", "论文一", 5, "实验显示引用页准确率提高。"),
    ]
    return {
        "status": "completed",
        "answer": (
            "方法使用页级混合检索 [chunk:c1]。\n\n"
            "- 实验显示引用页准确率提高 [chunk:c2]。"
        ),
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


async def _submitted_run(repository: MemoryRepository, user_id: str = "u1"):
    chat_session = await repository.create_chat_session(
        user_id, "新会话", "library", None, None
    )
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


def test_delete_rechecks_active_run_after_route_precheck() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        chat_session = await repository.create_chat_session(
            "u1", "删除竞态", "library", None, None
        )
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
        assert any(
            item.agent_run_id == submission.run.id
            for item in repository.jobs.values()
        )

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
        assert any(
            item.event_key == "stage:generate:start" for item in events
        )
        assert any(
            item.event_key == "stage:generate:finish" for item in events
        )
        assert all(item.data.get("citations") for item in events if item.event == "message_delta")

    asyncio.run(scenario())


def test_one_invalid_paragraph_prevents_every_draft_write() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        _chat_session, submission, claim_token = await _submitted_run(repository)
        result = _valid_result()
        result["answer"] = (
            "方法使用页级混合检索 [chunk:c1]。\n\n"
            "第二段包含没有引用的事实。"
        )

        await execute_agent_run(
            repository,
            ResultGraph(result),
            submission.run.id,
            claim_token,
            answer_quality_policy=AnswerQualityPolicy(),
        )

        run = await repository.get_agent_run(submission.run.id)
        assert run is not None and run.status == "failed"
        messages = await repository.list_chat_messages(run.session_id, run.user_id)
        assert messages is not None
        assistant = next(item for item in messages if item.role == "assistant")
        assert assistant.content == ""
        events = await repository.list_owned_agent_run_events(run.id, run.user_id)
        assert events is not None
        assert not [item for item in events if item.event == "message_delta"]

    asyncio.run(scenario())


def test_out_of_scope_evidence_is_never_published() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        _chat_session, submission, claim_token = await _submitted_run(repository)
        result = _valid_result()
        result["retrieved_evidence"] = [
            Evidence("foreign", "paper-of-u2", "他人论文", 1, "越权证据")
        ]
        result["citations"] = [
            CitationClaim("foreign", "paper-of-u2", 1, "越权证据")
        ]
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
            item
            for item in repository.jobs.values()
            if item.agent_run_id == submission.run.id
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
        before = await repository.list_owned_agent_run_events(
            submission.run.id, "u1"
        )
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
