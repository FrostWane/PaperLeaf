"""确定性的上下文预算与会话压缩。

摘要只帮助模型延续会话，绝不具备论文证据资格。原始消息仍永久保存在数据库中。
"""

from __future__ import annotations

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
    for pair in pairs[:-keep_complete] if keep_complete else pairs:
        if len(pair) != 2:
            continue
        result = pair[1]
        content = str(result.get("content", ""))
        if estimate_tokens(content) <= preview_tokens:
            continue
        limit = max(200, preview_tokens * 2)
        result["content"] = content[:limit] + "…"
        result["compacted"] = True
    return [entry for pair in pairs for entry in pair]
