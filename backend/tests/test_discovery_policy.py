from __future__ import annotations

import pytest

from paperleaf_api.agent.discovery_policy import (
    academic_source_policy,
    requested_paper_count,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("推荐 5 篇论文", 5),
        ("推荐五篇论文", 5),
        ("五篇就可以", 5),
        ("recommend five papers", 5),
        ("find ten papers", 10),
        ("列出 20 篇", 10),
        ("没有表达数量", None),
    ],
)
def test_requested_paper_count_supports_chinese_english_and_arabic(
    text: str, expected: int | None
) -> None:
    assert requested_paper_count(text) == expected


@pytest.mark.parametrize(
    ("text", "requested", "denied"),
    [
        (
            "请使用 OpenAlex 搜索",
            {"mcp__academic__search_openalex"},
            set(),
        ),
        (
            "不要使用 OpenAlex",
            set(),
            {"mcp__academic__search_openalex"},
        ),
        (
            "不要 OpenAlex，改用 Semantic Scholar",
            {"mcp__academic__search_semantic_scholar"},
            {"mcp__academic__search_openalex"},
        ),
        (
            "Use Semantic Scholar without OpenAlex",
            {"mcp__academic__search_semantic_scholar"},
            {"mcp__academic__search_openalex"},
        ),
        (
            "排除 arXiv 和 OpenAlex",
            set(),
            {"search_arxiv", "mcp__academic__search_openalex"},
        ),
        (
            "只使用 OpenAlex 推荐论文",
            {"mcp__academic__search_openalex"},
            {"search_arxiv", "mcp__academic__search_semantic_scholar"},
        ),
        (
            "only use Semantic Scholar",
            {"mcp__academic__search_semantic_scholar"},
            {"search_arxiv", "mcp__academic__search_openalex"},
        ),
    ],
)
def test_academic_source_policy_understands_positive_and_negative_semantics(
    text: str, requested: set[str], denied: set[str]
) -> None:
    policy = academic_source_policy(text)
    assert policy.requested_tools == frozenset(requested)
    assert policy.denied_tools == frozenset(denied)
