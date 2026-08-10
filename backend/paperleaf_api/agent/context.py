"""构建可审计的阅读上下文，并以确定性规则解析常见中文指代。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .discovery_policy import academic_source_policy, requested_paper_count

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
_DISCOVERY_CONTINUATION_RE = re.compile(
    r"更近|更新|最新|近期|近年|今年|换一批|再(?:找|搜|推荐)|还有|"
    r"(?:19|20)\d{2}\s*年?",
    re.IGNORECASE,
)
_DISCOVERY_REQUEST_RE = re.compile(
    r"(?:联网|搜索|检索|查找|推荐).{0,40}(?:论文|文献|研究)|"
    r"(?:相关论文|相关文献|openalex|semantic\s+scholar|arxiv)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_TASK_SWITCH_RE = re.compile(
    r"解释|总结|概括|翻译|原文|方法|实验|结果|局限|结构图|脑图|"
    r"对比|比较",
    re.IGNORECASE,
)


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


def _previous_discovery_text(messages: list[dict[str, Any]], query: str) -> str:
    """找最近一次明确的论文发现任务，用于从旧版失败续问中恢复。"""

    for item in reversed(messages):
        if str(item.get("role", "")) != "user":
            continue
        content = str(item.get("content", "")).strip()
        if content and content != query and _DISCOVERY_REQUEST_RE.search(content):
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


def _discovery_task_context(
    query: str,
    previous: str,
    cached: dict[str, Any],
) -> dict[str, Any] | None:
    """识别“再新一点 / 2026 年的呢”这类对上一轮论文发现任务的续问。

    只继承任务名、数量、年份和“排除已入库”等可审计约束，不保存或推测
    隐藏推理。没有明确的上一轮发现任务时绝不猜测。
    """

    if not _DISCOVERY_CONTINUATION_RE.search(query):
        return None
    if _TASK_SWITCH_RE.search(query) and not _DISCOVERY_REQUEST_RE.search(query):
        return None
    entity_state = cached.get("entity_state")
    if not isinstance(entity_state, dict):
        entity_state = {}
    stored = entity_state.get("active_task")
    if isinstance(stored, dict) and stored.get("name") == "find_related_papers":
        task = dict(stored)
    elif previous and _DISCOVERY_REQUEST_RE.search(previous):
        previous_sources = academic_source_policy(previous)
        task = {
            "name": "find_related_papers",
            "web_required": True,
            "requested_count": requested_paper_count(previous, default=5),
            "exclude_library": bool(
                re.search(r"尚未.{0,8}文献库|未入库|不在.{0,8}文献库|排除.{0,8}已入库", previous)
            ),
            "source_policy": "academic_external",
        }
        if previous_sources.has_explicit_source:
            task["requested_sources"] = sorted(previous_sources.requested_tools)
            task["denied_sources"] = sorted(previous_sources.denied_tools)
    else:
        return None
    current_count = requested_paper_count(query)
    if current_count is not None:
        task["requested_count"] = current_count
    years = [int(value) for value in _YEAR_RE.findall(query)]
    if years:
        task["year_from"] = min(years)
        task["year_to"] = max(years)
    current_sources = academic_source_policy(query)
    if current_sources.has_explicit_source:
        task["requested_sources"] = sorted(current_sources.requested_tools)
        task["denied_sources"] = sorted(current_sources.denied_tools)
    task["inherited"] = True
    return task


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
    previous = _previous_user_text(messages, original)
    discovery_anchor = _previous_discovery_text(messages, original)
    active_task = _discovery_task_context(original, discovery_anchor, cached)
    has_cached_anchor = bool(cached.get("conversation_summary") or cached.get("entity_state"))
    if (
        not selected
        and not has_cached_anchor
        and not active_task
        and not followup_entity
        and not any(marker in original for marker in _REFERENCE_MARKERS)
    ):
        return ContextResolution(original, original, {}, 1.0, ("explicit_query",))

    paper_title = str(context.get("paper_title", "")).strip()
    paper_id = str(context.get("paper_id", "")).strip()
    collection_id = str(context.get("collection_id", "")).strip()
    collection_title = str(context.get("collection_title", "")).strip()
    page = context.get("physical_page")
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
    if active_task:
        references["active_task"] = active_task
        sources.append("active_task")

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
    if active_task:
        count = int(active_task.get("requested_count") or 5)
        constraints = [f"继续联网推荐 {count} 篇相关论文"]
        if active_task.get("exclude_library"):
            constraints.append("排除已在当前文献库中的论文")
        year_from = active_task.get("year_from")
        year_to = active_task.get("year_to")
        if year_from and year_to:
            constraints.append(
                f"目标发表年份：{year_from}"
                if year_from == year_to
                else f"目标发表年份：{year_from}–{year_to}"
            )
        source_names = {
            "mcp__academic__search_openalex": "OpenAlex",
            "mcp__academic__search_semantic_scholar": "Semantic Scholar",
            "search_arxiv": "arXiv",
        }
        requested_sources = [
            source_names[value]
            for value in active_task.get("requested_sources", [])
            if value in source_names
        ]
        denied_sources = [
            source_names[value]
            for value in active_task.get("denied_sources", [])
            if value in source_names
        ]
        if requested_sources:
            constraints.append("要求使用数据源：" + "、".join(requested_sources))
        if denied_sources:
            constraints.append("排除数据源：" + "、".join(denied_sources))
        qualifiers.append("延续上一轮任务：" + "；".join(constraints))
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
