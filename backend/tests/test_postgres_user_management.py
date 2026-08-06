"""真实 PostgreSQL 用户管理集成测试。

默认跳过。将 ``PAPERLEAF_TEST_DATABASE_URL`` 指向已经执行 Alembic 升级的独立
测试数据库后运行本文件；为避免误删数据，数据库名必须包含 ``test``。
"""

import asyncio
import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from paperleaf_api import db
from paperleaf_api.config import settings
from paperleaf_api.main import create_app
from paperleaf_api.models import User, UserRole
from paperleaf_api.repository import LastAdminProtectionError, SQLAlchemyRepository
from paperleaf_api.storage import LocalObjectStorage

TEST_DATABASE_URL = os.getenv("PAPERLEAF_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要设置 PAPERLEAF_TEST_DATABASE_URL 才运行真实 PostgreSQL 测试",
)


def _test_database_url() -> str:
    assert TEST_DATABASE_URL
    url = make_url(TEST_DATABASE_URL)
    if "test" not in (url.database or "").casefold():
        pytest.fail("PAPERLEAF_TEST_DATABASE_URL 的数据库名必须包含 test")
    if url.drivername != "postgresql+asyncpg":
        pytest.fail("PAPERLEAF_TEST_DATABASE_URL 必须使用 postgresql+asyncpg")
    return TEST_DATABASE_URL


async def _truncate_users(engine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))


def test_postgres_concurrent_admin_removal_and_password_refresh() -> None:
    async def scenario() -> None:
        engine = create_async_engine(_test_database_url(), poolclass=NullPool)
        previous_engine, previous_factory = db._engine, db._session_factory
        db._engine = engine
        db._session_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        try:
            await _truncate_users(engine)
            repository = SQLAlchemyRepository("postgres-test-session-secret")
            first = await repository.create_user(
                "first-admin@example.com",
                "first-admin-password-123",
                UserRole.admin,
                must_change_password=False,
            )
            second = await repository.create_user(
                "second-admin@example.com",
                "second-admin-password-123",
                UserRole.admin,
                must_change_password=False,
            )

            results = await asyncio.gather(
                repository.update_managed_user(first.id, second.id, role=UserRole.user),
                repository.update_managed_user(second.id, first.id, active=False),
                return_exceptions=True,
            )
            assert sum(isinstance(item, User) for item in results) == 1
            assert sum(isinstance(item, LastAdminProtectionError) for item in results) == 1
            assert await repository.count_active_admins() == 1

            reader = await repository.create_user(
                "reader@example.com",
                "reader-password-123",
                UserRole.user,
            )
            assert reader.must_change_password is True
            refreshed = await repository.set_password(reader.id, "reader-new-password-456")
            assert refreshed.must_change_password is False
            assert await repository.authenticate(
                "reader@example.com", "reader-new-password-456"
            )
        finally:
            await _truncate_users(engine)
            await engine.dispose()
            db._engine, db._session_factory = previous_engine, previous_factory

    asyncio.run(scenario())


def test_postgres_change_password_api_returns_refreshed_user(tmp_path) -> None:
    database_url = _test_database_url()
    engine = create_async_engine(database_url, poolclass=NullPool)
    previous_engine, previous_factory = db._engine, db._session_factory
    db._engine = engine
    db._session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    config = replace(
        settings,
        mode="test",
        database_url=database_url,
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
    )
    try:
        asyncio.run(_truncate_users(engine))
        app = create_app(
            config,
            repository=SQLAlchemyRepository(config.session_secret),
            storage=LocalObjectStorage(tmp_path),
        )
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"email": "admin@example.com", "password": "admin-password-123"},
            )
            assert login.status_code == 200
            csrf = client.cookies.get(config.csrf_cookie)
            assert csrf
            created = client.post(
                "/api/v1/admin/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "email": "reader@example.com",
                    "temporary_password": "reader-password-123",
                    "role": "user",
                },
            )
            assert created.status_code == 201
            client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

            reader_login = client.post(
                "/api/v1/auth/login",
                json={"email": "reader@example.com", "password": "reader-password-123"},
            )
            assert reader_login.status_code == 200
            reader_csrf = client.cookies.get(config.csrf_cookie)
            assert reader_csrf
            changed = client.post(
                "/api/v1/auth/change-password",
                headers={"X-CSRF-Token": reader_csrf},
                json={
                    "current_password": "reader-password-123",
                    "new_password": "reader-new-password-456",
                },
            )
            assert changed.status_code == 200
            assert changed.json()["must_change_password"] is False
    finally:
        asyncio.run(_truncate_users(engine))
        asyncio.run(engine.dispose())
        db._engine, db._session_factory = previous_engine, previous_factory
