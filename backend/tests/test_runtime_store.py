import asyncio
import os
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from paperleaf_api.config import settings
from paperleaf_api.main import create_app
from paperleaf_api.models import PaperStatus
from paperleaf_api.repository import MemoryRepository, PaperRecord
from paperleaf_api.runtime_store import (
    MemoryRuntimeStore,
    RedisRuntimeStore,
    ResilientRuntimeStore,
    create_runtime_store,
)
from paperleaf_api.storage import LocalObjectStorage


class FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.eval_calls: list[tuple] = []
        self.closed = False

    async def ping(self) -> bool:
        if self.fail:
            raise ConnectionError("redis unavailable")
        return True

    async def eval(self, *args):
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.eval_calls.append(args)
        return [1, 1, 60]

    async def info(self, section: str) -> dict[str, int]:
        if self.fail:
            raise ConnectionError("redis unavailable")
        if section == "memory":
            return {"used_memory": 2048, "maxmemory": 8192}
        return {"connected_clients": 3}

    async def dbsize(self) -> int:
        if self.fail:
            raise ConnectionError("redis unavailable")
        return 7

    async def aclose(self) -> None:
        self.closed = True


def test_memory_rate_limit_is_atomic_and_idempotent() -> None:
    now = [100.0]
    store = MemoryRuntimeStore(clock=lambda: now[0])

    first = asyncio.run(
        store.acquire_rate_limit(
            "agent-submit",
            "user-1",
            limit=2,
            window_seconds=60,
            idempotency_key="message-1",
        )
    )
    replay = asyncio.run(
        store.acquire_rate_limit(
            "agent-submit",
            "user-1",
            limit=2,
            window_seconds=60,
            idempotency_key="message-1",
        )
    )
    second = asyncio.run(
        store.acquire_rate_limit(
            "agent-submit",
            "user-1",
            limit=2,
            window_seconds=60,
            idempotency_key="message-2",
        )
    )
    blocked = asyncio.run(
        store.acquire_rate_limit(
            "agent-submit",
            "user-1",
            limit=2,
            window_seconds=60,
            idempotency_key="message-3",
        )
    )

    assert first.allowed is True
    assert replay.allowed is True
    assert replay.used == 1
    assert second.allowed is True
    assert second.used == 2
    assert blocked.allowed is False
    assert blocked.used == 3
    assert blocked.remaining == 0
    assert blocked.retry_after_seconds == 60

    now[0] += 61
    renewed = asyncio.run(
        store.acquire_rate_limit(
            "agent-submit",
            "user-1",
            limit=2,
            window_seconds=60,
            idempotency_key="message-4",
        )
    )
    assert renewed.allowed is True
    assert renewed.used == 1


def test_redis_rate_limit_hashes_identifiers_and_closes_client() -> None:
    client = FakeRedis()
    store = RedisRuntimeStore(client, key_prefix="paperleaf", timeout_seconds=0.5)

    decision = asyncio.run(
        store.acquire_rate_limit(
            "agent-submit",
            "private-user-id",
            limit=12,
            window_seconds=60,
            idempotency_key="private-message-id",
        )
    )

    assert decision.allowed is True
    assert decision.backend == "redis"
    call = client.eval_calls[0]
    assert "private-user-id" not in " ".join(str(item) for item in call)
    assert "private-message-id" not in " ".join(str(item) for item in call)
    assert asyncio.run(store.stats()) == {
        "backend": "redis",
        "used_memory_bytes": 2048,
        "max_memory_bytes": 8192,
        "key_count": 7,
        "connected_clients": 3,
    }
    asyncio.run(store.close())
    assert client.closed is True


def test_redis_failure_falls_back_to_process_local_limit() -> None:
    primary = RedisRuntimeStore(
        FakeRedis(fail=True),
        key_prefix="paperleaf",
        timeout_seconds=0.5,
    )
    store = ResilientRuntimeStore(primary)

    assert asyncio.run(store.ping()) is False
    first = asyncio.run(
        store.acquire_rate_limit(
            "agent-submit",
            "user-1",
            limit=1,
            window_seconds=60,
            idempotency_key="message-1",
        )
    )
    blocked = asyncio.run(
        store.acquire_rate_limit(
            "agent-submit",
            "user-1",
            limit=1,
            window_seconds=60,
            idempotency_key="message-2",
        )
    )

    assert first.allowed is True
    assert first.degraded is True
    assert first.backend == "memory-fallback"
    assert blocked.allowed is False


def test_runtime_config_defaults_are_safe_for_local_fallback() -> None:
    config = replace(settings, redis_url=None)
    config.validate_production()
    assert config.agent_rate_limit_requests >= 1
    assert config.agent_rate_limit_window_seconds >= 1


def test_agent_submission_returns_retry_after_when_rate_limited(tmp_path) -> None:
    config = replace(
        settings,
        mode="test",
        local_storage_path=tmp_path,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="admin-password-123",
        agent_rate_limit_requests=1,
        agent_rate_limit_window_seconds=60,
    )
    repository = MemoryRepository(config.session_secret)
    runtime_store = MemoryRuntimeStore()
    app = create_app(
        config,
        repository=repository,
        storage=LocalObjectStorage(tmp_path),
        runtime_store=runtime_store,
    )

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-123"},
        )
        assert login.status_code == 200
        user_id = login.json()["id"]
        csrf = client.cookies.get("paperleaf_csrf")
        assert csrf
        asyncio.run(
            repository.create_paper(
                PaperRecord(
                    id="rate-limit-paper",
                    owner_id=user_id,
                    title="限流测试论文",
                    authors=[],
                    year=None,
                    abstract=None,
                    doi=None,
                    arxiv_id=None,
                    filename="rate-limit.pdf",
                    storage_key=f"{user_id}/rate-limit.pdf",
                    mime_type="application/pdf",
                    size_bytes=100,
                    sha256="f" * 64,
                    page_count=1,
                    status=PaperStatus.ready,
                )
            )
        )
        session = client.post(
            "/api/v1/chat/sessions",
            headers={"X-CSRF-Token": csrf},
            json={"title": "限流测试", "type": "library"},
        )
        assert session.status_code == 201
        asyncio.run(
            runtime_store.acquire_rate_limit(
                "agent-submit",
                user_id,
                limit=1,
                window_seconds=60,
                idempotency_key="preloaded-message",
            )
        )

        blocked = client.post(
            f"/api/v1/chat/sessions/{session.json()['id']}/messages",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "new-message",
            },
            json={"content": "这篇论文讲了什么？"},
        )

    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    assert blocked.json()["detail"]["code"] == "AGENT_RATE_LIMITED"
    assert blocked.json()["detail"]["message"] == "提问过于频繁，请稍后再试"


def test_real_redis_atomic_idempotent_limit_when_configured() -> None:
    redis_url = os.getenv("PAPERLEAF_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("未配置隔离 Redis")
    namespace = f"integration-{uuid.uuid4().hex}"
    config = replace(
        settings,
        redis_url=redis_url,
        redis_key_prefix=f"paperleaf-test-{uuid.uuid4().hex}",
        redis_timeout_seconds=1,
    )

    async def scenario() -> None:
        store = create_runtime_store(config)
        try:
            assert await store.ping() is True
            first = await store.acquire_rate_limit(
                namespace,
                "user-1",
                limit=1,
                window_seconds=2,
                idempotency_key="message-1",
            )
            replay = await store.acquire_rate_limit(
                namespace,
                "user-1",
                limit=1,
                window_seconds=2,
                idempotency_key="message-1",
            )
            blocked = await store.acquire_rate_limit(
                namespace,
                "user-1",
                limit=1,
                window_seconds=2,
                idempotency_key="message-2",
            )
            assert (first.allowed, first.used) == (True, 1)
            assert (replay.allowed, replay.used) == (True, 1)
            assert blocked.allowed is False
            assert blocked.used == 2
            await asyncio.sleep(2.1)
            renewed = await store.acquire_rate_limit(
                namespace,
                "user-1",
                limit=1,
                window_seconds=2,
                idempotency_key="message-2",
            )
            assert (renewed.allowed, renewed.used) == (True, 1)
        finally:
            await store.close()

    asyncio.run(scenario())
