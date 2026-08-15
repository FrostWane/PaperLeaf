import asyncio
from dataclasses import replace

import pytest

from paperleaf_api.config import settings
from paperleaf_api.readiness import readiness_report, worker_component
from paperleaf_api.runtime_store import MemoryRuntimeStore
from paperleaf_api.storage import LocalObjectStorage


def test_worker_readiness_requires_recent_successful_queue_poll() -> None:
    async def scenario() -> None:
        store = MemoryRuntimeStore()
        missing = await worker_component(store, ttl_seconds=20, now=100)
        assert missing["status"] == "degraded"

        await store.set_cached_json(
            "worker-readiness",
            "primary",
            {"observed_at": 90.0, "queue_poll_ok": True},
            ttl_seconds=30,
        )
        recent = await worker_component(store, ttl_seconds=20, now=100)
        assert recent["status"] == "ready"
        stale = await worker_component(store, ttl_seconds=5, now=100)
        assert stale["status"] == "degraded"

    asyncio.run(scenario())


def test_demo_readiness_is_explicitly_not_applicable(tmp_path) -> None:
    async def scenario() -> None:
        config = replace(settings, mode="test", local_storage_path=tmp_path)
        status_code, payload = await readiness_report(
            config, LocalObjectStorage(tmp_path), MemoryRuntimeStore()
        )
        assert status_code == 200
        assert payload["status"] == "ready"
        assert payload["components"]["postgresql"]["status"] == "not_applicable"

    asyncio.run(scenario())


def test_production_rejects_insecure_cookie_even_with_strong_secrets() -> None:
    config = replace(
        settings,
        mode="production",
        secure_cookies=False,
        session_secret="s" * 64,
        bootstrap_admin_password="A-strong-admin-password-123",
        minio_secret_key="M-strong-storage-password-123",
    )
    with pytest.raises(RuntimeError, match="Secure Cookie"):
        config.validate_production()
