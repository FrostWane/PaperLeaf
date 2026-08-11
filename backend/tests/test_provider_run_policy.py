from __future__ import annotations

import asyncio

from paperleaf_api.agent.graph import build_agent_graph
from paperleaf_api.agent.provider_policy import (
    build_provider_run_policy,
    claim_provider_attempt,
    provider_can_run,
)
from paperleaf_api.agent.tools import ToolResult


class EmptyLibrary:
    async def __call__(self, _request):
        return []


class CountingArxiv:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _request):
        self.calls += 1
        return ToolResult(data=[], audit_summary="empty")


async def _answer_without_evidence(_query, _evidence, *_args):
    return "当前指定来源没有返回可核验结果。", []


def _state(policy):
    return {
        "run_id": "run-1",
        "session_id": "session-1",
        "user_id": "user-1",
        "query": "只使用 Semantic Scholar 推荐五篇论文",
        "messages": [],
        "selected_paper_ids": [],
        "web_enabled": True,
        "provider_policy": policy,
        "tool_steps": 0,
        "status": "pending",
    }


def test_exclusive_semantic_scholar_policy_blocks_legacy_arxiv_fallback() -> None:
    arxiv = CountingArxiv()
    policy = build_provider_run_policy(
        {"requested_sources": ["mcp__academic__search_semantic_scholar"]}
    )
    graph = build_agent_graph(
        EmptyLibrary(),
        _answer_without_evidence,
        arxiv_search=arxiv,
        use_langgraph=False,
    )

    result = asyncio.run(graph.ainvoke(_state(policy)))

    assert arxiv.calls == 0
    assert result["status"] == "completed"
    assert provider_can_run(result["provider_policy"], "arxiv") == (
        False,
        "source_excluded_by_user",
    )


def test_provider_attempt_in_function_harness_blocks_same_run_graph_retry() -> None:
    arxiv = CountingArxiv()
    policy = build_provider_run_policy({"requested_sources": ["search_arxiv"]})
    claimed, reason = claim_provider_attempt(policy, "arxiv", tool_name="search_arxiv")
    assert claimed is True and reason is None
    graph = build_agent_graph(
        EmptyLibrary(),
        _answer_without_evidence,
        arxiv_search=arxiv,
        use_langgraph=False,
    )

    result = asyncio.run(graph.ainvoke(_state(policy)))

    assert arxiv.calls == 0
    assert result["provider_policy"]["attempted"] == {"arxiv": 1, "library": 1}
    assert provider_can_run(result["provider_policy"], "arxiv") == (
        False,
        "provider_budget_exhausted",
    )
