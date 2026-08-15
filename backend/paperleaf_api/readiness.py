"""面向编排器的依赖就绪探测。

`/health` 只证明 API 进程能够响应；本模块负责 `/ready` 的外部依赖与
Worker 消费能力检查。返回值只包含低敏状态码，不暴露连接串、Bucket 名或异常正文。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

from .config import Settings
from .runtime_store import RuntimeStore
from .storage import ObjectStorage

WORKER_HEARTBEAT_NAMESPACE = "worker-readiness"
WORKER_HEARTBEAT_KEY = "primary"


def expected_alembic_heads() -> tuple[str, ...]:
    backend_root = Path(__file__).resolve().parent.parent
    config = AlembicConfig(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    return tuple(sorted(ScriptDirectory.from_config(config).get_heads()))


async def database_components(config: Settings) -> dict[str, dict[str, Any]]:
    engine = create_async_engine(config.database_url, pool_pre_ping=True, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            current = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:
        return {
            "postgresql": {"status": "degraded", "code": "database_unavailable"},
            "alembic": {"status": "degraded", "code": "migration_state_unknown"},
        }
    finally:
        await engine.dispose()

    expected = expected_alembic_heads()
    migration_ready = bool(current) and str(current) in expected
    return {
        "postgresql": {"status": "ready"},
        "alembic": {
            "status": "ready" if migration_ready else "degraded",
            "code": "at_head" if migration_ready else "migration_not_at_head",
            "current": str(current or "missing"),
            "expected": list(expected),
        },
    }


async def worker_component(
    runtime_store: RuntimeStore, *, ttl_seconds: int, now: float | None = None
) -> dict[str, Any]:
    heartbeat = await runtime_store.get_cached_json(
        WORKER_HEARTBEAT_NAMESPACE, WORKER_HEARTBEAT_KEY
    )
    observed_at = heartbeat.get("observed_at") if heartbeat else None
    queue_poll_ok = heartbeat.get("queue_poll_ok") is True if heartbeat else False
    try:
        age = max(0.0, (now if now is not None else time.time()) - float(observed_at))
    except (TypeError, ValueError):
        age = float("inf")
    ready = queue_poll_ok and age <= ttl_seconds
    return {
        "status": "ready" if ready else "degraded",
        "code": "queue_poll_recent" if ready else "worker_heartbeat_stale_or_missing",
        "heartbeat_age_seconds": round(age, 3) if age != float("inf") else None,
    }


async def readiness_report(
    config: Settings,
    storage: ObjectStorage,
    runtime_store: RuntimeStore,
) -> tuple[int, dict[str, Any]]:
    if config.is_demo:
        storage_state = await storage.check_ready()
        components = {
            "postgresql": {"status": "not_applicable"},
            "alembic": {"status": "not_applicable"},
            "object_storage": storage_state,
            "redis": {"status": "not_applicable", "backend": runtime_store.backend},
            "worker": {"status": "not_applicable"},
        }
        return 200, {"status": "ready", "agent_ready": True, "components": components}

    database_task = asyncio.create_task(database_components(config))
    storage_task = asyncio.create_task(storage.check_ready())
    redis_task = asyncio.create_task(runtime_store.ping())
    try:
        worker_task = asyncio.create_task(
            worker_component(
                runtime_store,
                ttl_seconds=config.worker_heartbeat_ttl_seconds,
            )
        )
        database_state, storage_state, redis_ready, worker_state = await asyncio.gather(
            database_task, storage_task, redis_task, worker_task
        )
    except Exception:
        for task in (database_task, storage_task, redis_task):
            if not task.done():
                task.cancel()
        return 503, {
            "status": "degraded",
            "agent_ready": False,
            "components": {"readiness": {"status": "degraded", "code": "probe_failed"}},
        }

    components = {
        **database_state,
        "object_storage": storage_state,
        "redis": {
            "status": "ready" if redis_ready else "degraded",
            "backend": runtime_store.backend,
        },
        "worker": worker_state,
    }
    ready = all(item.get("status") == "ready" for item in components.values())
    return (
        200 if ready else 503,
        {
            "status": "ready" if ready else "degraded",
            "agent_ready": ready,
            "components": components,
        },
    )
