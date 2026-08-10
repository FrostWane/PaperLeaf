from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from paperleaf_api.agent.function_tools import (
    FunctionToolHarness,
    PlannerDecision,
    ToolCallRequest,
    ToolExecutionContext,
)
from paperleaf_api.agent.skills import SkillRegistry
from paperleaf_api.config import settings
from paperleaf_api.harness_observability import aggregate_harness_metrics
from paperleaf_api.mcp_gateway import McpGateway, McpGatewayError, _safe_endpoint
from paperleaf_api.repository import (
    AgentRunRecord,
    AgentToolCallRecord,
    MemoryItemRecord,
    MemoryRepository,
)
from paperleaf_api.runtime_store import MemoryRuntimeStore


class _UnusedRouter:
    def has_provider(self, _purpose: str) -> bool:
        return False


class _Retriever:
    async def __call__(self, _request):
        return []


class _Planner:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return PlannerDecision(
                (
                    ToolCallRequest(
                        "mcp-call-1",
                        "mcp__academic__search_semantic_scholar",
                        {"query": "drug target affinity", "limit": 3},
                    ),
                )
            )
        return PlannerDecision()


class _FakeMcpGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        source = "OpenAlex" if name.endswith("search_openalex") else "Semantic Scholar"
        return {
            "source": source,
            "available": True,
            "cached": False,
            "results": [
                {
                    "external_id": "paper-1",
                    "title": "A related paper",
                    "authors": ["Researcher"],
                    "year": 2025,
                    "url": "https://example.org/paper-1",
                }
            ],
        }


def test_mcp_endpoint_rejects_untrusted_hosts_and_private_ip() -> None:
    with pytest.raises(McpGatewayError, match="白名单"):
        _safe_endpoint("https://attacker.example/mcp", {"academic-search-mcp"})
    with pytest.raises(McpGatewayError, match="私有或环回"):
        _safe_endpoint("http://127.0.0.1/mcp", {"127.0.0.1"})
    assert (
        _safe_endpoint(
            "http://academic-search-mcp:8080/mcp", {"academic-search-mcp"}
        )
        == "http://academic-search-mcp:8080/mcp"
    )


def test_mcp_gateway_caches_sanitized_public_metadata() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        runtime_store = MemoryRuntimeStore()
        gateway = McpGateway(
            repository,
            runtime_store,
            replace(
                settings,
                mcp_enabled=True,
                academic_mcp_allowed_hosts="academic-search-mcp",
            ),
        )
        calls = 0

        async def fake_session(_operation, *, require_enabled=True):
            nonlocal calls
            assert require_enabled is True
            calls += 1
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "source": "Semantic Scholar",
                    "available": True,
                    "results": [
                        {
                            "external_id": "paper-1",
                            "title": "  Related   paper  ",
                            "authors": ["Researcher"],
                            "year": 2025,
                            "url": "javascript:alert(1)",
                            "open_access_pdf_url": "http://127.0.0.1/private.pdf",
                            "abstract": "public metadata",
                        }
                    ],
                },
            )

        gateway._with_session = fake_session  # type: ignore[method-assign]
        first = await gateway.call(
            "mcp__academic__search_semantic_scholar", {"query": "DTA", "limit": 3}
        )
        second = await gateway.call(
            "mcp__academic__search_semantic_scholar", {"query": "DTA", "limit": 3}
        )
        assert calls == 1
        assert first["cached"] is False and second["cached"] is True
        assert first["results"][0]["title"] == "Related paper"
        assert first["results"][0]["url"] is None
        assert first["results"][0]["open_access_pdf_url"] is None
        client = await gateway._client()
        assert await gateway._client() is client
        await gateway.close()
        assert client.is_closed

    asyncio.run(scenario())


def test_mcp_disabled_after_cache_never_returns_stale_result() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        gateway = McpGateway(
            repository,
            MemoryRuntimeStore(),
            replace(
                settings,
                mcp_enabled=True,
                academic_mcp_allowed_hosts="academic-search-mcp",
            ),
        )
        calls = 0

        async def fake_session(_operation, *, require_enabled=True):
            nonlocal calls
            calls += 1
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "source": "OpenAlex",
                    "available": True,
                    "results": [{"external_id": "x", "title": "cached paper"}],
                },
            )

        gateway._with_session = fake_session  # type: ignore[method-assign]
        name = "mcp__academic__search_openalex"
        arguments = {"query": "DTA", "limit": 3}
        assert (await gateway.call(name, arguments))["cached"] is False
        assert (await gateway.call(name, arguments))["cached"] is True
        await gateway.set_enabled(False)

        with pytest.raises(McpGatewayError) as captured:
            await gateway.call(name, arguments)

        assert captured.value.code == "MCP_DISABLED"
        assert calls == 1

    asyncio.run(scenario())


def test_function_harness_exposes_mcp_only_when_web_enabled() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        session = await repository.create_chat_session(
            "u1", "相关论文", "library", None, None
        )
        submission = await repository.submit_chat_message(
            session.id,
            "u1",
            "查找相关论文",
            "mcp-client",
            "mcp-hash",
            {"type": "library", "paper_ids": [], "web_enabled": True},
        )
        assert submission is not None
        token = await repository.claim_agent_run_job(submission.run.id)
        assert token is not None
        gateway = _FakeMcpGateway()
        harness = FunctionToolHarness(
            repository,
            _Retriever(),
            _UnusedRouter(),
            planner=_Planner(),
            mcp_gateway=gateway,  # type: ignore[arg-type]
        )
        skill = SkillRegistry.default().get("find_related_papers")
        offline_schemas = harness.schemas_for(skill, web_enabled=False)
        online_schemas = harness.schemas_for(skill, web_enabled=True)
        assert not any(
            item["function"]["name"].startswith("mcp__") for item in offline_schemas
        )
        assert any(
            item["function"]["name"]
            == "mcp__academic__search_semantic_scholar"
            for item in online_schemas
        )
        result = await harness.run(
            "查找相关论文",
            ToolExecutionContext(
                run_id=submission.run.id,
                claim_token=token,
                user_id="u1",
                skill=skill,
                allowed_paper_ids=(),
                current_paper_id=None,
                web_enabled=True,
            ),
        )
        assert result.automatic_source_fallback_used is True
        assert [item["source"] for item in result.calls] == [
            "OpenAlex",
            "Semantic Scholar",
        ]
        assert gateway.calls == [
            (
                "mcp__academic__search_openalex",
                {"query": "查找相关论文", "limit": 8},
            ),
            (
                "mcp__academic__search_semantic_scholar",
                {"query": "drug target affinity", "limit": 3},
            )
        ]

    asyncio.run(scenario())


def test_harness_metrics_never_include_user_content_or_identifiers() -> None:
    timestamp = datetime.now(timezone.utc)
    run = AgentRunRecord(
        id="private-run-id",
        user_id="private-user-id",
        session_id="private-session-id",
        thread_id="private-thread-id",
        status="completed",
        selected_skill="paper_qa",
        reference_confidence=0.9,
        context_snapshot={
            "usage": {
                "conversation_before_tokens": 1000,
                "conversation_after_tokens": 400,
                "compacted": True,
            },
            "secret_question": "不要泄露",
        },
        harness_trace={"skill_route_source": "model_function_call"},
        result_summary={
            "rag_trace": {
                "stage_timings_ms": {"context": 12},
                "vector_fallback_reasons": ["query_dimension_mismatch"],
            }
        },
        created_at=timestamp,
    )
    call = AgentToolCallRecord(
        id="private-call-id",
        call_id="model-call",
        run_id=run.id,
        user_id=run.user_id,
        skill_name="paper_qa",
        tool_name="search_library",
        status="succeeded",
        arguments={"query": "private question"},
        duration_ms=30,
        created_at=timestamp,
    )
    memory = MemoryItemRecord(
        id="memory-1",
        user_id=run.user_id,
        type="research_interest",
        value="private memory value",
        normalized_hash="hash",
        confidence=1,
        source_kind="explicit",
    )
    report = aggregate_harness_metrics(
        [run],
        [call],
        {
            "total": 1,
            "active": 1,
            "disabled": 0,
            "pinned": 0,
            "users_with_memory": 1,
            "capacity": 200,
            "superseded_versions": 0,
            "types": {memory.type: 1},
            "sources": {memory.source_kind: 1},
        },
        [],
        embedding={
            "configured": True,
            "model": "qwen3-embedding:0.6b",
            "dimensions": 1024,
            "revision": 1,
            "ready": 2,
            "ready_current": 2,
            "stale": 1,
        },
        window_hours=24,
        limit_reached=False,
    )
    serialized = str(report)
    assert report["context"]["compression_rate"] == pytest.approx(0.6)
    assert report["tools"]["success_rate"] == 1
    assert report["embedding"]["ready_current"] == 2
    assert report["embedding"]["fallback_reasons"] == {
        "query_dimension_mismatch": 1
    }
    for secret in (
        "private-run-id",
        "private-user-id",
        "private-session-id",
        "private question",
        "private memory value",
        "不要泄露",
    ):
        assert secret not in serialized
