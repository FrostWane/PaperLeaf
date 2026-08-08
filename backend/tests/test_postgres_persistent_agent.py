"""真实 PostgreSQL 的持久会话、事件原子性与 0007 结构测试（默认跳过）。"""

import asyncio
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from paperleaf_api import db
from paperleaf_api.models import (
    AgentRun,
    AgentToolArtifact,
    AgentToolCall,
    ChatMessage,
    Job,
    PaperStatus,
    UserRole,
)
from paperleaf_api.repository import (
    AgentToolArtifactRecord,
    AgentToolCallRecord,
    ChatActiveRunError,
    ChatIdempotencyConflictError,
    PaperRecord,
    SQLAlchemyRepository,
)

TEST_DATABASE_URL = os.getenv("PAPERLEAF_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要设置 PAPERLEAF_TEST_DATABASE_URL 并先执行 Alembic upgrade head",
)


def _test_database_url() -> str:
    assert TEST_DATABASE_URL
    url = make_url(TEST_DATABASE_URL)
    if "test" not in (url.database or "").casefold():
        pytest.fail("PAPERLEAF_TEST_DATABASE_URL 的数据库名必须包含 test")
    if url.drivername != "postgresql+asyncpg":
        pytest.fail("PAPERLEAF_TEST_DATABASE_URL 必须使用 postgresql+asyncpg")
    return TEST_DATABASE_URL


def test_postgres_0007_schema_and_persistent_event_contract() -> None:
    async def scenario() -> None:
        engine = create_async_engine(_test_database_url(), poolclass=NullPool)
        previous_engine, previous_factory = db._engine, db._session_factory
        db._engine = engine
        db._session_factory = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        try:
            async with engine.begin() as connection:
                await connection.execute(sa.text("TRUNCATE TABLE users CASCADE"))
                schema = await connection.run_sync(
                    lambda sync_connection: {
                        "tables": set(sa.inspect(sync_connection).get_table_names()),
                        "agent_columns": {
                            item["name"]
                            for item in sa.inspect(sync_connection).get_columns(
                                "agent_runs"
                            )
                        },
                        "job_columns": {
                            item["name"]
                            for item in sa.inspect(sync_connection).get_columns("jobs")
                        },
                        "agent_fks": {
                            item["name"]
                            for item in sa.inspect(sync_connection).get_foreign_keys(
                                "agent_runs"
                            )
                        },
                    }
                )
            assert {
                "chat_sessions",
                "chat_messages",
                "agent_run_events",
                "paper_artifacts",
                "agent_tool_calls",
                "agent_tool_artifacts",
            } <= schema["tables"]
            assert {
                "cancel_requested",
                "scope_snapshot",
                "user_message_id",
                "assistant_message_id",
            } <= schema["agent_columns"]
            assert "agent_run_id" in schema["job_columns"]
            assert {
                "fk_agent_runs_session_id_chat_sessions",
                "fk_agent_runs_user_message_id_chat_messages",
                "fk_agent_runs_assistant_message_id_chat_messages",
            } <= schema["agent_fks"]

            repository = SQLAlchemyRepository("persistent-agent-test")
            user = await repository.create_user(
                "reader@example.com",
                "reader-password-123",
                UserRole.user,
                must_change_password=False,
            )
            paper = await repository.create_paper(
                PaperRecord(
                    id="artifact-paper",
                    owner_id=user.id,
                    title="Artifact paper",
                    authors=[],
                    year=None,
                    abstract=None,
                    doi=None,
                    arxiv_id=None,
                    filename="artifact.pdf",
                    storage_key=f"{user.id}/artifact.pdf",
                    mime_type="application/pdf",
                    size_bytes=100,
                    sha256="d" * 64,
                    page_count=1,
                    status=PaperStatus.ready,
                )
            )
            artifact = await repository.upsert_paper_artifact(
                paper.id,
                user.id,
                "summary",
                "e" * 64,
                "ready",
                None,
                {"sections": []},
                "## Summary",
            )
            assert artifact is not None and artifact.status == "ready"
            await repository.mark_paper_artifacts_stale(paper.id)
            stale_artifact = await repository.get_owned_paper_artifact(
                paper.id, user.id, "summary"
            )
            assert stale_artifact is not None and stale_artifact.status == "stale"
            chat_session = await repository.create_chat_session(
                user.id, "持久问答", "library", None, None
            )
            submission = await repository.submit_chat_message(
                chat_session.id,
                user.id,
                "比较方法和实验",
                "postgres-client-1",
                "postgres-hash-1",
                {"type": "library", "paper_ids": ["p1"]},
            )
            assert submission is not None
            replay = await repository.submit_chat_message(
                chat_session.id,
                user.id,
                "比较方法和实验",
                "postgres-client-1",
                "postgres-hash-1",
                {"type": "library", "paper_ids": ["p1"]},
            )
            assert replay is not None and replay.replayed is True

            claim_token = await repository.claim_agent_run_job(submission.run.id)
            assert claim_token is not None
            assert await repository.start_agent_run(
                submission.run.id, claim_token
            ) is not None
            tool_call = await repository.start_agent_tool_call(
                AgentToolCallRecord(
                    id="tool-record-1",
                    call_id="model-call-1",
                    run_id=submission.run.id,
                    user_id=user.id,
                    skill_name="paper_qa",
                    tool_name="search_library",
                    arguments={"query": "方法"},
                ),
                claim_token,
            )
            assert tool_call is not None
            tool_artifact = await repository.create_agent_tool_artifact(
                AgentToolArtifactRecord(
                    id="tool-artifact-1",
                    tool_call_id=tool_call.id,
                    user_id=user.id,
                    content={"items": ["large-result"]},
                    token_count=9000,
                ),
                claim_token,
            )
            assert tool_artifact is not None
            finished_tool = await repository.finish_agent_tool_call(
                tool_call.id,
                submission.run.id,
                claim_token,
                status="succeeded",
                attempt=2,
                duration_ms=42,
                result_preview={"count": 1},
                error_code=None,
            )
            assert finished_tool is not None and finished_tool.attempt == 2
            first = await repository.publish_agent_paragraph(
                submission.run.id,
                0,
                "第一段 [chunk:c1]。",
                [{"chunk_id": "c1", "paper_id": "p1", "physical_page": 1}],
                "grounded_fact",
                claim_token,
            )
            duplicate = await repository.publish_agent_paragraph(
                submission.run.id,
                0,
                "不应重复写入",
                [{"chunk_id": "c1", "paper_id": "p1", "physical_page": 1}],
                "grounded_fact",
                claim_token,
            )
            second = await repository.publish_agent_paragraph(
                submission.run.id,
                1,
                "- 第二段 [chunk:c2]。",
                [{"chunk_id": "c2", "paper_id": "p1", "physical_page": 2}],
                "grounded_fact",
                claim_token,
            )
            assert first is not None and duplicate is not None and second is not None
            assert duplicate.id == first.id
            completed = await repository.finish_agent_run(
                submission.run.id,
                status="completed",
                result_summary={"answer": "", "citations": []},
                claim_token=claim_token,
            )
            assert completed is not None and completed.status == "completed"
            messages = await repository.list_chat_messages(chat_session.id, user.id)
            events = await repository.list_owned_agent_run_events(
                submission.run.id, user.id
            )
            assert messages is not None and events is not None
            assistant = next(item for item in messages if item.role == "assistant")
            deltas = [
                item.data["delta"] for item in events if item.event == "message_delta"
            ]
            assert "".join(deltas) == assistant.content
            assert assistant.content == (
                "第一段 [chunk:c1]。\n\n- 第二段 [chunk:c2]。"
            )
            async with db.get_session_factory()() as session:
                assert await session.get(AgentToolCall, "tool-record-1") is not None
                assert await session.get(AgentToolArtifact, "tool-artifact-1") is not None

            concurrent_session = await repository.create_chat_session(
                user.id, "并发幂等", "library", None, None
            )

            async def submit_identical():
                return await repository.submit_chat_message(
                    concurrent_session.id,
                    user.id,
                    "同一个浏览器请求",
                    "concurrent-client-1",
                    "concurrent-hash-1",
                    {"type": "library", "paper_ids": ["p1"]},
                )

            identical = await asyncio.gather(
                *(submit_identical() for _ in range(10))
            )
            assert all(item is not None for item in identical)
            assert len({item.run.id for item in identical if item is not None}) == 1
            assert sum(bool(item and item.replayed) for item in identical) == 9
            async with db.get_session_factory()() as session:
                message_count = int(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(ChatMessage)
                        .where(ChatMessage.session_id == concurrent_session.id)
                    )
                    or 0
                )
                run_count = int(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(AgentRun)
                        .where(AgentRun.session_id == concurrent_session.id)
                    )
                    or 0
                )
                job_count = int(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(Job)
                        .join(AgentRun, AgentRun.id == Job.agent_run_id)
                        .where(AgentRun.session_id == concurrent_session.id)
                    )
                    or 0
                )
            assert (message_count, run_count, job_count) == (2, 1, 1)
            with pytest.raises(ChatActiveRunError):
                await repository.delete_owned_chat_session(
                    concurrent_session.id, user.id
                )
            assert await repository.get_owned_chat_session(
                concurrent_session.id, user.id
            )
            assert await repository.list_chat_messages(
                concurrent_session.id, user.id
            )

            conflict_session = await repository.create_chat_session(
                user.id, "并发冲突", "library", None, None
            )

            async def submit_conflicting(content: str, request_hash: str):
                return await repository.submit_chat_message(
                    conflict_session.id,
                    user.id,
                    content,
                    "same-client-different-hash",
                    request_hash,
                    {"type": "library", "paper_ids": ["p1"]},
                )

            conflicting = await asyncio.gather(
                submit_conflicting("请求 A", "hash-a"),
                submit_conflicting("请求 B", "hash-b"),
                return_exceptions=True,
            )
            assert sum(
                isinstance(item, ChatIdempotencyConflictError)
                for item in conflicting
            ) == 1
            successful = [
                item for item in conflicting if not isinstance(item, Exception)
            ]
            assert len(successful) == 1 and successful[0] is not None
        finally:
            db._engine, db._session_factory = previous_engine, previous_factory
            await engine.dispose()

    asyncio.run(scenario())
