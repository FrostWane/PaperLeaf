from pathlib import Path

import pytest

from paperleaf_api.agent.skills import SkillRegistry, SkillRegistryError


def test_default_skill_registry_is_versioned_and_loads_only_catalog_on_start() -> None:
    registry = SkillRegistry.default()
    catalog = registry.catalog()

    assert {item["name"] for item in catalog} == {
        "paper_qa",
        "trace_original",
        "compare_papers",
        "find_related_papers",
        "verify_claim",
        "summarize_paper",
        "build_research_map",
    }
    versions = {item["name"]: item["version"] for item in catalog}
    assert versions == {
        "paper_qa": 1,
        "trace_original": 1,
        "compare_papers": 1,
        "find_related_papers": 3,
        "verify_claim": 1,
        "summarize_paper": 1,
        "build_research_map": 1,
    }
    assert all("allowed_tools" not in item and "instructions" not in item for item in catalog)
    selected = registry.get("trace_original")
    assert selected.manifest.allowed_tools == ["search_current_paper", "get_page_text"]
    assert "物理页" in selected.instructions


@pytest.mark.parametrize(
    ("query", "intent", "scope", "web_enabled", "expected"),
    [
        ("原文是怎么处理的？", "fact_lookup", "paper", False, "trace_original"),
        ("比较两篇论文的方法", "comparison", "collection", False, "compare_papers"),
        ("这个主张有证据支持吗？", "fact_lookup", "paper", False, "verify_claim"),
        ("画研究脑图", "structure", "paper", False, "build_research_map"),
        ("概括这篇论文", "summary", "paper", False, "summarize_paper"),
        ("搜索相关论文", "discovery", "library", True, "find_related_papers"),
        (
            "请通过 OpenAlex 查找与集合主题相关的近期公开论文",
            "comparison",
            "collection",
            True,
            "find_related_papers",
        ),
        (
            "请联网查找与当前集合研究主题相关的 arXiv 论文",
            "comparison",
            "collection",
            True,
            "find_related_papers",
        ),
        ("解释这个概念", "fact_lookup", "library", False, "paper_qa"),
    ],
)
def test_skill_routing_is_deterministic(
    query: str, intent: str, scope: str, web_enabled: bool, expected: str
) -> None:
    registry = SkillRegistry.default()
    first = registry.route(query, intent=intent, scope=scope, web_enabled=web_enabled)
    second = registry.route(query, intent=intent, scope=scope, web_enabled=web_enabled)
    expected_version = 3 if expected == "find_related_papers" else 1
    assert first.identity == second.identity == f"{expected}@{expected_version}"


def test_invalid_manifest_and_unknown_tool_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text(
        """---
name: bad_skill
version: 1
description: 这是一个长度足够但引用未知工具的测试 Skill
allowed_tools:
  - execute_shell
max_tool_steps: 1
requires_evidence: true
web_policy: disabled
approval_policy: none
---
这里是完整的 Skill 指令，绝对不应通过启动校验。
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillRegistryError, match="未知工具"):
        SkillRegistry.from_directory(tmp_path)


def test_internal_verified_context_label_does_not_route_to_claim_verification() -> None:
    registry = SkillRegistry.default()
    selected = registry.route(
        "这篇论文的核心方法是什么？\n\n[已验证阅读上下文]\n当前论文：DeepDTA",
        intent="method",
        scope="paper",
        web_enabled=False,
    )
    assert selected.manifest.name == "paper_qa"
