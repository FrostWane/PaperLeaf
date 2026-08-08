"""Redis 驱动的短期运行态存储。

Redis 只保存可以丢失或重新计算的限流计数与幂等判定。用户、会话、任务、
Agent Run 和消息仍以 PostgreSQL 为唯一真相源。Redis 不可用时，调用方可以
明确选择降级策略，不能把连接错误伪装成业务限流。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings

logger = logging.getLogger("paperleaf.runtime_store")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    used: int
    retry_after_seconds: int
    backend: str
    degraded: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


class RuntimeStoreUnavailable(RuntimeError):
    """Redis 操作超时或连接失败。"""


class RuntimeStore(Protocol):
    backend: str

    async def ping(self) -> bool: ...

    async def stats(self) -> dict[str, int | str | None]: ...

    async def acquire_rate_limit(
        self,
        namespace: str,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
        idempotency_key: str,
    ) -> RateLimitDecision: ...

    async def get_cached_json(self, namespace: str, key: str) -> dict[str, Any] | None: ...

    async def set_cached_json(
        self, namespace: str, key: str, value: dict[str, Any], *, ttl_seconds: int
    ) -> None: ...

    async def close(self) -> None: ...


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MemoryRuntimeStore:
    """测试与离线 Demo 使用的单进程实现。"""

    backend = "memory"

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._windows: dict[str, tuple[int, float]] = {}
        self._decisions: dict[str, tuple[bool, float]] = {}
        self._json_cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._lock = asyncio.Lock()

    async def ping(self) -> bool:
        return True

    async def stats(self) -> dict[str, int | str | None]:
        return {
            "backend": self.backend,
            "used_memory_bytes": None,
            "max_memory_bytes": None,
            "key_count": len(self._windows) + len(self._decisions),
            "connected_clients": None,
        }

    async def acquire_rate_limit(
        self,
        namespace: str,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
        idempotency_key: str,
    ) -> RateLimitDecision:
        now = float(self._clock())
        subject_key = f"{namespace}:{_digest(subject)}"
        decision_key = f"{subject_key}:{_digest(idempotency_key)}"
        async with self._lock:
            stored = self._decisions.get(decision_key)
            if stored and stored[1] > now:
                used, expires_at = self._windows.get(subject_key, (0, now + window_seconds))
                return RateLimitDecision(
                    stored[0],
                    limit,
                    used,
                    max(1, math.ceil(expires_at - now)),
                    self.backend,
                )

            used, expires_at = self._windows.get(subject_key, (0, now + window_seconds))
            if expires_at <= now:
                used = 0
                expires_at = now + window_seconds
            used += 1
            allowed = used <= limit
            self._windows[subject_key] = (used, expires_at)
            self._decisions[decision_key] = (allowed, expires_at)
            return RateLimitDecision(
                allowed,
                limit,
                used,
                max(1, math.ceil(expires_at - now)),
                self.backend,
            )

    async def get_cached_json(self, namespace: str, key: str) -> dict[str, Any] | None:
        cache_key = f"{namespace}:{_digest(key)}"
        async with self._lock:
            stored = self._json_cache.get(cache_key)
            if not stored:
                return None
            if stored[1] <= self._clock():
                self._json_cache.pop(cache_key, None)
                return None
            return dict(stored[0])

    async def set_cached_json(
        self, namespace: str, key: str, value: dict[str, Any], *, ttl_seconds: int
    ) -> None:
        cache_key = f"{namespace}:{_digest(key)}"
        async with self._lock:
            self._json_cache[cache_key] = (dict(value), self._clock() + ttl_seconds)

    async def close(self) -> None:
        return None


_ACQUIRE_RATE_LIMIT_SCRIPT = """
local previous = redis.call('GET', KEYS[2])
if previous then
    local used = tonumber(redis.call('GET', KEYS[1]) or '0')
    local ttl = redis.call('TTL', KEYS[1])
    if ttl < 1 then ttl = tonumber(ARGV[2]) end
    return {tonumber(previous), used, ttl}
end

local used = redis.call('INCR', KEYS[1])
if used == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
local allowed = 0
if used <= tonumber(ARGV[1]) then allowed = 1 end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then ttl = tonumber(ARGV[2]) end
redis.call('SET', KEYS[2], allowed, 'EX', ttl)
return {allowed, used, ttl}
"""


class RedisRuntimeStore:
    """使用 Lua 原子维护固定窗口计数和幂等判定。"""

    backend = "redis"

    def __init__(self, client: Any, *, key_prefix: str, timeout_seconds: float) -> None:
        self._client = client
        self._key_prefix = key_prefix.strip(":")
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_url(cls, config: Settings) -> RedisRuntimeStore:
        if not config.redis_url:
            raise ValueError("未配置 Redis URL")
        from redis.asyncio import Redis

        client = Redis.from_url(
            config.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=config.redis_timeout_seconds,
            socket_timeout=config.redis_timeout_seconds,
            health_check_interval=30,
        )
        return cls(
            client,
            key_prefix=config.redis_key_prefix,
            timeout_seconds=config.redis_timeout_seconds,
        )

    async def _execute(self, operation: Any) -> Any:
        try:
            return await asyncio.wait_for(operation, timeout=self._timeout_seconds)
        except Exception as exc:
            raise RuntimeStoreUnavailable("Redis 运行态存储暂时不可用") from exc

    async def ping(self) -> bool:
        return bool(await self._execute(self._client.ping()))

    async def stats(self) -> dict[str, int | str | None]:
        memory, clients, key_count = await self._execute(
            asyncio.gather(
                self._client.info("memory"),
                self._client.info("clients"),
                self._client.dbsize(),
            )
        )
        return {
            "backend": self.backend,
            "used_memory_bytes": int(memory.get("used_memory", 0)),
            "max_memory_bytes": int(memory.get("maxmemory", 0)) or None,
            "key_count": int(key_count),
            "connected_clients": int(clients.get("connected_clients", 0)),
        }

    async def acquire_rate_limit(
        self,
        namespace: str,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
        idempotency_key: str,
    ) -> RateLimitDecision:
        subject_hash = _digest(subject)
        idempotency_hash = _digest(idempotency_key)
        counter_key = f"{self._key_prefix}:rate:{namespace}:{subject_hash}"
        decision_key = (
            f"{self._key_prefix}:rate-decision:{namespace}:"
            f"{subject_hash}:{idempotency_hash}"
        )
        result = await self._execute(
            self._client.eval(
                _ACQUIRE_RATE_LIMIT_SCRIPT,
                2,
                counter_key,
                decision_key,
                limit,
                window_seconds,
            )
        )
        allowed, used, retry_after = (int(value) for value in result)
        return RateLimitDecision(
            bool(allowed),
            limit,
            used,
            max(1, retry_after),
            self.backend,
        )

    async def get_cached_json(self, namespace: str, key: str) -> dict[str, Any] | None:
        cache_key = f"{self._key_prefix}:cache:{namespace}:{_digest(key)}"
        raw = await self._execute(self._client.get(cache_key))
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    async def set_cached_json(
        self, namespace: str, key: str, value: dict[str, Any], *, ttl_seconds: int
    ) -> None:
        cache_key = f"{self._key_prefix}:cache:{namespace}:{_digest(key)}"
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        await self._execute(self._client.set(cache_key, payload, ex=ttl_seconds))

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close:
            await close()


class ResilientRuntimeStore:
    """Redis 故障时退回进程内限流，保证模型入口仍有本机保护。"""

    backend = "redis"

    def __init__(
        self,
        primary: RedisRuntimeStore,
        fallback: MemoryRuntimeStore | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or MemoryRuntimeStore()

    async def ping(self) -> bool:
        try:
            return await self._primary.ping()
        except RuntimeStoreUnavailable:
            return False

    async def stats(self) -> dict[str, int | str | None]:
        try:
            return await self._primary.stats()
        except RuntimeStoreUnavailable:
            stats = await self._fallback.stats()
            return {**stats, "backend": "memory-fallback"}

    async def acquire_rate_limit(
        self,
        namespace: str,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
        idempotency_key: str,
    ) -> RateLimitDecision:
        try:
            return await self._primary.acquire_rate_limit(
                namespace,
                subject,
                limit=limit,
                window_seconds=window_seconds,
                idempotency_key=idempotency_key,
            )
        except RuntimeStoreUnavailable:
            logger.warning("Redis 不可用，Agent 限流已降级为当前 API 进程内计数")
            decision = await self._fallback.acquire_rate_limit(
                namespace,
                subject,
                limit=limit,
                window_seconds=window_seconds,
                idempotency_key=idempotency_key,
            )
            return RateLimitDecision(
                allowed=decision.allowed,
                limit=decision.limit,
                used=decision.used,
                retry_after_seconds=decision.retry_after_seconds,
                backend="memory-fallback",
                degraded=True,
            )

    async def get_cached_json(self, namespace: str, key: str) -> dict[str, Any] | None:
        try:
            return await self._primary.get_cached_json(namespace, key)
        except RuntimeStoreUnavailable:
            return await self._fallback.get_cached_json(namespace, key)

    async def set_cached_json(
        self, namespace: str, key: str, value: dict[str, Any], *, ttl_seconds: int
    ) -> None:
        try:
            await self._primary.set_cached_json(
                namespace, key, value, ttl_seconds=ttl_seconds
            )
        except RuntimeStoreUnavailable:
            await self._fallback.set_cached_json(
                namespace, key, value, ttl_seconds=ttl_seconds
            )

    async def close(self) -> None:
        await self._primary.close()
        await self._fallback.close()


def create_runtime_store(config: Settings) -> RuntimeStore:
    if config.redis_url:
        return ResilientRuntimeStore(RedisRuntimeStore.from_url(config))
    return MemoryRuntimeStore()
