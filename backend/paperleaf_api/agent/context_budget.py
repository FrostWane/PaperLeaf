"""确定性的上下文预算与会话压缩。

摘要只帮助模型延续会话，绝不具备论文证据资格。原始消息仍永久保存在数据库中。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

SUMMARY_VERSION = 1
_CJK = re.compile(r"[\u3400-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9_]+|[^\s]")
_CITATION = re.compile(r"\s*\[chunk:[^\]]+\]")


def estimate_tokens(value: str) -> int:
    """无需模型 tokenizer 的稳定保守估算，中英文混排也可复现。"""

    if not value:
        return 0
    cjk_count = len(_CJK.findall(value))
    non_cjk = _CJK.sub("", value)
    latin_tokens = sum(max(1, (len(part) + 3) // 4) for part in _WORD.findall(non_cjk))
    return max(1, cjk_count + latin_tokens)


@dataclass(frozen=True)
class ContextBudget:
    model_window: int
    safety: int
    system: int
    reading: int
    skill: int
    recent_messages: int
    summary_memory: int
    evidence_tools: int
    output: int
    input_limit: int
    compact_at: int
    hard_limit: int

    def as_dict(self) -> dict[str, int]:
        return {
            "model_window": self.model_window,
            "safety": self.safety,
            "system": self.system,
            "reading": self.reading,
            "skill": self.skill,
            "recent_messages": self.recent_messages,
            "summary_memory": self.summary_memory,
            "evidence_tools": self.evidence_tools,
            "output": self.output,
            "input_limit": self.input_limit,
            "compact_at": self.compact_at,
            "hard_limit": self.hard_limit,
        }


def allocate_context_budget(
    model_window: int,
    *,
    safety_ratio: float = 0.10,
    compact_ratio: float = 0.70,
    hard_limit_ratio: float = 0.85,
) -> ContextBudget:
    window = max(2048, model_window)
    safety = round(window * safety_ratio)
    usable = window - safety
    values = {
        "system": round(usable * 0.10),
        "reading": round(usable * 0.05),
        "skill": round(usable * 0.05),
        "recent_messages": round(usable * 0.20),
        "summary_memory": round(usable * 0.10),
        "evidence_tools": round(usable * 0.35),
        "output": round(usable * 0.15),
    }
    input_limit = usable - values["output"]
    return ContextBudget(
        model_window=window,
        safety=safety,
        **values,
        input_limit=input_limit,
        compact_at=round(input_limit * compact_ratio),
        hard_limit=round(input_limit * hard_limit_ratio),
    )


def _message_value(message: Any, field: str, default: Any = None) -> Any:
    return (
        message.get(field, default)
        if isinstance(message, dict)
        else getattr(message, field, default)
    )


def _unique(values: Sequence[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split()).strip()
        if not normalized or normalized in seen:
            continue
        result.append(normalized)
        seen.add(normalized)
        if len(result) >= limit:
            break
    return result


def summarize_messages(messages: Sequence[Any], existing: dict | None = None) -> dict:
    """生成受控结构摘要；不复制 Citation，也不把 assistant 内容当用户约束。"""

    previous = dict(existing or {})
    user_contents = [
        str(_message_value(item, "content", "")).strip()
        for item in messages
        if _message_value(item, "role") == "user"
        and str(_message_value(item, "content", "")).strip()
    ]
    assistant_contents = [
        _CITATION.sub("", str(_message_value(item, "content", ""))).strip()
        for item in messages
        if _message_value(item, "role") == "assistant"
        and str(_message_value(item, "content", "")).strip()
    ]
    constraints = list(previous.get("user_constraints", []))
    constraints.extend(
        content[:500]
        for content in user_contents
        if any(marker in content for marker in ("不要", "必须", "请用", "只要", "偏好", "以后请"))
    )
    unresolved = list(previous.get("unresolved_questions", []))
    unresolved.extend(content[:500] for content in user_contents if content.endswith(("?", "？")))
    conclusions = list(previous.get("conversation_conclusions", []))
    conclusions.extend(content[:500] for content in assistant_contents[-3:])
    summary = {
        "current_topic": user_contents[-1][:500]
        if user_contents
        else previous.get("current_topic", ""),
        "entities": _unique(list(previous.get("entities", [])), 20),
        "user_constraints": _unique(constraints, 20),
        "conversation_conclusions": _unique(conclusions, 12),
        "unresolved_questions": _unique(unresolved, 12),
        "papers_discussed": _unique(list(previous.get("papers_discussed", [])), 20),
        "pages_referenced": list(dict.fromkeys(previous.get("pages_referenced", [])))[:30],
        "tool_outcomes": list(previous.get("tool_outcomes", []))[-10:],
    }
    trim_order = (
        "conversation_conclusions",
        "tool_outcomes",
        "unresolved_questions",
        "pages_referenced",
        "papers_discussed",
        "entities",
        "user_constraints",
    )
    while estimate_tokens(str(summary)) > 1200:
        changed = False
        for key in trim_order:
            values = summary.get(key)
            if isinstance(values, list) and values:
                values.pop(0)
                changed = True
                break
        if not changed:
            summary["current_topic"] = str(summary.get("current_topic", ""))[:200]
            break
    return summary


@dataclass(frozen=True)
class CompactionResult:
    recent_messages: list[dict[str, str]]
    summary: dict
    compacted_through_message_id: str | None
    before_tokens: int
    after_tokens: int
    compacted: bool


@dataclass(frozen=True)
class ContextEnvelope:
    messages: list[dict[str, Any]]
    evidence: list[Any]
    tool_entries: list[dict[str, Any]]
    usage: dict[str, Any]
    exceeded: bool


def compact_conversation(
    messages: Sequence[Any],
    *,
    existing_summary: dict | None,
    keep_recent_turns: int,
    compact_at_tokens: int,
) -> CompactionResult:
    normalized = [
        {
            "id": str(_message_value(item, "id", "")),
            "role": str(_message_value(item, "role", "")),
            "content": str(_message_value(item, "content", "")),
        }
        for item in messages
        if str(_message_value(item, "content", "")).strip()
    ]
    before = sum(estimate_tokens(item["content"]) for item in normalized)
    keep_count = max(2, keep_recent_turns * 2)
    should_compact = before > compact_at_tokens and len(normalized) > keep_count
    if not should_compact:
        recent = normalized
        summary = dict(existing_summary or {})
        compacted_through = None
    else:
        older = normalized[:-keep_count]
        recent = normalized[-keep_count:]
        summary = summarize_messages(older, existing_summary)
        compacted_through = older[-1]["id"] or None
    summary_tokens = estimate_tokens(str(summary))
    after = sum(estimate_tokens(item["content"]) for item in recent) + summary_tokens
    return CompactionResult(
        recent_messages=[{"role": item["role"], "content": item["content"]} for item in recent],
        summary=summary,
        compacted_through_message_id=compacted_through,
        before_tokens=before,
        after_tokens=after,
        compacted=should_compact,
    )


def compress_tool_results(
    entries: Sequence[dict[str, Any]], *, keep_complete: int = 3, preview_tokens: int = 800
) -> list[dict[str, Any]]:
    """保持 call/result 配对；旧大结果只留下结构预览和 Artifact 引用。"""

    pairs: list[list[dict[str, Any]]] = []
    pending: dict[str, dict[str, Any]] = {}
    for entry in entries:
        call_id = str(entry.get("tool_call_id", ""))
        if entry.get("kind") == "call":
            pending[call_id] = dict(entry)
        elif entry.get("kind") == "result" and call_id in pending:
            pairs.append([pending.pop(call_id), dict(entry)])
    pairs.extend([[call]] for call in pending.values())
    def compact_content(content: str, token_limit: int) -> str:
        if estimate_tokens(content) <= token_limit:
            return content
        try:
            payload = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            limit = max(60, token_limit - 1)
            return content[:limit] + "…"
        if not isinstance(payload, dict):
            limit = max(120, token_limit * 2)
            return json.dumps(
                {"compacted": True, "preview": str(payload)[:limit]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        compact = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "items",
                "results",
                "abstract",
                "excerpt",
                "raw",
                "content",
            }
        }
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raw_items = payload.get("results")
        if isinstance(raw_items, list):
            compact["item_count"] = len(raw_items)
            compact["items"] = [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in {
                        "external_id",
                        "arxiv_id",
                        "title",
                        "paper_title",
                        "year",
                        "publication",
                        "doi",
                        "source",
                        "physical_page",
                    }
                }
                for item in raw_items[:3]
                if isinstance(item, dict)
            ]
        compact["compacted"] = True
        rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)
        while estimate_tokens(rendered) > token_limit and compact.get("items"):
            compact["items"].pop()
            rendered = json.dumps(
                compact, ensure_ascii=False, separators=(",", ":"), default=str
            )
        if estimate_tokens(rendered) > token_limit:
            rendered = json.dumps(
                {
                    "compacted": True,
                    "tool": compact.get("tool"),
                    "status": compact.get("status"),
                    "error_code": compact.get("error_code"),
                    "item_count": compact.get("item_count"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return rendered

    complete_from = max(0, len(pairs) - keep_complete) if keep_complete else len(pairs)
    for index, pair in enumerate(pairs):
        if len(pair) != 2:
            continue
        call = pair[0]
        call_content = str(call.get("content", ""))
        if estimate_tokens(call_content) > 600:
            call["content"] = compact_content(call_content, 600)
            call["compacted"] = True
        result = pair[1]
        content = str(result.get("content", ""))
        token_limit = 2400 if index >= complete_from else preview_tokens
        if estimate_tokens(content) <= token_limit:
            continue
        result["content"] = compact_content(content, token_limit)
        result["compacted"] = True
    return [entry for pair in pairs for entry in pair]


def _envelope_tokens(
    query: str,
    messages: Sequence[dict[str, Any]],
    evidence: Sequence[Any],
    tool_entries: Sequence[dict[str, Any]],
    *,
    system_reserve: int,
) -> dict[str, int]:
    return {
        "system": system_reserve,
        "query": estimate_tokens(query),
        "messages": sum(estimate_tokens(str(item.get("content", ""))) for item in messages),
        "evidence": sum(estimate_tokens(str(getattr(item, "text", ""))) for item in evidence),
        "tools": sum(estimate_tokens(str(item.get("content", ""))) for item in tool_entries),
    }


def enforce_context_envelope(
    *,
    query: str,
    messages: Sequence[dict[str, Any]],
    evidence: Sequence[Any],
    tool_entries: Sequence[dict[str, Any]],
    hard_limit: int,
    protected_evidence_ids: set[str] | None = None,
    system_reserve: int = 1200,
) -> ContextEnvelope:
    """在最后一次模型调用前执行确定性硬门禁。

    当前问题与已验证选文位于 ``query``，因此不会被裁剪。Tool Call/Result
    先按 call_id 原子压缩；证据只从低优先级尾部移除，受保护选文证据不删除。
    """

    protected = set(protected_evidence_ids or set())
    kept_messages = [dict(item) for item in messages]
    kept_evidence = list(evidence)
    kept_tools = compress_tool_results(tool_entries, keep_complete=3, preview_tokens=800)
    actions: list[str] = []
    dropped_messages = 0
    dropped_evidence = 0
    dropped_tool_pairs = 0
    original_tool_tokens = sum(
        estimate_tokens(str(item.get("content", ""))) for item in tool_entries
    )
    compacted_tool_tokens = sum(
        estimate_tokens(str(item.get("content", ""))) for item in kept_tools
    )
    if compacted_tool_tokens < original_tool_tokens:
        actions.append("compress_old_tool_results")

    def usage() -> dict[str, int]:
        return _envelope_tokens(
            query,
            kept_messages,
            kept_evidence,
            kept_tools,
            system_reserve=system_reserve,
        )

    current = usage()
    while sum(current.values()) > hard_limit:
        removable_index = next(
            (
                index
                for index, item in enumerate(kept_messages)
                if str(item.get("role", "")) in {"user", "assistant", "external_tool"}
            ),
            None,
        )
        if removable_index is None:
            break
        kept_messages.pop(removable_index)
        dropped_messages += 1
        current = usage()
    if dropped_messages:
        actions.append("drop_old_messages")

    while sum(current.values()) > hard_limit:
        removable_index = next(
            (
                index
                for index in range(len(kept_evidence) - 1, -1, -1)
                if str(getattr(kept_evidence[index], "chunk_id", "")) not in protected
            ),
            None,
        )
        if removable_index is None:
            break
        kept_evidence.pop(removable_index)
        dropped_evidence += 1
        current = usage()
    if dropped_evidence:
        actions.append("drop_low_priority_evidence")

    # 紧急压缩仅执行一次：摘要/记忆或 Skill 仍可能很大，但不能删除其角色边界。
    if sum(current.values()) > hard_limit:
        emergency_tools = compress_tool_results(
            kept_tools, keep_complete=0, preview_tokens=100
        )
        if emergency_tools != kept_tools:
            kept_tools = emergency_tools
            actions.append("emergency_compress_tool_results")
            current = usage()
    if sum(current.values()) > hard_limit:
        changed = False
        emergency: list[dict[str, Any]] = []
        for item in kept_messages:
            value = dict(item)
            content = str(value.get("content", ""))
            if value.get("role") in {"context", "skill"} and estimate_tokens(content) > 800:
                value["content"] = content[:2400] + "…"
                changed = True
            emergency.append(value)
        kept_messages = emergency
        if changed:
            actions.append("emergency_compaction")
        current = usage()

    # 仍超限时只能整对移除最旧 Tool Call/Result，绝不留下孤立半对。
    while sum(current.values()) > hard_limit and len(kept_tools) >= 2:
        oldest_call_id = str(kept_tools[0].get("tool_call_id", ""))
        before = len(kept_tools)
        kept_tools = [
            item
            for item in kept_tools
            if str(item.get("tool_call_id", "")) != oldest_call_id
        ]
        if len(kept_tools) == before:
            break
        dropped_tool_pairs += 1
        current = usage()
    if dropped_tool_pairs:
        actions.append("drop_old_tool_pairs")

    total = sum(current.values())
    return ContextEnvelope(
        messages=kept_messages,
        evidence=kept_evidence,
        tool_entries=kept_tools,
        usage={
            **current,
            "final_input_tokens": total,
            "hard_limit": hard_limit,
            "dropped_messages": dropped_messages,
            "dropped_evidence": dropped_evidence,
            "dropped_tool_pairs": dropped_tool_pairs,
            "compression_actions": actions,
        },
        exceeded=total > hard_limit,
    )
