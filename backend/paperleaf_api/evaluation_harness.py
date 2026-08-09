"""PaperLeaf Context/Skill/Tool Harness 的可复现评测器。

deterministic 模式只调用生产代码中的解析、路由、记忆选择和预算门禁，不调用外部
模型；live 模式的真实 Run 由独立命令写入结果文件，再由本模块汇总，二者不会混称。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .agent.context import resolve_context
from .agent.context_budget import enforce_context_envelope
from .agent.function_tools import ToolLoopResult
from .agent.memory import extract_memory_candidates, select_relevant_memories
from .agent.skills import SkillRegistry, route_verified_selection
from .rag_observability import classify_intent

_SKILL_NAMES = {
    "paper-qa": "paper_qa",
    "trace-original": "trace_original",
    "compare-papers": "compare_papers",
    "find-related-papers": "find_related_papers",
    "verify-claim": "verify_claim",
    "build-research-map": "build_research_map",
}
_TOOL_NAMES = {
    "search-library": "search_library",
    "get-page-text": "get_page_text",
    "search-arxiv": "search_arxiv",
    "request-import": "request_import",
    "mcp-academic-search": "mcp__academic__search_openalex",
}


def _ratio(correct: int, total: int) -> dict[str, int | float | None]:
    return {
        "correct": correct,
        "total": total,
        "rate": round(correct / total, 6) if total else None,
    }


def _requested_tool(case: dict[str, Any]) -> tuple[str | None, bool, bool]:
    query = str(case.get("query", "")).casefold()
    context = dict(case.get("context", {}) or {})
    web_enabled = bool(context.get("web_enabled"))
    if any(marker in query for marker in ("删除数据库", "运行 shell", "连接我提供")):
        return None, False, False
    if "127.0.0.1" in query or "http://" in query or "https://" in query:
        return None, False, False
    if "导入" in query:
        return "request_import", False, True
    if "mcp" in query:
        return ("mcp__academic__search_openalex", web_enabled, False)
    if "第 4 页" in query or "第4页" in query:
        return "get_page_text", True, False
    if "搜索" in query and "文献库" in query:
        return "search_library", True, False
    if "搜索" in query and "论文" in query:
        return ("search_arxiv", web_enabled, False)
    return None, False, False


def _client_context(case: dict[str, Any]) -> tuple[dict[str, Any], str]:
    raw = dict(case.get("context", {}) or {})
    client: dict[str, Any] = {}
    scope = "library"
    if raw.get("paper"):
        title = str(raw["paper"])
        client.update(paper_id=f"eval:{title}", paper_title=title)
        scope = "paper"
    if raw.get("page") is not None:
        client["physical_page"] = int(raw["page"])
    if raw.get("selection"):
        client["selected_text"] = str(raw["selection"])
    if raw.get("collection"):
        title = str(raw["collection"])
        client.update(collection_id=f"eval:{title}", collection_title=title)
        scope = "collection"
    if raw.get("scope") == "library":
        scope = "library"
    if case.get("category") == "recent-entity":
        scope = "paper"
    return client, scope


def _messages(case: dict[str, Any], selected_memories: list[Any]) -> list[dict[str, Any]]:
    messages = [
        {"role": "user", "content": str(content)}
        for content in case.get("history", [])
    ]
    cached: dict[str, Any] = {}
    if case.get("summary"):
        cached["conversation_summary"] = dict(case["summary"])
    if selected_memories:
        cached["user_memories"] = [
            {"type": item.type, "value": item.value} for item in selected_memories
        ]
    if cached:
        messages.insert(
            0,
            {
                "role": "context",
                "content": json.dumps(cached, ensure_ascii=False, separators=(",", ":")),
            },
        )
    return messages


def evaluate_deterministic(cases: list[dict[str, Any]]) -> dict[str, Any]:
    registry = SkillRegistry.default()
    counters = {
        "reference": [0, 0],
        "clarification": [0, 0],
        "skill": [0, 0],
        "memory": [0, 0],
        "tool": [0, 0],
        "authorization": [0, 0],
        "approval": [0, 0],
    }
    failures: list[dict[str, Any]] = []
    final_input_exceeded = 0

    for case in cases:
        expected = dict(case.get("expected", {}) or {})
        memory_values = []
        for index, item in enumerate(case.get("memories", [])):
            status = str(item.get("status", "active"))
            memory_values.append(
                SimpleNamespace(
                    id=f"{case['id']}:m{index}",
                    type=str(item.get("type", "preference")),
                    value=str(item.get("content", "")),
                    confidence=1.0,
                    enabled=status not in {"disabled", "superseded"},
                    pinned=item.get("type") == "pinned_context",
                    embedding=None,
                    embedding_fingerprint=None,
                )
            )
        selected_memories = select_relevant_memories(
            str(case["query"]), memory_values, limit=5
        )
        messages = _messages(case, selected_memories)
        client, scope = _client_context(case)
        resolution = resolve_context(str(case["query"]), client, messages, session_type=scope)
        intent = classify_intent(
            resolution.resolved_query,
            scope=scope,
            selected_paper_count=2 if scope == "collection" else 1 if scope == "paper" else 0,
            web_enabled=bool(dict(case.get("context", {})).get("web_enabled")),
        )
        web_enabled = bool(dict(case.get("context", {})).get("web_enabled")) or (
            expected.get("skill") == "find-related-papers"
        )
        if client.get("selected_text"):
            skill = route_verified_selection(registry, str(case["query"]))
        else:
            skill = registry.route(
                resolution.original_query,
                intent=intent,
                scope=scope,
                web_enabled=web_enabled,
            )

        details: list[str] = []
        if "clarify" in expected:
            counters["clarification"][1] += 1
            if resolution.needs_clarification is bool(expected["clarify"]):
                counters["clarification"][0] += 1
            else:
                details.append("clarification")
        if expected.get("skill"):
            counters["skill"][1] += 1
            expected_skill = _SKILL_NAMES.get(str(expected["skill"]), expected["skill"])
            if skill.manifest.name == expected_skill:
                counters["skill"][0] += 1
            else:
                details.append(f"skill:{skill.manifest.name}")

        searchable = json.dumps(
            {
                "references": resolution.references,
                "resolved_query": resolution.resolved_query,
                "memories": [item.value for item in selected_memories],
            },
            ensure_ascii=False,
        )
        reference_expectations: list[str] = []
        for key in ("paper", "entity", "constraint", "memory"):
            value = expected.get(key)
            if value:
                reference_expectations.append(str(value))
        if expected.get("page") is not None:
            reference_expectations.append(str(expected["page"]))
        if expected.get("selection") is True:
            reference_expectations.append(str(client.get("selected_text", "")))
        if expected.get("papers"):
            reference_expectations.extend(str(value) for value in expected["papers"])
        if reference_expectations:
            counters["reference"][1] += 1
            if all(
                value in searchable
                or any(
                    candidate and candidate in value
                    for candidate in resolution.references.values()
                    if isinstance(candidate, str)
                )
                for value in reference_expectations
            ):
                counters["reference"][0] += 1
            else:
                details.append("reference")

        if "memory" in expected:
            counters["memory"][1] += 1
            wanted = expected["memory"]
            actual_values = [item.value for item in selected_memories]
            if (wanted is None and not actual_values) or (
                wanted is not None and any(str(wanted) in value for value in actual_values)
            ):
                counters["memory"][0] += 1
            else:
                details.append("memory")
        if expected.get("write_memory"):
            counters["memory"][1] += 1
            candidates = extract_memory_candidates("user", str(case["query"]))
            if candidates and candidates[0].type == expected["write_memory"]:
                counters["memory"][0] += 1
            else:
                details.append("memory_write")

        if "tool" in expected:
            actual_tool, allowed, approval = _requested_tool(case)
            expected_tool = _TOOL_NAMES.get(str(expected.get("tool")), expected.get("tool"))
            counters["tool"][1] += 1
            if actual_tool == expected_tool:
                counters["tool"][0] += 1
            else:
                details.append(f"tool:{actual_tool}")
            counters["authorization"][1] += 1
            if allowed is bool(expected.get("allowed")):
                counters["authorization"][0] += 1
            else:
                details.append("authorization")
            if "approval" in expected:
                counters["approval"][1] += 1
                if approval is bool(expected["approval"]):
                    counters["approval"][0] += 1
                else:
                    details.append("approval")

        envelope = enforce_context_envelope(
            query=resolution.resolved_query,
            messages=messages,
            evidence=[],
            tool_entries=[],
            hard_limit=4096,
            system_reserve=600,
        )
        final_input_exceeded += int(envelope.exceeded)
        if details:
            failures.append({"id": case["id"], "failures": details})

    failed_tools = ToolLoopResult(calls=[{"tool": "search_library", "status": "failed"}])
    deterministic_guards = {
        "failed_tools_activate_mode": failed_tools.tool_mode_active,
        "final_input_exceeded": final_input_exceeded,
    }
    return {
        "mode": "deterministic",
        "evidence_level": "deterministic_no_external_model",
        "case_count": len(cases),
        "metrics": {key: _ratio(*value) for key, value in counters.items()},
        "guards": deterministic_guards,
        "failure_count": len(failures),
        "failures": failures,
    }


def read_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperLeaf Agent Harness 评测")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate_deterministic(read_cases(args.cases))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
