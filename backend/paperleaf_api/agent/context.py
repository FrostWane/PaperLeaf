"""构建可审计的阅读上下文，并以确定性规则解析常见中文指代。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

CONTEXT_VERSION = 1

_REFERENCE_MARKERS = (
    "原文",
    "这篇",
    "这项研究",
    "作者",
    "它",
    "这个方法",
    "这种方法",
    "这里",
    "这一页",
    "这句话",
    "这段",
    "这张表",
    "这个数字",
    "这个结果",
    "这些",
    "这个结论",
    "前者",
    "后者",
    "哪篇",
    "那药物",
    "为什么这样",
    "为什么这么",
    "验证一下",
)
_SELECTION_MARKERS = ("这里", "这一页", "这句话", "这段", "这个数字", "这个公式")
_NEEDS_SUBJECT_MARKERS = (
    "为什么这样",
    "为什么这么",
    "这个结果",
    "这个结论",
    "验证一下",
)
_PAIR_MARKERS = ("前者", "后者")
_FOLLOWUP_ENTITY = re.compile(r"^那\s*([^？?呢]{1,40})\s*呢?[？?]?$")


@dataclass(frozen=True)
class ContextResolution:
    original_query: str
    resolved_query: str
    references: dict[str, Any]
    confidence: float
    sources: tuple[str, ...]
    clarification_question: str | None = None

    @property
    def needs_clarification(self) -> bool:
        return self.clarification_question is not None

    def snapshot(self, client_context: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": CONTEXT_VERSION,
            "original_query": self.original_query,
            "resolved_query": self.resolved_query,
            "client_context": client_context,
            "resolved_references": self.references,
            "reference_confidence": self.confidence,
            "sources": list(self.sources),
            "needs_clarification": self.needs_clarification,
        }


def _previous_user_text(messages: list[dict[str, Any]], query: str) -> str:
    for item in reversed(messages):
        if str(item.get("role", "")) != "user":
            continue
        content = str(item.get("content", "")).strip()
        if content and content != query:
            return content[:1000]
    return ""


def _has_pair(text: str) -> bool:
    return bool(re.search(r"(?:和|与|、|versus|\bvs\.?\b)", text, re.IGNORECASE))


def _cached_context(messages: list[dict[str, Any]]) -> dict[str, Any]:
    for item in messages:
        if str(item.get("role", "")) != "context":
            continue
        try:
            value = json.loads(str(item.get("content", "")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _summary_anchor(payload: dict[str, Any], query: str) -> tuple[str, Any] | None:
    summary = payload.get("conversation_summary")
    if not isinstance(summary, dict):
        summary = {}
    entity_state = payload.get("entity_state")
    if not isinstance(entity_state, dict):
        entity_state = {}
    if "第" in query and "页" in query:
        match = re.search(r"第\s*(\d+)\s*页", query)
        if match:
            return "physical_page", int(match.group(1))
    if "第二篇" in query:
        papers = summary.get("papers_discussed")
        if isinstance(papers, list) and len(papers) >= 2:
            return "summary_entity", str(papers[1])
    if "这两篇" in query:
        papers = summary.get("papers_discussed")
        if isinstance(papers, list) and len(papers) >= 2:
            return "papers_discussed", [str(value) for value in papers[-2:]]
    if any(marker in query for marker in ("没回答", "未解决", "待验证", "还没解决")):
        unresolved = summary.get("unresolved_questions")
        if isinstance(unresolved, list) and unresolved:
            return "summary_entity", str(unresolved[-1])
    if "列出来" in query and isinstance(summary.get("unresolved_questions"), list):
        unresolved = summary["unresolved_questions"]
        if unresolved:
            return "summary_entity", "、".join(str(item) for item in unresolved[-5:])
    if "遵循" in query or "沿用" in query:
        constraints = summary.get("user_constraints")
        if isinstance(constraints, list) and constraints:
            return "summary_constraint", str(constraints[-1])
    if "检索" in query and "为什么" in query:
        outcomes = summary.get("tool_outcomes")
        if isinstance(outcomes, list) and outcomes:
            return "tool_outcome", str(outcomes[-1])
    if "最开始" in query or "继续" in query or "之前" in query or "上次" in query:
        entities = summary.get("entities")
        if isinstance(entities, dict) and entities:
            return "summary_entity", str(list(entities.values())[-1])
        if isinstance(entities, list) and entities:
            return "summary_entity", str(entities[-1])
        current = entity_state.get("discussion_entity") or summary.get("current_topic")
        if current:
            return "summary_entity", str(current)
    return None


def resolve_context(
    query: str,
    client_context: dict[str, Any] | None,
    messages: list[dict[str, Any]],
    *,
    session_type: str,
) -> ContextResolution:
    """解析“原文/它/这里”等常见追问；低置信度时返回澄清而不猜测。"""

    original = query.strip()
    context = dict(client_context or {})
    selected = str(context.get("selected_text", "")).strip()
    cached = _cached_context(messages)
    followup_entity = _FOLLOWUP_ENTITY.match(original)
    has_cached_anchor = bool(cached.get("conversation_summary") or cached.get("entity_state"))
    if (
        not selected
        and not has_cached_anchor
        and not followup_entity
        and not any(marker in original for marker in _REFERENCE_MARKERS)
    ):
        return ContextResolution(original, original, {}, 1.0, ("explicit_query",))

    paper_title = str(context.get("paper_title", "")).strip()
    paper_id = str(context.get("paper_id", "")).strip()
    collection_id = str(context.get("collection_id", "")).strip()
    collection_title = str(context.get("collection_title", "")).strip()
    page = context.get("physical_page")
    previous = _previous_user_text(messages, original)
    summary_anchor = _summary_anchor(cached, original)
    references: dict[str, Any] = {}
    sources: list[str] = []

    if paper_id:
        references["paper_id"] = paper_id
        references["paper_title"] = paper_title or "当前论文"
        sources.append("current_paper")
    if collection_id:
        references["collection_id"] = collection_id
        references["collection_title"] = collection_title
        sources.append("current_collection")
    if page is not None:
        references["physical_page"] = int(page)
        sources.append("current_page")
    if selected:
        references["selected_text"] = selected[:4000]
        sources.append("selected_text")
    if previous:
        references["previous_user_topic"] = previous
        sources.append("recent_entity")
    if followup_entity:
        references["discussion_entity"] = followup_entity.group(1).strip()
        sources.append("explicit_followup_entity")
    if summary_anchor:
        key, value = summary_anchor
        references[key] = value
        sources.append("conversation_summary")

    requires_selection = any(marker in original for marker in _SELECTION_MARKERS)
    requires_subject = any(marker in original for marker in _NEEDS_SUBJECT_MARKERS)
    requires_pair = any(marker in original for marker in _PAIR_MARKERS)
    generic_followup = bool(
        followup_entity and followup_entity.group(1).strip() in {"另一个", "另外一个", "那个"}
    )
    lacks_anchor = (
        not paper_id
        and not collection_id
        and not selected
        and not previous
        and not summary_anchor
    )
    ambiguous = (
        lacks_anchor
        or (requires_selection and not selected and page is None)
        or (requires_subject and not selected and not previous)
        or (requires_pair and not _has_pair(previous))
        or (generic_followup and not _has_pair(previous))
        or bool(re.fullmatch(r"哪篇更好[？?]?", original.strip()))
    )
    if session_type == "library" and not paper_id and not previous and "这篇" in original:
        ambiguous = True

    if ambiguous:
        return ContextResolution(
            original,
            original,
            references,
            0.35 if lacks_anchor else 0.5,
            tuple(sources),
            "我还不能确定你指的是哪篇论文、哪一页或哪个方法。请补充论文名称，"
            "或在阅读器中选中对应原文后再提问。",
        )

    qualifiers: list[str] = []
    if paper_id:
        qualifiers.append(f"当前论文：{paper_title or paper_id}")
    if collection_id:
        qualifiers.append(f"当前集合：{collection_title or collection_id}")
    if page is not None:
        qualifiers.append(f"当前物理页：第 {page} 页")
    if selected:
        qualifiers.append(f"本轮首要材料（已验证选文）：{selected[:4000]}")
    if previous:
        qualifiers.append(f"上一轮讨论：{previous}")
    if followup_entity:
        qualifiers.append(f"本轮追问对象：{followup_entity.group(1).strip()}")
    if summary_anchor:
        qualifiers.append(f"会话摘要锚点：{summary_anchor[1]}")
    resolved = original + ("\n\n[已验证阅读上下文]\n" + "\n".join(qualifiers) if qualifiers else "")
    confidence = (
        0.97
        if selected
        else 0.9
        if paper_id and previous
        else 0.84
        if paper_id or collection_id
        else 0.72
    )
    return ContextResolution(original, resolved, references, confidence, tuple(sources))
