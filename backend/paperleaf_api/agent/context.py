"""构建可审计的阅读上下文，并以确定性规则解析常见中文指代。"""

from __future__ import annotations

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
    "这个结论",
    "前者",
    "后者",
    "那药物",
    "为什么这样",
    "为什么这么",
)
_SELECTION_MARKERS = ("这里", "这一页", "这句话", "这段", "这个数字", "这个公式")
_NEEDS_SUBJECT_MARKERS = ("为什么这样", "为什么这么", "这个结果", "这个结论")
_PAIR_MARKERS = ("前者", "后者")


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
    if not any(marker in original for marker in _REFERENCE_MARKERS):
        return ContextResolution(original, original, {}, 1.0, ("explicit_query",))

    paper_title = str(context.get("paper_title", "")).strip()
    paper_id = str(context.get("paper_id", "")).strip()
    page = context.get("physical_page")
    selected = str(context.get("selected_text", "")).strip()
    previous = _previous_user_text(messages, original)
    references: dict[str, Any] = {}
    sources: list[str] = []

    if paper_id:
        references["paper_id"] = paper_id
        references["paper_title"] = paper_title or "当前论文"
        sources.append("current_paper")
    if page is not None:
        references["physical_page"] = int(page)
        sources.append("current_page")
    if selected:
        references["selected_text"] = selected[:1200]
        sources.append("selected_text")
    if previous:
        references["previous_user_topic"] = previous
        sources.append("recent_entity")

    requires_selection = any(marker in original for marker in _SELECTION_MARKERS)
    requires_subject = any(marker in original for marker in _NEEDS_SUBJECT_MARKERS)
    requires_pair = any(marker in original for marker in _PAIR_MARKERS)
    lacks_anchor = not paper_id and not selected and not previous
    ambiguous = (
        lacks_anchor
        or (requires_selection and not selected and page is None)
        or (requires_subject and not selected and not previous)
        or (requires_pair and not _has_pair(previous))
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
    if page is not None:
        qualifiers.append(f"当前物理页：第 {page} 页")
    if selected:
        qualifiers.append(f"当前选中原文：{selected[:1200]}")
    if previous:
        qualifiers.append(f"上一轮讨论：{previous}")
    resolved = original + ("\n\n[已验证阅读上下文]\n" + "\n".join(qualifiers) if qualifiers else "")
    confidence = 0.97 if selected else 0.9 if paper_id and previous else 0.84 if paper_id else 0.72
    return ContextResolution(original, resolved, references, confidence, tuple(sources))
