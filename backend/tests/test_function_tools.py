from __future__ import annotations

import asyncio
from dataclasses import replace

from paperleaf_api.agent.function_tools import (
    FunctionToolHarness,
    PlannerDecision,
    ToolCallRequest,
    ToolExecutionContext,
)
from paperleaf_api.agent.skills import SkillRegistry
from paperleaf_api.agent_execution import execute_agent_run
from paperleaf_api.config import settings
from paperleaf_api.rag.answer_quality import AnswerQualityPolicy
from paperleaf_api.rag.citations import CitationClaim, Evidence
from paperleaf_api.repository import MemoryRepository, PaperArtifactRecord


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


class UnusedRouter:
    pass


class SelectingPlanner(SequencePlanner):
    def __init__(self, selected: str) -> None:
        super().__init__()
        self.selected = selected

    async def select_skill(self, **_kwargs) -> str:
        return self.selected


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
        harness = FunctionToolHarness(
            repository, retriever, UnusedRouter(), planner=planner
        )
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
            PlannerDecision(
                (ToolCallRequest("fixed-call", "search_library", {"query": "方法"}),)
            ),
            PlannerDecision(),
        )
        harness = FunctionToolHarness(
            repository, retriever, UnusedRouter(), planner=planner
        )
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
        harness = FunctionToolHarness(
            repository, FakeRetriever(), UnusedRouter(), planner=planner
        )
        result = await harness.run("读取第一页", await _context(repository, "paper_qa"))

        assert result.evidence == []
        record = next(iter(repository.agent_tool_calls.values()))
        assert record.status == "failed"
        assert record.error_code == "TOOL_PERMISSION_DENIED"

    asyncio.run(scenario())


def test_unknown_tool_is_rejected_and_parallel_batch_is_capped_at_three() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("secret")
        planner = SequencePlanner(
            PlannerDecision(
                tuple(
                    ToolCallRequest(f"call-{index}", f"unknown_{index}", {})
                    for index in range(4)
                )
            )
        )
        harness = FunctionToolHarness(
            repository, FakeRetriever(), UnusedRouter(), planner=planner
        )
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
        harness = FunctionToolHarness(
            repository, FakeRetriever(), UnusedRouter(), planner=planner
        )
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
        rejected, rejected_error = await harness.resume_confirmed_action(
            "u1", action, "reject"
        )
        assert "取消" in rejected and rejected_error is None and imported == []

        approved, approved_error = await harness.resume_confirmed_action(
            "u1", action, "approve"
        )
        assert "确认后的论文" in approved and approved_error is None
        assert imported == [("u1", "2601.00001")]

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
            PlannerDecision(
                (ToolCallRequest("slow-1", "search_library", {"query": "方法"}),)
            ),
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
        harness = FunctionToolHarness(
            repository, FakeRetriever(), UnusedRouter(), planner=planner
        )
        result = await harness.run(
            "概括论文", await _context(repository, "summarize_paper")
        )

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
        harness = FunctionToolHarness(
            repository, FakeRetriever(), UnusedRouter(), planner=planner
        )
        result = await harness.run("方法", await _context(repository, "paper_qa"))

        assert result.provider_supported is False
        assert result.fallback_reason == "provider_without_native_function_calling"
        assert repository.agent_tool_calls == {}

    asyncio.run(scenario())


def test_function_tool_evidence_is_reused_by_agent_graph_without_duplicate_retrieval() -> None:
    class EvidenceGraph:
        initial = None

        async def ainvoke(self, initial, _config):
            self.initial = initial
            evidence = list(initial["pre_retrieved_evidence"])
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
        harness = FunctionToolHarness(
            repository, retriever, UnusedRouter(), planner=planner
        )
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
