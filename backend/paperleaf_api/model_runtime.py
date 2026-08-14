"""OpenAI-compatible 模型运行时。

业务代码只声明调用目的和操作函数；本模块统一处理超时、受控重试、备用端点、
断路器和不含提示词/响应正文的公开尝试记录。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any, Generic, Literal, TypeVar

ModelPurpose = Literal[
    "answer",
    "query_rewrite",
    "answerability",
    "evidence_support",
    "research_scout",
    "summary",
    "translation",
    "embedding",
    "vision",
]
AttemptStatus = Literal[
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "circuit_open",
]

T = TypeVar("T")
@dataclass(frozen=True)
class ModelProvider:
    name: str
    api_key: str
    base_url: str
    chat_model: str
    embedding_model: str
    vision_model: str | None = None

    def model_for(self, purpose: ModelPurpose) -> str | None:
        if purpose == "embedding":
            return self.embedding_model
        if purpose == "vision":
            return self.vision_model
        return self.chat_model

    def supports(self, purpose: ModelPurpose) -> bool:
        return bool(self.api_key and self.model_for(purpose))


@dataclass(frozen=True)
class ModelAttempt:
    purpose: ModelPurpose
    provider: str
    model: str
    status: AttemptStatus
    duration_ms: int
    attempt: int
    fallback_used: bool
    error_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "attempt": self.attempt,
            "fallback_used": self.fallback_used,
            "error_code": self.error_code,
        }


_attempt_buffer: ContextVar[list[ModelAttempt] | None] = ContextVar(
    "paperleaf_model_attempt_buffer", default=None
)


@contextmanager
def collect_model_attempts() -> Iterator[list[ModelAttempt]]:
    """为单次 Agent/产物请求收集脱敏的模型尝试记录。"""

    attempts: list[ModelAttempt] = []
    token = _attempt_buffer.set(attempts)
    try:
        yield attempts
    finally:
        _attempt_buffer.reset(token)


def _record_attempt(attempt: ModelAttempt) -> None:
    buffer = _attempt_buffer.get()
    if buffer is not None:
        buffer.append(attempt)


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False


class ModelCircuitBreaker:
    """进程内轻量断路器；按 provider + purpose 隔离故障域。"""

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("断路器失败阈值必须至少为 1")
        if cooldown_seconds <= 0:
            raise ValueError("断路器冷却时间必须大于 0")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._states: dict[str, _CircuitState] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            state = self._states.setdefault(key, _CircuitState())
            if state.opened_at is None:
                return True
            if self._clock() - state.opened_at < self.cooldown_seconds:
                return False
            if state.probe_in_flight:
                return False
            state.probe_in_flight = True
            return True

    def success(self, key: str) -> None:
        with self._lock:
            self._states[key] = _CircuitState()

    def failure(self, key: str) -> None:
        with self._lock:
            state = self._states.setdefault(key, _CircuitState())
            state.probe_in_flight = False
            state.consecutive_failures += 1
            if state.opened_at is not None or state.consecutive_failures >= self.failure_threshold:
                state.opened_at = self._clock()

    def cancelled(self, key: str) -> None:
        with self._lock:
            state = self._states.setdefault(key, _CircuitState())
            state.probe_in_flight = False

    def snapshot(self, key: str) -> dict[str, Any]:
        with self._lock:
            state = self._states.get(key, _CircuitState())
            retry_after_ms = 0
            status = "closed"
            if state.opened_at is not None:
                remaining = self.cooldown_seconds - (self._clock() - state.opened_at)
                if remaining > 0:
                    status = "open"
                    retry_after_ms = max(0, round(remaining * 1000))
                else:
                    status = "half_open"
            return {
                "status": status,
                "consecutive_failures": state.consecutive_failures,
                "retry_after_ms": retry_after_ms,
            }


class ModelRuntimeError(RuntimeError):
    def __init__(self, error_code: str, attempts: list[ModelAttempt]) -> None:
        super().__init__("模型服务暂时不可用")
        self.error_code = error_code
        self.attempts = attempts


def _error_code(error: BaseException) -> str:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):  # noqa: UP038
        return "MODEL_TIMEOUT"
    status_code = getattr(error, "status_code", None)
    name = error.__class__.__name__.casefold()
    if status_code == 429 or "ratelimit" in name:
        return "MODEL_RATE_LIMITED"
    if status_code in {401, 403} or "authentication" in name or "permission" in name:
        return "MODEL_AUTHENTICATION_FAILED"
    if "connection" in name or "connect" in name:
        return "MODEL_UNREACHABLE"
    return "MODEL_PROVIDER_ERROR"


def _retryable(error_code: str) -> bool:
    return error_code in {
        "MODEL_TIMEOUT",
        "MODEL_RATE_LIMITED",
        "MODEL_UNREACHABLE",
        "MODEL_PROVIDER_ERROR",
    }


class ModelRouter(Generic[T]):
    """按主端点→备用端点执行模型调用，并统一记录故障与降级。"""

    def __init__(
        self,
        providers: list[ModelProvider],
        *,
        timeout_seconds: float = 30.0,
        attempts_per_provider: int = 1,
        circuit_breaker: ModelCircuitBreaker | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("模型超时必须大于 0")
        if attempts_per_provider < 1 or attempts_per_provider > 3:
            raise ValueError("单端点尝试次数必须位于 1 到 3 之间")
        self.providers = providers
        self.timeout_seconds = timeout_seconds
        self.attempts_per_provider = attempts_per_provider
        self.circuit_breaker = circuit_breaker or ModelCircuitBreaker()

    def has_provider(self, purpose: ModelPurpose) -> bool:
        return any(provider.supports(purpose) for provider in self.providers)

    def circuit_retry_after_seconds(self, purpose: ModelPurpose) -> float:
        """返回该用途已配置端点中最长的剩余冷却时间。"""

        delays = [
            float(
                self.circuit_breaker.snapshot(f"{provider.name}:{purpose}").get(
                    "retry_after_ms", 0
                )
            )
            / 1000
            for provider in self.providers
            if provider.supports(purpose)
        ]
        return max(delays, default=0.0)

    async def execute(
        self,
        purpose: ModelPurpose,
        operation: Callable[[ModelProvider], Awaitable[T]],
        *,
        timeout_seconds: float | None = None,
        required_model: str | None = None,
    ) -> T:
        """执行一次受控模型调用。

        个别用途可以使用更短或更长的总时限。例如查询改写不应阻塞首字响应，
        而回答生成需要允许模型完成一个经过约束的长回答。断路器和尝试记录仍然
        统一由这里维护。
        """

        effective_timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if effective_timeout <= 0 or effective_timeout > 120:
            raise ValueError("模型调用超时必须位于 0 到 120 秒之间")
        attempts: list[ModelAttempt] = []
        candidates = [
            provider
            for provider in self.providers
            if provider.supports(purpose)
            and (required_model is None or provider.model_for(purpose) == required_model)
        ]
        if not candidates:
            raise ModelRuntimeError("MODEL_NOT_CONFIGURED", attempts)

        for provider_index, provider in enumerate(candidates):
            model = provider.model_for(purpose) or ""
            circuit_key = f"{provider.name}:{purpose}"
            if not self.circuit_breaker.allow(circuit_key):
                attempt = ModelAttempt(
                    purpose,
                    provider.name,
                    model,
                    "circuit_open",
                    0,
                    0,
                    provider_index > 0,
                    "MODEL_CIRCUIT_OPEN",
                )
                attempts.append(attempt)
                _record_attempt(attempt)
                continue

            for attempt_number in range(1, self.attempts_per_provider + 1):
                started = time.perf_counter()
                try:
                    value = await asyncio.wait_for(
                        operation(provider), timeout=effective_timeout
                    )
                except asyncio.CancelledError:
                    duration_ms = round((time.perf_counter() - started) * 1000)
                    attempt = ModelAttempt(
                        purpose,
                        provider.name,
                        model,
                        "cancelled",
                        duration_ms,
                        attempt_number,
                        provider_index > 0,
                        "MODEL_CANCELLED",
                    )
                    attempts.append(attempt)
                    _record_attempt(attempt)
                    self.circuit_breaker.cancelled(circuit_key)
                    raise
                except Exception as error:
                    duration_ms = round((time.perf_counter() - started) * 1000)
                    code = _error_code(error)
                    status: AttemptStatus = "timed_out" if code == "MODEL_TIMEOUT" else "failed"
                    attempt = ModelAttempt(
                        purpose,
                        provider.name,
                        model,
                        status,
                        duration_ms,
                        attempt_number,
                        provider_index > 0,
                        code,
                    )
                    attempts.append(attempt)
                    _record_attempt(attempt)
                    self.circuit_breaker.failure(circuit_key)
                    if not _retryable(code):
                        break
                    if attempt_number < self.attempts_per_provider:
                        continue
                    break
                else:
                    duration_ms = round((time.perf_counter() - started) * 1000)
                    attempt = ModelAttempt(
                        purpose,
                        provider.name,
                        model,
                        "succeeded",
                        duration_ms,
                        attempt_number,
                        provider_index > 0,
                    )
                    attempts.append(attempt)
                    _record_attempt(attempt)
                    self.circuit_breaker.success(circuit_key)
                    return value

        raise ModelRuntimeError(
            attempts[-1].error_code if attempts else "MODEL_NOT_CONFIGURED", attempts
        )

    def health(self) -> list[dict[str, Any]]:
        purposes: tuple[ModelPurpose, ...] = (
            "answer",
            "query_rewrite",
            "evidence_support",
            "research_scout",
            "summary",
            "translation",
            "embedding",
            "vision",
        )
        result: list[dict[str, Any]] = []
        for provider in self.providers:
            purpose_states = {
                purpose: {
                    "configured": provider.supports(purpose),
                    **self.circuit_breaker.snapshot(f"{provider.name}:{purpose}"),
                }
                for purpose in purposes
            }
            result.append({"provider": provider.name, "purposes": purpose_states})
        return result


def build_model_router(config: Any) -> ModelRouter[Any]:
    providers: list[ModelProvider] = []
    if config.openai_api_key:
        providers.append(
            ModelProvider(
                name="primary",
                api_key=config.openai_api_key,
                base_url=config.openai_base_url,
                chat_model=config.chat_model,
                # 聊天与向量接口并不是所有 OpenAI-compatible 服务都会同时提供。
                # 例如 DeepSeek 可承担问答和总结，但目前不提供 Embeddings API。
                embedding_model=config.embedding_model if config.embedding_enabled else "",
                vision_model=config.vision_model,
            )
        )
    fallback_key = getattr(config, "fallback_openai_api_key", None)
    if fallback_key:
        providers.append(
            ModelProvider(
                name="fallback",
                api_key=fallback_key,
                base_url=config.fallback_openai_base_url,
                chat_model=config.fallback_chat_model,
                embedding_model=(
                    config.fallback_embedding_model
                    if config.fallback_embedding_enabled
                    else ""
                ),
                vision_model=config.fallback_vision_model,
            )
        )
    breaker = ModelCircuitBreaker(
        failure_threshold=config.model_circuit_failure_threshold,
        cooldown_seconds=config.model_circuit_cooldown_seconds,
    )
    return ModelRouter(
        providers,
        timeout_seconds=config.model_timeout_seconds,
        attempts_per_provider=config.model_attempts_per_provider,
        circuit_breaker=breaker,
    )
