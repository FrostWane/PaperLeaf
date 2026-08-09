import asyncio
from dataclasses import replace

import pytest

from paperleaf_api.config import settings
from paperleaf_api.model_runtime import (
    ModelCircuitBreaker,
    ModelProvider,
    ModelRouter,
    ModelRuntimeError,
    build_model_router,
    collect_model_attempts,
)


def _provider(name: str) -> ModelProvider:
    return ModelProvider(
        name=name,
        api_key=f"{name}-key",
        base_url=f"https://{name}.example/v1",
        chat_model=f"{name}-chat",
        embedding_model=f"{name}-embedding",
        vision_model=f"{name}-vision",
    )


def test_router_records_success_without_prompt_or_response_content() -> None:
    router = ModelRouter([_provider("primary")], timeout_seconds=1)

    async def run() -> tuple[str, list[dict]]:
        with collect_model_attempts() as attempts:
            value = await router.execute("answer", lambda provider: asyncio.sleep(0, provider.name))
            return value, [attempt.as_dict() for attempt in attempts]

    value, attempts = asyncio.run(run())

    assert value == "primary"
    assert attempts == [
        {
            "purpose": "answer",
            "provider": "primary",
            "model": "primary-chat",
            "status": "succeeded",
            "duration_ms": attempts[0]["duration_ms"],
            "attempt": 1,
            "fallback_used": False,
            "error_code": None,
        }
    ]
    assert "prompt" not in attempts[0]
    assert "response" not in attempts[0]


def test_router_falls_back_and_open_circuit_skips_failed_primary() -> None:
    clock_value = [100.0]
    breaker = ModelCircuitBreaker(
        failure_threshold=1,
        cooldown_seconds=30,
        clock=lambda: clock_value[0],
    )
    router = ModelRouter(
        [_provider("primary"), _provider("fallback")],
        timeout_seconds=1,
        circuit_breaker=breaker,
    )

    async def operation(provider: ModelProvider) -> str:
        if provider.name == "primary":
            raise ConnectionError("secret upstream detail")
        return "fallback-result"

    async def run_once() -> tuple[str, list[dict]]:
        with collect_model_attempts() as attempts:
            value = await router.execute("answer", operation)
            return value, [attempt.as_dict() for attempt in attempts]

    first_value, first_attempts = asyncio.run(run_once())
    second_value, second_attempts = asyncio.run(run_once())

    assert first_value == second_value == "fallback-result"
    assert [item["status"] for item in first_attempts] == ["failed", "succeeded"]
    assert first_attempts[1]["fallback_used"] is True
    assert [item["status"] for item in second_attempts] == ["circuit_open", "succeeded"]
    assert "secret upstream detail" not in str(first_attempts)


def test_half_open_probe_recovers_primary() -> None:
    clock_value = [10.0]
    breaker = ModelCircuitBreaker(
        failure_threshold=1,
        cooldown_seconds=5,
        clock=lambda: clock_value[0],
    )
    router = ModelRouter(
        [_provider("primary"), _provider("fallback")],
        timeout_seconds=1,
        circuit_breaker=breaker,
    )
    primary_healthy = [False]

    async def operation(provider: ModelProvider) -> str:
        if provider.name == "primary" and not primary_healthy[0]:
            raise ConnectionError("down")
        return provider.name

    assert asyncio.run(router.execute("summary", operation)) == "fallback"
    assert breaker.snapshot("primary:summary")["status"] == "open"

    primary_healthy[0] = True
    clock_value[0] += 6
    assert asyncio.run(router.execute("summary", operation)) == "primary"
    assert breaker.snapshot("primary:summary")["status"] == "closed"


def test_timeout_is_classified_and_opens_circuit() -> None:
    breaker = ModelCircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    router = ModelRouter(
        [_provider("primary")], timeout_seconds=0.01, circuit_breaker=breaker
    )

    async def slow(_: ModelProvider) -> str:
        await asyncio.sleep(1)
        return "late"

    with pytest.raises(ModelRuntimeError) as captured:
        asyncio.run(router.execute("embedding", slow))

    assert captured.value.error_code == "MODEL_TIMEOUT"
    assert captured.value.attempts[0].status == "timed_out"
    assert breaker.snapshot("primary:embedding")["status"] == "open"


def test_router_exposes_remaining_circuit_cooldown_for_controlled_retry() -> None:
    clock = [10.0]
    breaker = ModelCircuitBreaker(
        failure_threshold=1,
        cooldown_seconds=30,
        clock=lambda: clock[0],
    )
    router = ModelRouter([_provider("primary")], circuit_breaker=breaker)
    breaker.failure("primary:answer")

    assert router.circuit_retry_after_seconds("answer") == 30.0
    clock[0] += 12
    assert router.circuit_retry_after_seconds("answer") == 18.0


def test_cancellation_propagates_without_counting_as_provider_failure() -> None:
    breaker = ModelCircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    router = ModelRouter(
        [_provider("primary")], timeout_seconds=5, circuit_breaker=breaker
    )

    async def run() -> list[dict]:
        started = asyncio.Event()

        async def blocked(_: ModelProvider) -> str:
            started.set()
            await asyncio.Event().wait()
            return "never"

        with collect_model_attempts() as attempts:
            task = asyncio.create_task(router.execute("answer", blocked))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return [attempt.as_dict() for attempt in attempts]

    attempts = asyncio.run(run())

    assert attempts[0]["status"] == "cancelled"
    assert breaker.snapshot("primary:answer") == {
        "status": "closed",
        "consecutive_failures": 0,
        "retry_after_ms": 0,
    }


def test_chat_provider_can_disable_unsupported_embedding_endpoint() -> None:
    router = build_model_router(
        replace(
            settings,
            openai_api_key="deepseek-key",
            openai_base_url="https://api.deepseek.com",
            chat_model="deepseek-v4-flash",
            embedding_enabled=False,
        )
    )

    assert router.has_provider("answer") is True
    assert router.has_provider("query_rewrite") is True
    assert router.has_provider("summary") is True
    assert router.has_provider("embedding") is False
