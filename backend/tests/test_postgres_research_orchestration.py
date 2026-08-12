"""有界研究编排的真实 PostgreSQL 迁移与持久化契约（默认跳过）。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from alembic.config import Config
from paperleaf_api import db
from paperleaf_api.models import UserRole
from paperleaf_api.repository import SQLAlchemyRepository

TEST_DATABASE_URL = os.getenv("PAPERLEAF_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要设置 PAPERLEAF_TEST_DATABASE_URL，且数据库名必须包含 test",
)


def _database_url() -> str:
    assert TEST_DATABASE_URL
    url = make_url(TEST_DATABASE_URL)
    if "test" not in (url.database or "").casefold():
        pytest.fail("PAPERLEAF_TEST_DATABASE_URL 的数据库名必须包含 test")
    if url.drivername != "postgresql+asyncpg":
        pytest.fail("PAPERLEAF_TEST_DATABASE_URL 必须使用 postgresql+asyncpg")
    return TEST_DATABASE_URL


def _alembic_config() -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    return Config(str(backend_dir / "alembic.ini"))


async def _reset_public_schema() -> None:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("DROP SCHEMA public CASCADE"))
            await connection.execute(sa.text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


async def _schema_snapshot() -> dict[str, object]:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: {
                    "revision": sync_connection.execute(
                        sa.text("SELECT version_num FROM alembic_version")
                    ).scalar_one(),
                    "columns": {
                        item["name"]: item
                        for item in sa.inspect(sync_connection).get_columns("agent_runs")
                    },
                }
            )
    finally:
        await engine.dispose()


def test_postgres_0019_fresh_downgrade_and_reupgrade() -> None:
    asyncio.run(_reset_public_schema())
    config = _alembic_config()
    try:
        command.upgrade(config, "head")
        upgraded = asyncio.run(_schema_snapshot())
        assert upgraded["revision"] == "20260812_0019"
        column = upgraded["columns"]["orchestration_version"]  # type: ignore[index]
        assert column["nullable"] is False
        assert "single_agent_v1" in str(column["default"])

        command.downgrade(config, "20260810_0018")
        downgraded = asyncio.run(_schema_snapshot())
        assert downgraded["revision"] == "20260810_0018"
        assert "orchestration_version" not in downgraded["columns"]

        command.upgrade(config, "head")
        reupgraded = asyncio.run(_schema_snapshot())
        assert reupgraded["revision"] == "20260812_0019"
        assert "orchestration_version" in reupgraded["columns"]
    finally:
        command.upgrade(config, "head")


def test_postgres_orchestration_version_round_trips() -> None:
    async def scenario() -> None:
        engine = create_async_engine(_database_url(), poolclass=NullPool)
        previous_engine, previous_factory = db._engine, db._session_factory
        db._engine = engine
        db._session_factory = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        try:
            async with engine.begin() as connection:
                await connection.execute(sa.text("TRUNCATE TABLE users CASCADE"))
            repository = SQLAlchemyRepository("research-orchestration-test")
            user = await repository.create_user(
                "multi-agent-test@example.com",
                "test-password-123",
                role=UserRole.user,
                must_change_password=False,
            )
            session = await repository.create_chat_session(
                user.id, "并行比较", "library", None, None
            )
            submission = await repository.submit_chat_message(
                session.id,
                user.id,
                "比较三篇论文",
                "postgres-compare-message",
                "postgres-compare-hash",
                {
                    "type": "collection",
                    "paper_ids": ["p1", "p2", "p3"],
                    "orchestration_version": "compare_map_reduce_v2",
                },
            )
            assert submission is not None
            assert submission.run.orchestration_version == "compare_map_reduce_v2"
            loaded = await repository.get_agent_run(submission.run.id)
            owned = await repository.get_owned_agent_run(submission.run.id, user.id)
            assert loaded is not None and loaded.orchestration_version == "compare_map_reduce_v2"
            assert owned is not None and owned.orchestration_version == "compare_map_reduce_v2"
            async with engine.connect() as connection:
                stored = await connection.scalar(
                    sa.text("SELECT orchestration_version FROM agent_runs WHERE id=:run_id"),
                    {"run_id": submission.run.id},
                )
            assert stored == "compare_map_reduce_v2"
        finally:
            db._engine, db._session_factory = previous_engine, previous_factory
            await engine.dispose()

    asyncio.run(scenario())
