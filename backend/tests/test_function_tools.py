from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

from paperleaf_api.agent.context import (
    ContextResolution,
    resolve_context,
)
from paperleaf_api.agent.function_tools import (
    FunctionToolHarness,
    PlannerDecision,
    ToolCallRequest,
    ToolExecutionContext,
)
from paperleaf_api.agent.graph import _displayed_external_recommendations
from paperleaf_api.agent.skills import SkillRegistry
from paperleaf_api.agent_execution import (
    _next_entity_state,
    _selection_scope_is_locked,
    execute_agent_run,
)
from paperleaf_api.config import settings
from paperleaf_api.model_runtime import ModelRuntimeError
from paperleaf_api.rag.answer_quality import AnswerQualityPolicy
from paperleaf_api.rag.citations import CitationClaim, Evidence
from paperleaf_api.repository import MemoryRepository, PaperArtifactRecord, PaperRecord


class SequencePlanner:
    def __init__(self, *decisions: PlannerDecision) -> None:
        self.decisions = list(decisions)
        self.calls = 0

    async def decide(self, **_kwargs) -> PlannerDecision:
        self.calls += 1
        return self.decisions.pop(0) if self.decisions else PlannerDecision()


class FakeRetriever:
    def __init__(self) -> None:
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return [
            Evidence(
                "p1:p2:c0",
                "p1",
                "测试论文",
                2,
                "这是服务端验证后的论文证据。",
                retrieval_channels=("keyword",),
            )
        ]


def test_graph_returns_structured_candidates_without_parsing_markdown_titles() -> None:
    contexts = [
        json.dumps(
            [
                {
                    "kind": "result",
                    "tool": "mcp__academic__search_openalex",
                    "content": json.dumps(
                        {
                            "source": "OpenAlex",
                            "items": [
                                {
                                    "title": "Title with | markdown _ characters",
                                    "doi": "10.1/displayed",
                                    "year": 2026,
                                    "relevance_score": 0.9,
                                },
                                {
                                    "title": "Provider-only candidate",
                                    "doi": "10.1/provider",
                                    "year": 2026,
                                    "relevance_score": 0.8,
                                },
                            ],
                        }
                    ),
                }
            ]
        )
    ]

    displayed = _displayed_external_recommendations(
        "推荐 1 篇 2026 年论文", contexts, []
    )

    assert [item["doi"] for item in displayed] == ["10.1/displayed"]


class UnusedRouter:
    pass


class SelectingPlanner(SequencePlanner):
    def __init__(self, selected: str) -> None:
        super().__init__()
        self.selected = selected

    async def select_skill(self, **_kwargs) -> str:
        return self.selected


def test_selection_scope_defaults_to_same_page_and_requires_explicit_expansion() -> None:
    assert _selection_scope_is_locked("这些讲了什么？") is True
    assert _selection_scope_is_locked("只解释选中的原文") is True
    assert _selection_scope_is_locked("不要扩展成全文总结") is True
    assert _selection_scope_is_locked("结合全文解释这段原文") is False
    assert _selection_scope_is_locked("Explain this using the whole paper") is False


def test_recommendation_exposures_persist_for_next_batch_and_reset_for_new_task() -> None:
    inherited = ContextResolution(
        original_query="换一批",
        resolved_query="换一批",
        references={
            "active_task": {
                "name": "find_related_papers",
                "requested_count": 5,
                "shown_entities": ["doi:10.1/old"],
            }
        },
        confidence=0.9,
        sources=("active_task",),
    )
    continued = _next_entity_state(
        {},
        inherited,
        "换一批",
        selected_skill="find_related_papers",
        web_enabled=True,
        exposed_recommendation_entities=["doi:10.1/new"],
    )
    assert continued["active_task"]["shown_entities"] == [
        "doi:10.1/old",
        "doi:10.1/new",
    ]

    fresh = ContextResolution(
        original_query="推荐三篇图像生成论文",
        resolved_query="推荐三篇图像生成论文",
        references={},
        confidence=1.0,
        sources=("explicit_query",),
    )
    reset = _next_entity_state(
        continued,
        fresh,
        fresh.original_query,
        selected_skill="find_related_papers",
        web_enabled=True,
        exposed_recommendation_entities=["doi:10.1/image"],
    )
    assert reset["active_task"]["shown_entities"] == ["doi:10.1/image"]


async def _context(repository: MemoryRepository, skill_name: str, *, web: bool = False):
    session = await repository.create_chat_session("u1", "工具测试", "paper", "p1", None)
    submission = await repository.submit_chat_message(
        session.id,
        "u1",
        "测试工具",
        "tool-client-1",
        "tool-hash-1",
        {"type": "paper", "paper_ids": ["p1"], "web_enabled": web},
    )
    assert submission is not None
    token = await repository.claim_agent_run_job(submission.run.id)
    assert token is not None
    await repository.start_agent_run(submission.run.id, token)
    skill = SkillRegistry.default().get(skill_name)
    return ToolExecutionContext(
        run_id=submission.run.id,
        claim_token=token,
        user_id="u1",
        skill=skill,
        allowed_paper_ids=("p1",),
        current_paper_id="p1",
        web_enabled=web,
    )


def test_native_read_tool_uses_trusted_scope_and_persists_audit() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        retriever = FakeRetriever()
        planner = SequencePlanner(
            PlannerDecision(
                (ToolCallRequest("call-1", "search_current_paper", {"query": "方法"}),)
            ),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(repository, retriever, UnusedRouter(), planner=planner)
        result = await harness.run("这篇论文的方法是什么？", await _context(repository, "paper_qa"))

        assert result.provider_supported is True
        assert [item.chunk_id for item in result.evidence] == ["p1:p2:c0"]
        assert retriever.requests[0].user_id == "u1"
        assert retriever.requests[0].paper_ids == ["p1"]
        record = next(iter(repository.agent_tool_calls.values()))
        assert record.status == "succeeded"
        assert record.tool_name == "search_current_paper"
        assert "user_id" not in record.arguments and "paper_ids" not in record.arguments

    asyncio.run(scenario())


def test_verified_selection_locks_tool_evidence_to_bound_physical_page() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        retriever = FakeRetriever()
        planner = SequencePlanner(
            PlannerDecision(
                (ToolCallRequest("call-1", "search_current_paper", {"query": "全文"}),)
            ),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(repository, retriever, UnusedRouter(), planner=planner)
        context = replace(
            await _context(repository, "paper_qa"),
            verified_selection_page=1,
            selection_scope_locked=True,
        )
        result = await harness.run("这些讲了什么？", context)

        assert result.evidence == []
        assert result.tool_mode_active is False
        assert result.fallback_reason == "tool_outputs_not_usable"
        assert result.calls[0]["status"] == "succeeded"

    asyncio.run(scenario())


def test_model_skill_selection_is_versioned_and_web_policy_cannot_be_bypassed() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        registry = SkillRegistry.default()
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=SelectingPlanner("compare_papers"),
        )
        definition, source, confidence = await harness.select_skill(
            registry,
            "比较两篇论文",
            intent="comparison",
            scope="collection",
            web_enabled=False,
        )
        assert definition.identity == "compare_papers@1"
        assert source == "model_function_call" and confidence == 0.9

        blocked = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=SelectingPlanner("find_related_papers"),
        )
        definition, source, _confidence = await blocked.select_skill(
            registry,
            "解释当前论文",
            intent="fact_lookup",
            scope="paper",
            web_enabled=False,
        )
        assert definition.manifest.name == "paper_qa"
        assert source == "deterministic_fallback"

    asyncio.run(scenario())


def test_tool_schema_rejects_injected_identity_then_allows_one_repair() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        retriever = FakeRetriever()
        planner = SequencePlanner(
            PlannerDecision(
                (
                    ToolCallRequest(
                        "bad-call",
                        "search_library",
                        {"query": "方法", "user_id": "victim"},
                    ),
                )
            ),
            PlannerDecision((ToolCallRequest("fixed-call", "search_library", {"query": "方法"}),)),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(repository, retriever, UnusedRouter(), planner=planner)
        result = await harness.run("方法", await _context(repository, "paper_qa"))

        assert len(result.evidence) == 1
        assert len(retriever.requests) == 1
        assert retriever.requests[0].user_id == "u1"
        assert {item.status for item in repository.agent_tool_calls.values()} == {
            "rejected",
            "succeeded",
        }
        assert any(
            item.error_code == "TOOL_ARGUMENT_INVALID"
            for item in repository.agent_tool_calls.values()
        )

    asyncio.run(scenario())


def test_page_tool_cannot_read_paper_outside_trusted_scope() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        planner = SequencePlanner(
            PlannerDecision(
                (
                    ToolCallRequest(
                        "page-1",
                        "get_page_text",
                        {"paper_id": "victim-paper", "physical_page": 1},
                    ),
                )
            ),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(repository, FakeRetriever(), UnusedRouter(), planner=planner)
        result = await harness.run("读取第一页", await _context(repository, "paper_qa"))

        assert result.evidence == []
        record = next(iter(repository.agent_tool_calls.values()))
        assert record.status == "failed"
        assert record.error_code == "TOOL_PERMISSION_DENIED"

    asyncio.run(scenario())


def test_page_tool_resolves_unique_trusted_title_to_current_paper_id() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        repository.papers["p1"] = PaperRecord(
            id="p1",
            owner_id="u1",
            title="DeepDTA",
            authors=[],
            year=2018,
            abstract=None,
            doi=None,
            arxiv_id=None,
            filename="deepdta.pdf",
            storage_key="papers/u1/p1.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            sha256="page-tool-paper",
            page_count=1,
        )
        repository.paper_pages["p1"] = {1: "服务端验证后的第一页原文。"}
        planner = SequencePlanner(
            PlannerDecision(
                (
                    ToolCallRequest(
                        "page-title-alias",
                        "get_page_text",
                        {"paper_id": "DeepDTA", "physical_page": 1},
                    ),
                )
            ),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(repository, FakeRetriever(), UnusedRouter(), planner=planner)
        context = replace(
            await _context(repository, "trace_original"),
            scope_paper_titles=("DeepDTA",),
            selection_scope_locked=True,
            verified_selection_page=1,
        )

        result = await harness.run("解释选文", context)

        assert [item.chunk_id for item in result.evidence] == ["page:p1:p1"]
        record = next(iter(repository.agent_tool_calls.values()))
        assert record.status == "succeeded"
        assert result.calls[0]["argument_resolution"] == "trusted_current_paper_title"

    asyncio.run(scenario())


def test_unknown_tool_is_rejected_and_parallel_batch_is_capped_at_three() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        planner = SequencePlanner(
            PlannerDecision(
                tuple(
                    ToolCallRequest(f"call-{index}", f"unknown_{index}", {}) for index in range(4)
                )
            )
        )
        harness = FunctionToolHarness(repository, FakeRetriever(), UnusedRouter(), planner=planner)
        result = await harness.run("测试", await _context(repository, "paper_qa"))

        assert len(result.calls) == 3
        assert all(item["status"] == "rejected" for item in result.calls)
        assert len(repository.agent_tool_calls) == 3

    asyncio.run(scenario())


def test_write_tool_only_creates_interrupt_and_never_executes_import() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        planner = SequencePlanner(
            PlannerDecision(
                (
                    ToolCallRequest(
                        "import-1",
                        "request_import",
                        {
                            "arxiv_id": "2601.00001",
                            "title": "公开论文",
                            "pdf_url": "https://arxiv.org/pdf/2601.00001",
                        },
                    ),
                )
            )
        )
        harness = FunctionToolHarness(repository, FakeRetriever(), UnusedRouter(), planner=planner)
        result = await harness.run(
            "导入这篇论文", await _context(repository, "find_related_papers", web=True)
        )

        assert result.pending_action is not None
        assert result.pending_action["type"] == "confirm_arxiv_import"
        record = next(iter(repository.agent_tool_calls.values()))
        assert record.status == "approval_required"
        assert record.requires_approval is True

    asyncio.run(scenario())


def test_confirmed_import_executes_only_after_approval() -> None:
    async def scenario() -> None:
        imported = []

        async def importer(user_id, candidate):
            imported.append((user_id, candidate["arxiv_id"]))
            return type("Paper", (), {"title": "确认后的论文"})()

        harness = FunctionToolHarness(
            MemoryRepository("secret"),
            FakeRetriever(),
            UnusedRouter(),
            planner=SequencePlanner(),
            confirmed_importer=importer,
        )
        action = {
            "type": "confirm_arxiv_import",
            "candidates": [{"arxiv_id": "2601.00001"}],
        }
        rejected, rejected_error = await harness.resume_confirmed_action("u1", action, "reject")
        assert "取消" in rejected and rejected_error is None and imported == []

        approved, approved_error = await harness.resume_confirmed_action("u1", action, "approve")
        assert "确认后的论文" in approved and approved_error is None
        assert imported == [("u1", "2601.00001")]

    asyncio.run(scenario())


def test_confirmed_doi_import_revalidates_openalex_metadata_before_write() -> None:
    class FakeMcpGateway:
        async def call(self, name: str, arguments: dict) -> dict:
            assert name == "mcp__academic__get_academic_metadata"
            assert arguments == {
                "identifier": "10.1000/open.paper",
                "source": "openalex",
            }
            return {
                "results": [
                    {
                        "external_id": "W1",
                        "title": "Open paper",
                        "authors": ["Author"],
                        "year": 2026,
                        "publication": "Open Journal",
                        "doi": "10.1000/open.paper",
                        "open_access_pdf_url": "https://example.org/open.pdf",
                    }
                ]
            }

    async def scenario() -> None:
        imported: list[dict] = []

        async def importer(_user_id: str, candidate: dict):
            imported.append(candidate)
            return type("Paper", (), {"title": candidate["title"]})()

        harness = FunctionToolHarness(
            MemoryRepository("secret"),
            FakeRetriever(),
            UnusedRouter(),
            planner=SequencePlanner(),
            mcp_gateway=FakeMcpGateway(),
            confirmed_importer=importer,
        )
        answer, error = await harness.resume_confirmed_action(
            "u1",
            {
                "type": "confirm_arxiv_import",
                "candidates": [
                    {
                        "doi": "10.1000/open.paper",
                        "title": "模型提供的标题不会被直接信任",
                    }
                ],
            },
            "approve",
        )

        assert error is None
        assert "Open paper" in answer
        assert imported[0]["open_access_pdf_url"] == "https://example.org/open.pdf"
        assert imported[0]["title"] == "Open paper"

    asyncio.run(scenario())


def test_timeout_retries_once_and_stops_without_looping() -> None:
    class SlowHarness(FunctionToolHarness):
        attempts = 0

        async def _invoke_tool(self, name, parsed, context):
            self.attempts += 1
            await asyncio.sleep(0.03)
            return await super()._invoke_tool(name, parsed, context)

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        planner = SequencePlanner(
            PlannerDecision((ToolCallRequest("slow-1", "search_library", {"query": "方法"}),)),
            PlannerDecision(),
        )
        harness = SlowHarness(repository, FakeRetriever(), UnusedRouter(), planner=planner)
        harness.specs["search_library"] = replace(
            harness.specs["search_library"], timeout_seconds=0.01, retries=1
        )
        result = await harness.run("方法", await _context(repository, "paper_qa"))

        assert harness.attempts == 2
        assert result.calls[0]["error_code"] == "TOOL_TIMEOUT"
        assert next(iter(repository.agent_tool_calls.values())).status == "failed"

    asyncio.run(scenario())


def test_large_tool_result_is_externalized_to_owned_artifact() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        artifact = PaperArtifactRecord(
            id="summary-1",
            paper_id="p1",
            owner_id="u1",
            type="summary",
            source_revision="r1",
            status="ready",
            fallback_reason=None,
            structured_payload={"large": "证据" * 20000},
            markdown="中文概括",
        )
        repository.paper_artifacts[artifact.id] = artifact
        planner = SequencePlanner(
            PlannerDecision(
                (ToolCallRequest("summary-call", "summarize_paper", {"paper_id": "p1"}),)
            ),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(repository, FakeRetriever(), UnusedRouter(), planner=planner)
        result = await harness.run("概括论文", await _context(repository, "summarize_paper"))

        assert result.calls[0]["artifact_tokens"] > 8000
        assert result.calls[0]["artifact_id"] is not None
        stored = next(iter(repository.agent_tool_artifacts.values()))
        assert stored.user_id == "u1"
        assert stored.content["structured_payload"]["large"].startswith("证据")

    asyncio.run(scenario())


def test_provider_without_native_function_calling_falls_back_cleanly() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        planner = SequencePlanner(PlannerDecision(provider_supported=False))
        harness = FunctionToolHarness(repository, FakeRetriever(), UnusedRouter(), planner=planner)
        result = await harness.run("方法", await _context(repository, "paper_qa"))

        assert result.provider_supported is False
        assert result.native_function_calling_attempted is True
        assert result.tool_mode_active is False
        assert result.fallback_reason == "provider_without_native_function_calling"
        assert repository.agent_tool_calls == {}

    asyncio.run(scenario())


def test_all_failed_or_rejected_calls_do_not_activate_tool_mode() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        planner = SequencePlanner(
            PlannerDecision((ToolCallRequest("bad", "unknown_tool", {}),)),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(repository, FakeRetriever(), UnusedRouter(), planner=planner)

        result = await harness.run("解释方法", await _context(repository, "paper_qa"))

        assert result.native_function_calling_attempted is True
        assert result.calls[0]["status"] == "rejected"
        assert result.tool_mode_active is False
        assert result.usable_evidence is False
        assert result.fallback_reason == "tool_outputs_not_usable"

    asyncio.run(scenario())


def test_explicit_openalex_request_uses_scoped_titles_when_model_omits_tool_call() -> None:
    class FakeMcpGateway:
        async def call(self, name: str, arguments: dict) -> dict:
            assert name == "mcp__academic__search_openalex"
            return {
                "source": "OpenAlex",
                "available": True,
                "cached": False,
                "results": [
                    {
                        "external_id": f"openalex:{arguments['query']}",
                        "title": f"Related to {arguments['query']}",
                        "year": 2026,
                    }
                ],
            }

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        planner = SequencePlanner(PlannerDecision())
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=planner,
            mcp_gateway=FakeMcpGateway(),
        )
        context = replace(
            await _context(repository, "find_related_papers", web=True),
            scope_paper_titles=("DeepDTA", "AttentionDTA"),
        )

        result = await harness.run(
            "请只调用 OpenAlex 查找集合相关论文，不使用本地文献库。", context
        )

        assert result.native_function_calling_attempted is True
        assert result.explicit_source_fallback_used is True
        assert result.usable_external_context is True
        assert result.tool_mode_active is True
        assert [item["tool"] for item in result.calls] == [
            "mcp__academic__search_openalex",
        ]
        assert all(item["status"] == "succeeded" for item in result.calls)
        assert len(repository.agent_tool_calls) == 1

    asyncio.run(scenario())


def test_unspecified_online_discovery_reserves_openalex_and_caps_repeated_searches() -> None:
    class FakeMcpGateway:
        def __init__(self) -> None:
            self.arguments: list[dict] = []

        async def call(self, name: str, arguments: dict) -> dict:
            assert name == "mcp__academic__search_openalex"
            self.arguments.append(arguments)
            return {
                "source": "OpenAlex",
                "available": True,
                "cached": False,
                "results": [
                    {
                        "external_id": f"W-related-{index}",
                        "title": f"Related drug-target affinity paper {index}",
                        "year": 2025 - index,
                        "abstract": "完整摘要 " * 500,
                    }
                    for index in range(8)
                ],
            }

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        gateway = FakeMcpGateway()
        planner = SequencePlanner(
            PlannerDecision(
                (
                    ToolCallRequest("local-1", "search_library", {"query": "DTA"}),
                    ToolCallRequest("local-2", "search_library", {"query": "affinity"}),
                    ToolCallRequest(
                        "openalex-again",
                        "mcp__academic__search_openalex",
                        {"query": "another query", "limit": 5},
                    ),
                )
            ),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=planner,
            mcp_gateway=gateway,
        )
        context = replace(
            await _context(repository, "find_related_papers", web=True),
            scope_paper_titles=(
                "AR-RAG: Autoregressive Retrieval Augmentation for Image Generation",
                "AttentionDTA",
                "DeepDTA: deep drug-target binding affinity prediction",
                "SyntheticDTA",
            ),
        )

        result = await harness.run("请根据当前集合的主题联网推荐相关论文。", context)

        assert result.automatic_source_fallback_used is True
        assert result.native_function_calling_attempted is True
        assert result.usable_external_context is True
        assert result.tool_mode_active is True
        assert [item["tool"] for item in result.calls] == [
            "mcp__academic__search_openalex",
            "search_library",
        ]
        assert gateway.arguments == [
            {
                "query": "DeepDTA: deep drug-target binding affinity prediction",
                "limit": 10,
            }
        ]
        assert result.steps == 2
        assert len(repository.agent_tool_calls) == 2
        openalex_context = json.loads(result.context_entries[1]["content"])
        assert len(openalex_context["items"]) == 8
        assert openalex_context["items"][-1]["title"].endswith("7")
        assert openalex_context["scope_paper_count"] == 4
        assert len(result.context_entries[1]["content"]) <= 3000
        assert all(
            len(item.get("abstract_preview", "")) <= 120 for item in openalex_context["items"]
        )

    asyncio.run(scenario())


def test_automatic_openalex_provider_error_is_a_failed_tool_not_usable_output() -> None:
    class UnavailableMcpGateway:
        async def call(self, _name: str, _arguments: dict) -> dict:
            return {
                "source": "OpenAlex",
                "available": False,
                "error_code": "OPENALEX_RATE_LIMITED",
                "results": [],
            }

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=SequencePlanner(PlannerDecision()),
            mcp_gateway=UnavailableMcpGateway(),
        )
        context = replace(
            await _context(repository, "find_related_papers", web=True),
            scope_paper_titles=("DeepDTA",),
        )

        result = await harness.run("请联网推荐相关论文", context)

        assert result.calls[0] == {
            "tool": "mcp__academic__search_openalex",
            "status": "failed",
            "error_code": "OPENALEX_RATE_LIMITED",
        }
        assert result.usable_external_context is False
        assert result.tool_mode_active is False
        record = next(iter(repository.agent_tool_calls.values()))
        assert record.status == "failed"
        assert record.error_code == "OPENALEX_RATE_LIMITED"

    asyncio.run(scenario())


def test_automatic_openalex_respects_explicit_source_and_web_setting() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=SequencePlanner(),
            mcp_gateway=object(),
        )
        online = replace(
            await _context(repository, "find_related_papers", web=True),
            scope_paper_titles=("DeepDTA",),
        )
        schemas = harness.schemas_for(online.skill, web_enabled=True)

        assert harness._automatic_openalex_calls("请只搜索 arXiv 相关论文", online, schemas) == ()
        alternative = harness._automatic_openalex_calls(
            "请联网搜索，但不要使用 OpenAlex", online, schemas
        )
        assert len(alternative) == 1
        assert alternative[0].name == "mcp__academic__search_semantic_scholar"
        assert (
            harness._automatic_openalex_calls("请使用 Semantic Scholar 搜索", online, schemas) == ()
        )
        assert (
            harness._automatic_openalex_calls(
                "请联网推荐相关论文", replace(online, web_enabled=False), schemas
            )
            == ()
        )

    asyncio.run(scenario())


def test_automatic_openalex_applies_explicit_publication_year_filter() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=SequencePlanner(),
            mcp_gateway=object(),
        )
        context = replace(
            await _context(repository, "find_related_papers", web=True),
            scope_paper_titles=("DeepDTA: deep drug-target binding affinity prediction",),
        )
        schemas = harness.schemas_for(context.skill, web_enabled=True)

        calls = harness._automatic_openalex_calls(
            "有没有更近的论文，如 2026 年的\n\n[已验证阅读上下文]\n上一轮包含 2019 年",
            context,
            schemas,
        )

        assert len(calls) == 1
        assert calls[0].arguments == {
            "query": "DeepDTA: deep drug-target binding affinity prediction",
            "limit": 10,
            "year_from": 2026,
            "year_to": 2026,
        }

    asyncio.run(scenario())


def test_negated_openalex_model_call_is_rejected_and_semantic_scholar_is_used() -> None:
    class FakeMcpGateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call(self, name: str, _arguments: dict) -> dict:
            self.calls.append(name)
            return {
                "source": "Semantic Scholar",
                "available": True,
                "results": [{"title": "Cross-domain method", "year": 2026}],
            }

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        gateway = FakeMcpGateway()
        planner = SequencePlanner(
            PlannerDecision(
                (
                    ToolCallRequest(
                        "wrong-source",
                        "mcp__academic__search_openalex",
                        {"query": "topic", "limit": 5},
                    ),
                )
            ),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=planner,
            mcp_gateway=gateway,
        )
        context = replace(
            await _context(repository, "find_related_papers", web=True),
            scope_paper_titles=("General topic",),
        )

        result = await harness.run(
            "不要 OpenAlex，请使用 Semantic Scholar 推荐五篇论文",
            context,
        )

        assert "mcp__academic__search_openalex" not in gateway.calls
        assert gateway.calls == ["mcp__academic__search_semantic_scholar"]
        assert any(
            item.get("tool") == "mcp__academic__search_openalex"
            and item.get("status") == "rejected"
            for item in result.calls
        )
        rejected = [
            item
            for item in repository.agent_tool_calls.values()
            if item.tool_name == "mcp__academic__search_openalex"
        ]
        assert rejected and rejected[0].error_code == "SOURCE_EXCLUDED_BY_USER"

    asyncio.run(scenario())


def test_inherited_semantic_scholar_policy_and_year_reach_real_tool_arguments() -> None:
    class FakeMcpGateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call(self, name: str, arguments: dict) -> dict:
            self.calls.append((name, arguments))
            return {
                "source": "Semantic Scholar",
                "available": True,
                "results": [
                    {
                        "title": "A representative topic study",
                        "abstract": "A representative topic examined in 2026",
                        "year": 2026,
                    }
                ],
            }

    async def scenario() -> None:
        resolution = resolve_context(
            "有没有 2026 年的",
            {"collection_id": "collection-1"},
            [
                {
                    "role": "user",
                    "content": (
                        "联网推荐五篇尚未入库的论文，不要使用 OpenAlex，改用 Semantic Scholar。"
                    ),
                }
            ],
            session_type="collection",
        )
        repository = MemoryRepository("secret")
        gateway = FakeMcpGateway()
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=SequencePlanner(PlannerDecision()),
            mcp_gateway=gateway,
        )
        context = replace(
            await _context(repository, "find_related_papers", web=True),
            scope_paper_titles=("A representative topic",),
        )

        result = await harness.run(resolution.resolved_query, context)

        assert result.usable_external_context is True
        assert gateway.calls == [
            (
                "mcp__academic__search_semantic_scholar",
                {
                    "query": "A representative topic",
                    "limit": 10,
                    "year_from": 2026,
                    "year_to": 2026,
                },
            )
        ]

    asyncio.run(scenario())


def test_discovery_limit_is_enforced_per_provider_not_tool_alias() -> None:
    class CountingArxiv:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, _request):
            self.calls += 1
            return SimpleNamespace(data=[{"title": "A related preprint", "arxiv_id": "2601.00001"}])

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        search = CountingArxiv()
        planner = SequencePlanner(
            PlannerDecision(
                (
                    ToolCallRequest("arxiv-alias-1", "search_arxiv", {"query": "DTA", "limit": 5}),
                    ToolCallRequest(
                        "arxiv-alias-2",
                        "find_related_papers",
                        {"query": "drug target affinity", "limit": 5},
                    ),
                )
            ),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=planner,
            arxiv_search=search,
        )
        context = replace(
            await _context(repository, "find_related_papers", web=True),
            scope_paper_titles=("DeepDTA",),
            scope_paper_texts=("DeepDTA\ndrug target binding affinity",),
        )

        result = await harness.run("只使用 arXiv 推荐相关论文", context)

        assert search.calls == 1
        assert result.steps == 1
        assert [item["tool"] for item in result.calls] == ["search_arxiv"]

    asyncio.run(scenario())


def test_structured_discovery_task_reaches_provider_and_excludes_seen_entities() -> None:
    class FakeMcpGateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call(self, name: str, arguments: dict) -> dict:
            self.calls.append((name, arguments))
            return {
                "source": "Semantic Scholar",
                "available": True,
                "results": [
                    {
                        "external_id": "old",
                        "title": "Already shown",
                        "doi": "10.1/old",
                        "year": 2026,
                        "publication_types": ["JournalArticle"],
                    },
                    {
                        "external_id": "library",
                        "title": "Already in library",
                        "doi": "10.1/library",
                        "year": 2026,
                        "publication_types": ["JournalArticle"],
                    },
                    {
                        "external_id": "dataset",
                        "title": "A dataset",
                        "year": 2026,
                        "publication_types": ["Dataset"],
                    },
                    {
                        "external_id": "unrelated",
                        "title": "Quantum error correction for photonic systems",
                        "abstract": "fault tolerant qubits and optical circuits",
                        "year": 2026,
                        "publication_types": ["JournalArticle"],
                    },
                    *[
                        {
                            "external_id": f"new-{index}",
                            "title": f"New affinity paper {index}",
                            "doi": f"10.1/new-{index}",
                            "abstract": "drug target binding affinity",
                            "year": 2026,
                            "publication_types": ["JournalArticle"],
                        }
                        for index in range(4)
                    ],
                ],
            }

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        gateway = FakeMcpGateway()
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=SequencePlanner(PlannerDecision()),
            mcp_gateway=gateway,
        )
        context = replace(
            await _context(repository, "find_related_papers", web=True),
            scope_paper_titles=("DeepDTA",),
            scope_paper_texts=("DeepDTA\ndrug target binding affinity",),
            excluded_recommendation_entities=frozenset({"doi:10.1/library"}),
            previous_recommendation_entities=frozenset({"doi:10.1/old"}),
            discovery_task={
                "requested_count": 3,
                "year_from": 2026,
                "year_to": 2026,
                "requested_sources": ["mcp__academic__search_semantic_scholar"],
                "denied_sources": ["mcp__academic__search_openalex"],
            },
        )

        result = await harness.run("换一批", context)

        assert gateway.calls == [
            (
                "mcp__academic__search_semantic_scholar",
                {
                    "query": "DeepDTA",
                    "limit": 6,
                    "year_from": 2026,
                    "year_to": 2026,
                },
            )
        ]
        preview = next(item for item in result.calls if item["status"] == "succeeded")
        assert [item["title"] for item in preview["items"]] == [
            "New affinity paper 0",
            "New affinity paper 1",
            "New affinity paper 2",
        ]
        assert preview["filter_stats"]["type_filtered"] == 1
        assert preview["filter_stats"]["duplicate_filtered"] == 2
        assert preview["filter_stats"]["relevance_filtered"] == 1
        assert all("old" not in value for value in result.exposed_recommendation_entities)

    asyncio.run(scenario())


def test_later_planner_timeout_preserves_completed_tool_audit_and_evidence() -> None:
    class TimeoutAfterFirstCallPlanner:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, **_kwargs) -> PlannerDecision:
            self.calls += 1
            if self.calls == 1:
                return PlannerDecision(
                    (ToolCallRequest("kept-call", "search_current_paper", {"query": "方法"}),)
                )
            raise ModelRuntimeError("MODEL_TIMEOUT", [])

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        harness = FunctionToolHarness(
            repository,
            FakeRetriever(),
            UnusedRouter(),
            planner=TimeoutAfterFirstCallPlanner(),
        )

        result = await harness.run("这篇论文的方法是什么？", await _context(repository, "paper_qa"))

        assert result.native_function_calling_attempted is True
        assert result.tool_mode_active is True
        assert result.fallback_reason == "tool_planner_model_timeout"
        assert len(result.calls) == 1
        assert result.calls[0]["status"] == "succeeded"
        assert [item.chunk_id for item in result.evidence] == ["p1:p2:c0"]
        assert len(repository.agent_tool_calls) == 1

    asyncio.run(scenario())


def test_function_tool_evidence_is_reused_by_agent_graph_without_duplicate_retrieval() -> None:
    class EvidenceGraph:
        initial = None

        async def ainvoke(self, initial, _config):
            self.initial = initial
            evidence = list(initial.get("pre_retrieved_evidence", []))
            return {
                "status": "completed",
                "answer": "论文使用了服务端验证的方法 [chunk:p1:p2:c0]。",
                "retrieved_evidence": evidence,
                "citations": [CitationClaim("p1:p2:c0", "p1", 2)],
                "evidence_quality": {
                    "grade": "sufficient",
                    "answer_support_grade": "supported",
                    "answer_support_confidence": 1.0,
                    "reason_code": "answer_supported",
                },
                "tool_steps": initial["tool_steps"],
                "stage_timings_ms": initial["stage_timings_ms"],
            }

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        session = await repository.create_chat_session("u1", "集成测试", "paper", "p1", None)
        submission = await repository.submit_chat_message(
            session.id,
            "u1",
            "这篇论文的方法是什么？",
            "integration-client",
            "integration-hash",
            {
                "type": "paper",
                "paper_ids": ["p1"],
                "web_enabled": False,
                "client_context": {"paper_id": "p1"},
                "harness": {
                    "context_engine_enabled": True,
                    "skills_enabled": True,
                    "function_tools_enabled": True,
                },
            },
        )
        assert submission is not None
        token = await repository.claim_agent_run_job(submission.run.id)
        assert token is not None
        retriever = FakeRetriever()
        planner = SequencePlanner(
            PlannerDecision(
                (ToolCallRequest("integration-call", "search_current_paper", {"query": "方法"}),)
            ),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(repository, retriever, UnusedRouter(), planner=planner)
        graph = EvidenceGraph()
        await execute_agent_run(
            repository,
            graph,
            submission.run.id,
            token,
            answer_quality_policy=AnswerQualityPolicy(),
            harness_config=replace(
                settings,
                context_engine_enabled=True,
                skills_enabled=True,
                function_tools_enabled=True,
            ),
            skill_registry=SkillRegistry.default(),
            function_tool_harness=harness,
        )

        assert graph.initial is not None and graph.initial["tool_mode_active"] is True
        assert len(retriever.requests) == 1
        run = await repository.get_agent_run(submission.run.id)
        assert run is not None and run.status == "completed"
        assert run.harness_trace["function_calling"] == "native"
        assert run.tool_steps == 1

    asyncio.run(scenario())


def test_rejected_function_tool_falls_back_to_legacy_retrieval_in_agent_run() -> None:
    class FallbackGraph:
        initial = None

        def __init__(self, retriever):
            self.retriever = retriever

        async def ainvoke(self, initial, _config):
            self.initial = initial
            evidence = list(initial.get("pre_retrieved_evidence", []))
            if not initial.get("tool_mode_active", False):
                evidence = await self.retriever(
                    type(
                        "Request",
                        (),
                        {
                            "user_id": initial["user_id"],
                            "paper_ids": initial["selected_paper_ids"],
                        },
                    )()
                )
            return {
                "status": "completed",
                "answer": "兼容检索仍返回可信证据 [chunk:p1:p2:c0]。",
                "retrieved_evidence": evidence,
                "citations": [CitationClaim("p1:p2:c0", "p1", 2)],
                "evidence_quality": {
                    "grade": "sufficient",
                    "answer_support_grade": "supported",
                    "answer_support_confidence": 1.0,
                    "reason_code": "answer_supported",
                },
                "tool_steps": initial["tool_steps"],
                "stage_timings_ms": initial["stage_timings_ms"],
            }

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        session = await repository.create_chat_session("u1", "工具降级", "paper", "p1", None)
        submission = await repository.submit_chat_message(
            session.id,
            "u1",
            "解释论文方法",
            "fallback-client",
            "fallback-hash",
            {
                "type": "paper",
                "paper_ids": ["p1"],
                "web_enabled": False,
                "client_context": {"paper_id": "p1"},
                "harness": {
                    "context_engine_enabled": True,
                    "skills_enabled": True,
                    "function_tools_enabled": True,
                },
            },
        )
        assert submission is not None
        token = await repository.claim_agent_run_job(submission.run.id)
        assert token is not None
        retriever = FakeRetriever()
        harness = FunctionToolHarness(
            repository,
            retriever,
            UnusedRouter(),
            planner=SequencePlanner(
                PlannerDecision((ToolCallRequest("rejected", "unknown_tool", {}),)),
                PlannerDecision(),
            ),
        )
        graph = FallbackGraph(retriever)

        await execute_agent_run(
            repository,
            graph,
            submission.run.id,
            token,
            answer_quality_policy=AnswerQualityPolicy(),
            harness_config=replace(
                settings,
                context_engine_enabled=True,
                skills_enabled=True,
                function_tools_enabled=True,
            ),
            skill_registry=SkillRegistry.default(),
            function_tool_harness=harness,
        )

        assert graph.initial is not None
        assert graph.initial.get("tool_mode_active", False) is False
        assert graph.initial.get("pre_retrieved_evidence", []) == []
        assert [item["kind"] for item in graph.initial["tool_context_entries"]] == [
            "call",
            "result",
        ]
        assert "TOOL_NOT_ALLOWED" in graph.initial["tool_context_entries"][1]["content"]
        assert len(retriever.requests) == 1
        run = await repository.get_agent_run(submission.run.id)
        assert run is not None and run.status == "completed"
        assert run.harness_trace["native_function_calling_attempted"] is True
        assert run.harness_trace["tool_output_used"] is False
        assert run.harness_trace["function_fallback_reason"] == "tool_outputs_not_usable"
        assert next(iter(repository.agent_tool_calls.values())).status == "rejected"

    asyncio.run(scenario())


def test_verified_selection_is_forced_into_skill_context_and_evidence() -> None:
    class SelectionGraph:
        initial = None

        async def ainvoke(self, initial, _config):
            self.initial = initial
            evidence = list(initial["selection_evidence"])
            return {
                "status": "completed",
                "answer": "选文说明了服务端验证的方法 [chunk:p1:p2:c0]。",
                "retrieved_evidence": evidence,
                "citations": [CitationClaim("p1:p2:c0", "p1", 2)],
                "evidence_quality": {
                    "grade": "sufficient",
                    "answer_support_grade": "supported",
                    "answer_support_confidence": 1.0,
                    "reason_code": "answer_supported",
                },
                "tool_steps": initial["tool_steps"],
                "stage_timings_ms": initial["stage_timings_ms"],
            }

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        session = await repository.create_chat_session("u1", "选文解释", "paper", "p1", None)
        selected = "这是服务端验证后的论文证据。"
        submission = await repository.submit_chat_message(
            session.id,
            "u1",
            "这些讲了什么？",
            "selection-client",
            "selection-hash",
            {
                "type": "paper",
                "paper_ids": ["p1"],
                "web_enabled": False,
                "client_context": {
                    "paper_id": "p1",
                    "physical_page": 2,
                    "selected_text": selected,
                    "selected_text_hash": "trusted-by-api",
                },
                "harness": {
                    "context_engine_enabled": True,
                    "skills_enabled": True,
                    "function_tools_enabled": False,
                },
            },
        )
        assert submission is not None
        token = await repository.claim_agent_run_job(submission.run.id)
        assert token is not None
        retriever = FakeRetriever()
        harness = FunctionToolHarness(
            repository, retriever, UnusedRouter(), planner=SequencePlanner()
        )
        graph = SelectionGraph()

        await execute_agent_run(
            repository,
            graph,
            submission.run.id,
            token,
            answer_quality_policy=AnswerQualityPolicy(),
            harness_config=replace(
                settings,
                context_engine_enabled=True,
                skills_enabled=True,
                function_tools_enabled=False,
            ),
            skill_registry=SkillRegistry.default(),
            function_tool_harness=harness,
        )

        assert graph.initial is not None
        assert graph.initial["selected_skill"] == "paper_qa"
        assert graph.initial["resolved_references"]["selected_text"] == selected
        assert graph.initial["selection_evidence"][0].physical_page == 2
        assert retriever.requests[0].query == selected
        run = await repository.get_agent_run(submission.run.id)
        assert run is not None and run.status == "completed"
        assert run.context_snapshot["resolved_references"]["selected_text"] == selected
        assert run.harness_trace["skill_route_source"] == "verified_selection_override"

    asyncio.run(scenario())
