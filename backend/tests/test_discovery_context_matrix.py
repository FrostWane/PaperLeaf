from __future__ import annotations

import pytest

from paperleaf_api.agent.context import resolve_context
from paperleaf_api.agent.discovery_policy import academic_source_policy

COUNT_CASES = (
    ("5 篇", 5),
    ("五篇", 5),
    ("five papers", 5),
)
SOURCE_CASES = (
    ("使用 OpenAlex", {"mcp__academic__search_openalex"}, set()),
    (
        "不要使用 OpenAlex",
        set(),
        {"mcp__academic__search_openalex"},
    ),
    (
        "使用 Semantic Scholar",
        {"mcp__academic__search_semantic_scholar"},
        set(),
    ),
    (
        "不要 OpenAlex，改用 Semantic Scholar",
        {"mcp__academic__search_semantic_scholar"},
        {"mcp__academic__search_openalex"},
    ),
)
FOLLOWUP_CASES = (
    ("有没有 2026 年的", 5, 2026),
    ("换一批三篇，限定 2025 年", 3, 2025),
    ("再推荐 five papers，限定 2024 年", 5, 2024),
)


@pytest.mark.parametrize(("count_text", "initial_count"), COUNT_CASES)
@pytest.mark.parametrize(("source_text", "requested", "denied"), SOURCE_CASES)
@pytest.mark.parametrize(("followup", "final_count", "year"), FOLLOWUP_CASES)
def test_discovery_followup_combination_matrix_preserves_constraints(
    count_text: str,
    initial_count: int,
    source_text: str,
    requested: set[str],
    denied: set[str],
    followup: str,
    final_count: int,
    year: int,
) -> None:
    previous = (
        f"请根据当前集合主题联网推荐 {count_text} 尚未入库的相关论文，{source_text}。"
    )
    result = resolve_context(
        followup,
        {"collection_id": "collection-1", "collection_title": "跨领域测试集合"},
        [
            {"role": "user", "content": previous},
            {"role": "assistant", "content": "已返回候选论文。"},
        ],
        session_type="collection",
    )

    assert result.needs_clarification is False
    task = result.references["active_task"]
    assert initial_count == 5
    assert task["requested_count"] == final_count
    assert task["year_from"] == year
    assert task["year_to"] == year
    assert set(task.get("requested_sources", [])) == requested
    assert set(task.get("denied_sources", [])) == denied
    resolved_policy = academic_source_policy(result.resolved_query)
    assert resolved_policy.requested_tools == frozenset(requested)
    assert resolved_policy.denied_tools == frozenset(denied)


@pytest.mark.parametrize(
    ("previous_source", "followup", "requested", "denied"),
    [
        (
            "使用 OpenAlex",
            "2026 年的，别用 OpenAlex，改用 Semantic Scholar",
            {"mcp__academic__search_semantic_scholar"},
            {"mcp__academic__search_openalex"},
        ),
        (
            "不要使用 OpenAlex",
            "2026 年的，这次改用 OpenAlex",
            {"mcp__academic__search_openalex"},
            set(),
        ),
    ],
)
def test_discovery_followup_can_explicitly_replace_previous_source_policy(
    previous_source: str,
    followup: str,
    requested: set[str],
    denied: set[str],
) -> None:
    result = resolve_context(
        followup,
        {"collection_id": "collection-1"},
        [
            {
                "role": "user",
                "content": f"联网推荐五篇尚未入库的论文，{previous_source}。",
            }
        ],
        session_type="collection",
    )

    task = result.references["active_task"]
    assert set(task.get("requested_sources", [])) == requested
    assert set(task.get("denied_sources", [])) == denied
