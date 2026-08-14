"""持久化 Agent Run 执行与经核验段落发布。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable
from typing import Any

from .agent.context import (
    ContextResolution,
    fallback_task_frame_decision,
    resolve_context,
)
from .agent.context_budget import allocate_context_budget, compact_conversation
from .agent.discovery_policy import academic_source_policy, requested_paper_count
from .agent.function_tools import (
    FunctionToolHarness,
    ToolExecutionContext,
    ToolLoopResult,
)
from .agent.memory import (
    extract_memory_candidates,
    memory_hash,
    select_relevant_memories,
)
from .agent.provider_policy import build_provider_run_policy, provider_policy_snapshot
from .agent.recommendation_quality import entity_keys
from .agent.research_specialist_graph import (
    SPECIALIST_ORCHESTRATION_VERSION,
    ResearchGraphContext,
    research_checkpoint_thread_id,
    specialist_result_from_state,
)
from .agent.research_specialists import EvidenceSpecialist
from .agent.research_synthesis import (
    ORCHESTRATION_VERSION,
    ResearchLeaseLostError,
    ResearchSynthesisResult,
    ResearchTask,
    ScoutResult,
    build_deterministic_research_plan,
    execute_research_plan,
)
from .agent.skills import SkillRegistry, route_verified_selection
from .agent.tools import LibrarySearchInput
from .config import settings
from .discovery import embed_discovery_texts
from .embedding_contract import configured_embedding_contract, vector_matches_contract
from .model_runtime import ModelRuntimeError, collect_model_attempts
from .rag.answer_quality import AnswerQualityPolicy
from .rag.citations import CitationClaim, Evidence, validate_citations
from .rag_observability import (
    build_rag_trace,
    classify_intent,
    record_rag_run,
)
from .repository import MemoryItemRecord


async def _embed_memory_text(config: Any, text: str) -> list[float] | None:
    """复用 Embedding 路由；Ollama/向量服务故障时静默退回关键词选择。"""

    try:
        from .model_runtime import build_model_router

        values = await embed_discovery_texts(config, build_model_router(config), [text])
        return values[0] if values else None
    except Exception:
        return None


async def _save_run_memories(
    repository: Any,
    config: Any,
    run: Any,
    query: str,
) -> None:
    """Run 已落终态后执行；失败不会回滚用户已经收到的回答。"""

    for candidate in extract_memory_candidates("user", query):
        embedding = await _embed_memory_text(config, candidate.value)
        from .model_runtime import build_model_router

        contract = configured_embedding_contract(config, build_model_router(config))
        if contract is None or not embedding or not vector_matches_contract(embedding, contract):
            embedding = None
            embedding_fingerprint = None
        else:
            embedding_fingerprint = contract.fingerprint
        await repository.create_memory_item(
            MemoryItemRecord(
                id=str(uuid.uuid4()),
                user_id=run.user_id,
                type=candidate.type,
                value=candidate.value,
                normalized_hash=memory_hash(candidate.type, candidate.value),
                confidence=candidate.confidence,
                source_kind=candidate.source_kind,
                source_session_id=run.session_id,
                source_message_id=run.user_message_id,
                source_excerpt=candidate.source_excerpt,
                pinned=candidate.source_kind == "explicit",
                embedding=embedding,
                embedding_fingerprint=embedding_fingerprint,
            )
        )


_CITATION_RE = re.compile(r"\[chunk:([^\]]+)\]")
_CONTROLLED_NOTICE_RE = re.compile(r"^\s*>?\s*证据说明[：:]", re.IGNORECASE)
_STRUCTURAL_MARKDOWN_RE = re.compile(r"^\s*(?:#{1,6}\s+[^\n]+|[-*_]{3,})\s*$")


def _selection_scope_is_locked(query: str) -> bool:
    """选文默认只允许同页证据；只有明确要求结合全文时才放宽。"""

    normalized = " ".join(query.casefold().split())
    if re.search(r"(?:不要|不许|无需|别).{0,12}(?:全文|整篇|全篇)", normalized):
        return True
    if re.search(r"(?:只|仅).{0,12}(?:选中|所选|这段|原文)", normalized):
        return True
    expand_markers = (
        "结合全文",
        "基于全文",
        "检索全文",
        "扩展到全文",
        "整篇论文",
        "全篇论文",
        "whole paper",
        "entire paper",
    )
    return not any(marker in normalized for marker in expand_markers)


def _answer_paragraphs(answer: str) -> list[str]:
    """按空行切自然段，并保持 fenced code/list/table 等 Markdown 块完整。"""

    blocks: list[str] = []
    current: list[str] = []
    fence_marker: str | None = None
    for line in answer.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence_marker = None if fence_marker == marker else marker
            current.append(line)
            continue
        if not stripped and fence_marker is None:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _citation_dicts(
    citations: list[CitationClaim], evidence: list[Evidence]
) -> list[dict[str, Any]]:
    by_chunk = {item.chunk_id: item for item in evidence}
    result: list[dict[str, Any]] = []
    for citation in citations:
        source = by_chunk.get(citation.chunk_id)
        if not source:
            continue
        result.append(
            {
                "chunk_id": source.chunk_id,
                "paper_id": source.paper_id,
                "paper_title": source.paper_title,
                "physical_page": source.physical_page,
                "excerpt": citation.excerpt or source.text[:320],
                "viewer_url": (
                    f"/api/v1/papers/{source.paper_id}/file#page={source.physical_page}"
                ),
            }
        )
    return result


def _next_entity_state(
    existing: dict[str, Any],
    resolution: ContextResolution,
    original_query: str,
    *,
    selected_skill: str,
    web_enabled: bool,
    exposed_recommendation_entities: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """保存可审计的讨论实体，不保存选文正文或隐藏推理。"""

    state = dict(existing)
    references = resolution.references
    paper_id = str(references.get("paper_id", "")).strip()
    if paper_id and state.get("paper_id") not in {None, "", paper_id}:
        state = {}
    if paper_id:
        state["paper_id"] = paper_id
    collection_id = str(references.get("collection_id", "")).strip()
    if collection_id and state.get("collection_id") not in {None, "", collection_id}:
        state = {}
    if collection_id:
        state["collection_id"] = collection_id
    if references.get("paper_title"):
        state["paper_title"] = str(references["paper_title"])
    if references.get("physical_page") is not None:
        state["physical_page"] = int(references["physical_page"])
    if references.get("discussion_entity"):
        state["discussion_entity"] = str(references["discussion_entity"])
    elif references.get("summary_entity"):
        state["discussion_entity"] = str(references["summary_entity"])
    if references.get("selected_text"):
        state["selection_consumed"] = True
    if selected_skill == "find_related_papers" and web_enabled:
        inherited = references.get("active_task")
        task = dict(inherited) if isinstance(inherited, dict) else {}
        requested_count = requested_paper_count(original_query)
        years = [
            int(value) for value in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", original_query)
        ]
        task.update(
            {
                "name": "find_related_papers",
                "web_required": True,
                "requested_count": requested_count
                if requested_count is not None
                else int(task.get("requested_count") or 5),
                "exclude_library": bool(task.get("exclude_library"))
                or bool(re.search(r"尚未.{0,8}文献库|未入库|不在.{0,8}文献库", original_query)),
                "source_policy": "academic_external",
            }
        )
        if years:
            task["year_from"] = min(years)
            task["year_to"] = max(years)
        current_sources = academic_source_policy(original_query)
        if current_sources.has_explicit_source:
            task["requested_sources"] = sorted(current_sources.requested_tools)
            task["denied_sources"] = sorted(current_sources.denied_tools)
        prior_entities = task.get("shown_entities")
        shown_entities = [
            str(value)
            for value in (prior_entities if isinstance(prior_entities, list) else [])
            if str(value).strip()
        ]
        for value in exposed_recommendation_entities:
            normalized = str(value).strip()
            if normalized and normalized not in shown_entities:
                shown_entities.append(normalized)
        task["shown_entities"] = shown_entities[-400:]
        task.pop("inherited", None)
        state["active_task"] = task
    else:
        state.pop("active_task", None)
    state["last_user_query"] = original_query[:500]
    return state


def _validate_publishable_paragraph(
    paragraph: str,
    citations: list[CitationClaim],
    evidence: list[Evidence],
    evidence_quality: dict[str, Any],
    _policy: AnswerQualityPolicy,
) -> tuple[bool, str, list[CitationClaim]]:
    cited_ids = set(_CITATION_RE.findall(paragraph))
    paragraph_citations = [item for item in citations if item.chunk_id in cited_ids]
    if not cited_ids:
        controlled_notice = (
            _CONTROLLED_NOTICE_RE.match(paragraph) is not None
            or _STRUCTURAL_MARKDOWN_RE.match(paragraph) is not None
            or (not citations and str(evidence_quality.get("grade", "")) == "insufficient")
        )
        return controlled_notice, "controlled_notice", []
    valid, _errors = validate_citations(paragraph_citations, evidence)
    if not valid or cited_ids != {item.chunk_id for item in paragraph_citations}:
        return False, "invalid_citation", []
    # 这里验证的是用户可回读的来源契约，而不是再次让另一个 LLM 覆盖回答。
    # 事实段落至少有一个本轮真实召回的引用；段末引用可支持该段的多句综合表达。
    return True, "cited_answer", paragraph_citations


async def _invoke_with_cancel(
    repository: Any,
    graph: Any,
    run: Any,
    initial: dict[str, Any],
    *,
    checkpoint_namespace: str | None = None,
) -> dict[str, Any]:
    orchestration_version = str(
        getattr(run, "orchestration_version", "")
        or (run.scope_snapshot or {}).get("orchestration_version")
        or "single_agent_v1"
    )
    graph_config = {
        "recursion_limit": 8,
        # 同一父 Run 的 v1 回退与 v2 编排不能共享 Checkpoint 命名空间，
        # 否则恢复时可能把不同状态形状的节点结果混在一起。
        "configurable": {
            "thread_id": run.thread_id,
            "checkpoint_ns": checkpoint_namespace or f"{orchestration_version}/final",
        },
    }
    resume_decision = (run.scope_snapshot or {}).get("resume_decision")
    if resume_decision:
        try:
            from langgraph.types import Command

            invocation: Any = Command(resume=resume_decision)
        except ImportError:
            invocation = initial
    else:
        invocation = initial
    task = asyncio.create_task(graph.ainvoke(invocation, graph_config))
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=0.2)
            if done:
                return await task
            current = await repository.get_agent_run(run.id)
            if not current or current.cancel_requested or current.status == "cancelled":
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise asyncio.CancelledError
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        raise


async def _invoke_tools_with_cancel(
    repository: Any,
    run: Any,
    harness: FunctionToolHarness,
    query: str,
    context: ToolExecutionContext,
) -> ToolLoopResult:
    """工具循环与 Graph 使用相同的持久取消语义。"""

    task = asyncio.create_task(harness.run(query, context))
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=0.2)
            if done:
                return await task
            current = await repository.get_agent_run(run.id)
            if not current or current.cancel_requested or current.status == "cancelled":
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise asyncio.CancelledError
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        raise


async def _invoke_parallel_compare_with_cancel(
    repository: Any,
    run: Any,
    claim_token: str,
    operation: Awaitable[ResearchSynthesisResult],
) -> ResearchSynthesisResult:
    """在并行分支运行期间轮询用户取消和 Worker 租约，避免后台空转。"""

    task = asyncio.create_task(operation)
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=0.2)
            if done:
                return await task
            current = await repository.get_agent_run(run.id)
            claim_current = await repository.is_agent_claim_current(run.id, claim_token)
            if (
                not current
                or current.cancel_requested
                or current.status == "cancelled"
                or not claim_current
            ):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise asyncio.CancelledError
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        raise


async def _invoke_specialist_graph_with_cancel(
    repository: Any,
    run: Any,
    claim_token: str,
    graph: Any,
    initial: dict[str, Any],
    context: ResearchGraphContext,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """恢复或启动同一父 Run 的 Specialist Research Graph。"""

    graph_config = {
        "recursion_limit": 8,
        "configurable": {
            "thread_id": research_checkpoint_thread_id(run.thread_id),
            "checkpoint_ns": "",
        },
    }
    invocation: Any = initial
    try:
        snapshot = await graph.aget_state(graph_config)
        if getattr(snapshot, "values", None):
            if getattr(snapshot, "next", ()):
                invocation = None
            elif str(snapshot.values.get("status", "")) in {
                "succeeded",
                "partial",
                "failed",
            }:
                return dict(snapshot.values)
    except ValueError:
        # 无 Checkpointer 的测试/演示图从初始输入启动；生产图始终使用 PostgreSQL。
        invocation = initial

    task = asyncio.create_task(graph.ainvoke(invocation, graph_config, context=context))
    started_at = time.monotonic()
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=0.2)
            if done:
                return await task
            if time.monotonic() - started_at >= timeout_seconds:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise TimeoutError("SPECIALIST_GRAPH_TIMEOUT")
            current = await repository.get_agent_run(run.id)
            claim_current = await repository.is_agent_claim_current(run.id, claim_token)
            if (
                not current
                or current.cancel_requested
                or current.status == "cancelled"
                or not claim_current
            ):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise asyncio.CancelledError
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        raise


def _parallel_compare_trace(
    result: ResearchSynthesisResult | None,
    *,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    """生成不包含用户、论文或证据标识符的低基数编排轨迹。"""

    if result is None:
        return {
            "orchestration_version": ORCHESTRATION_VERSION,
            "compare_mode": "parallel_map_reduce",
            "planned_subtasks": 0,
            "succeeded_subtasks": 0,
            "failed_subtasks": 0,
            "timeout_subtasks": 0,
            "partial_failure": False,
            "fallback_to_v1": True,
            "fallback_reason": fallback_reason or "internal_error",
            "subtask_durations_ms": [],
            "merge_duration_ms": 0,
            "merge_dedup_count": 0,
            "merge_conflict_count": 0,
            "finding_count": 0,
            "paper_coverage_count": 0,
            "branch_metrics": [],
        }
    findings = list(result.report.findings)
    branch_metrics = list(result.branch_metrics)
    succeeded = sum(item.status == "succeeded" for item in findings)
    timed_out = sum(item.status == "timeout" for item in findings)
    failed = len(findings) - succeeded
    fallback = result.fallback_required
    return {
        "orchestration_version": ORCHESTRATION_VERSION,
        "compare_mode": "parallel_map_reduce",
        "planned_subtasks": len(result.plan.tasks),
        "succeeded_subtasks": succeeded,
        "failed_subtasks": failed,
        "timeout_subtasks": timed_out,
        "partial_failure": result.report.status == "partial",
        "fallback_to_v1": fallback,
        "fallback_reason": (fallback_reason if fallback else None),
        "subtask_durations_ms": [item.duration_ms for item in findings],
        "merge_duration_ms": result.report.merge_duration_ms,
        "merge_dedup_count": result.report.dedup_count,
        "merge_conflict_count": result.report.conflict_count,
        "finding_count": len(result.evidence),
        "paper_coverage_count": len(result.report.evidence_paper_ids),
        "branch_metrics": [
            {
                "subtask_id": str(item.get("subtask_id", ""))[:8],
                "status": str(item.get("status", "failed"))[:24],
                "duration_ms": max(0, int(item.get("duration_ms", 0) or 0)),
                "evidence_count": max(0, int(item.get("evidence_count", 0) or 0)),
                "claim_count": max(0, int(item.get("claim_count", 0) or 0)),
                "input_tokens": max(0, int(item.get("input_tokens", 0) or 0)),
                "output_tokens": max(0, int(item.get("output_tokens", 0) or 0)),
                "provider_input_tokens": item.get("provider_input_tokens"),
                "provider_output_tokens": item.get("provider_output_tokens"),
                "schema_repair_count": max(
                    0, int(item.get("schema_repair_count", 0) or 0)
                ),
                "schema_fallback_used": bool(item.get("schema_fallback_used", False)),
                "timeout_fallback_used": bool(item.get("timeout_fallback_used", False)),
                "error_category": (
                    "timeout"
                    if str(item.get("status")) == "timeout"
                    else "schema"
                    if "OUTPUT" in str(item.get("error_code") or "")
                    or "ALIAS" in str(item.get("error_code") or "")
                    or "DIMENSION" in str(item.get("error_code") or "")
                    else "budget"
                    if "BUDGET" in str(item.get("error_code") or "")
                    else "provider"
                    if str(item.get("status")) == "failed"
                    else None
                ),
            }
            for item in branch_metrics[:3]
        ],
    }


async def _execute_parallel_compare(
    repository: Any,
    run: Any,
    claim_token: str,
    *,
    query: str,
    paper_ids: list[str],
    retriever: Any,
    harness_flags: dict[str, Any],
    retrieval_config: dict[str, Any],
    event_epoch: str,
) -> ResearchSynthesisResult:
    """在父 Run 内执行有界只读 Map-Reduce，并发活动通过安全事件公开。"""

    await repository.append_agent_run_event(
        run.id,
        "node_started",
        {
            "node": "plan_comparison",
            "public_stage": "comparison_planning",
            "orchestration_version": ORCHESTRATION_VERSION,
        },
        event_key=f"stage:compare:{event_epoch}:plan:start",
        claim_token=claim_token,
    )
    plan = build_deterministic_research_plan(
        query,
        paper_ids,
        ("研究问题", "核心方法", "实验设置", "主要结果", "局限"),
        max_branches=int(harness_flags.get("multi_agent_max_branches", 3)),
        total_token_budget=int(harness_flags.get("multi_agent_token_budget", 12000)),
    )
    await repository.append_agent_run_event(
        run.id,
        "node_finished",
        {
            "node": "plan_comparison",
            "public_stage": "comparison_planning",
            "subtask_total": len(plan.tasks),
            "orchestration_version": ORCHESTRATION_VERSION,
        },
        event_key=f"stage:compare:{event_epoch}:plan:finish",
        claim_token=claim_token,
    )
    ordinal_by_id = {task.subtask_id: index + 1 for index, task in enumerate(plan.tasks)}

    async def lease_guard() -> bool:
        return await repository.is_agent_claim_current(run.id, claim_token)

    async def scout(task: ResearchTask) -> ScoutResult:
        # Scout 只能访问 Coordinator 已冻结的论文子集；不提供联网、写入、Memory
        # 或审批工具。最终事实仍由可信 Evidence 与现有回答 Graph 组织。
        evidence = await retriever(
            LibrarySearchInput(
                user_id=run.user_id,
                query=(f"{query}\n比较维度：{'、'.join(task.dimensions)}"),
                paper_ids=list(task.paper_ids),
                limit=min(18, max(8, len(task.paper_ids) * 5)),
                ensure_paper_coverage=True,
                per_paper_query_mode="paper_specific",
                retrieval_config=retrieval_config,
            )
        )
        return ScoutResult(evidence=tuple(evidence))

    async def event_sink(event: str, data: dict[str, Any]) -> None:
        subtask_id = str(data.get("subtask_id", ""))
        ordinal = ordinal_by_id.get(subtask_id)
        total = len(plan.tasks)
        if event == "subtask_started" and ordinal is not None:
            await repository.append_agent_run_event(
                run.id,
                "node_started",
                {
                    "node": "compare_subtask",
                    "public_stage": "comparison_subtask",
                    "subtask_id": f"s{ordinal}",
                    "ordinal": ordinal,
                    "total": total,
                    "paper_count": int(data.get("paper_count", 0)),
                    "dimensions": ["研究问题", "核心方法", "实验设置", "主要结果", "局限"],
                    "orchestration_version": ORCHESTRATION_VERSION,
                },
                event_key=f"stage:compare:{event_epoch}:subtask:s{ordinal}:start",
                claim_token=claim_token,
            )
        elif event == "subtask_finished" and ordinal is not None:
            raw_status = str(data.get("status", "failed"))
            error_category = (
                "timeout"
                if raw_status == "timeout"
                else "retrieval"
                if raw_status == "failed"
                else None
            )
            await repository.append_agent_run_event(
                run.id,
                "node_finished",
                {
                    "node": "compare_subtask",
                    "public_stage": "comparison_subtask",
                    "subtask_id": f"s{ordinal}",
                    "ordinal": ordinal,
                    "total": total,
                    "status": "completed" if raw_status == "succeeded" else raw_status,
                    "finding_count": int(data.get("evidence_count", 0)),
                    "duration_ms": int(data.get("duration_ms", 0)),
                    **({"error_category": error_category} if error_category else {}),
                    "orchestration_version": ORCHESTRATION_VERSION,
                },
                event_key=f"stage:compare:{event_epoch}:subtask:s{ordinal}:finish",
                claim_token=claim_token,
            )
        elif event == "merge_started":
            await repository.append_agent_run_event(
                run.id,
                "node_started",
                {
                    "node": "merge_comparison",
                    "public_stage": "comparison_merge",
                    "orchestration_version": ORCHESTRATION_VERSION,
                },
                event_key=f"stage:compare:{event_epoch}:merge:start",
                claim_token=claim_token,
            )
        elif event == "merge_finished":
            raw_status = str(data.get("status", "failed"))
            status = "completed" if raw_status == "succeeded" else raw_status
            await repository.append_agent_run_event(
                run.id,
                "node_finished",
                {
                    "node": "merge_comparison",
                    "public_stage": "comparison_merge",
                    "status": status,
                    "succeeded_subtasks": int(data.get("succeeded_subtask_count", 0)),
                    "failed_subtasks": int(data.get("failed_subtask_count", 0)),
                    "timeout_subtasks": int(data.get("timeout_subtask_count", 0)),
                    "finding_count": int(data.get("evidence_count", 0)),
                    "dedup_count": int(data.get("dedup_count", 0)),
                    "conflict_count": int(data.get("conflict_count", 0)),
                    "duration_ms": int(data.get("duration_ms", 0)),
                    "partial_failure": raw_status == "partial",
                    "fallback_to_v1": raw_status == "failed",
                    **(
                        {"fallback_reason": "all_subtasks_failed"} if raw_status == "failed" else {}
                    ),
                    "orchestration_version": ORCHESTRATION_VERSION,
                },
                event_key=f"stage:compare:{event_epoch}:merge:finish",
                claim_token=claim_token,
            )

    return await asyncio.wait_for(
        execute_research_plan(
            plan,
            scout,
            allowed_paper_ids=paper_ids,
            branch_timeout_seconds=float(
                harness_flags.get("multi_agent_branch_timeout_seconds", 20)
            ),
            lease_guard=lease_guard,
            event_sink=event_sink,
            max_concurrency=int(harness_flags.get("multi_agent_max_branches", 3)),
        ),
        timeout=float(harness_flags.get("multi_agent_total_timeout_seconds", 45)),
    )


async def _finish_observed_run(
    repository: Any,
    run_id: str,
    claim_token: str,
    *,
    started_at: float,
    status: str,
    intent: str,
    scope: str,
    outcome: str,
    result: dict[str, Any] | None,
    result_summary: dict[str, Any],
    error_code: str | None = None,
    stage_timings_ms: dict[str, int] | None = None,
    **finish_values: Any,
) -> Any:
    """原子落终态后记录低基数指标；持久轨迹仍是管理员统计的事实源。"""

    duration_ms = round((time.perf_counter() - started_at) * 1000)
    trace = build_rag_trace(
        intent=intent,
        scope=scope,
        result=result,
        stage_timings_ms=stage_timings_ms,
        outcome=outcome,
        error_code=error_code,
    )
    summary = dict(result_summary)
    summary["rag_trace"] = trace
    finished = await repository.finish_agent_run(
        run_id,
        status=status,
        error_code=error_code,
        duration_ms=duration_ms,
        result_summary=summary,
        claim_token=claim_token,
        **finish_values,
    )
    if finished:
        record_rag_run(
            trace,
            status=str(getattr(finished, "status", status)),
            duration_ms=getattr(finished, "duration_ms", duration_ms),
        )
    return finished


async def execute_agent_run(
    repository: Any,
    graph: Any,
    run_id: str,
    claim_token: str,
    *,
    answer_quality_policy: AnswerQualityPolicy,
    harness_config: Any = settings,
    skill_registry: SkillRegistry | None = None,
    function_tool_harness: FunctionToolHarness | None = None,
    research_graph: Any | None = None,
    evidence_specialist: EvidenceSpecialist | None = None,
) -> None:
    """执行 Graph；只把通过 citation + support 的完整段落写入持久层。"""

    started_at = time.perf_counter()
    run_input = await repository.get_agent_run_input(run_id)
    if not run_input:
        raise RuntimeError("AGENT_RUN_INPUT_MISSING")
    run, query = run_input
    started = await repository.start_agent_run(run_id, claim_token)
    if not started:
        return
    run = started
    snapshot = dict(run.scope_snapshot or {})
    retrieval_config = dict(snapshot.get("retrieval_config", {}) or {})
    scope = str(snapshot.get("type", "library"))
    resumed_action = snapshot.get("resumed_action")
    resume_decision = str(snapshot.get("resume_decision", ""))
    if (
        isinstance(resumed_action, dict)
        and resumed_action.get("type") == "confirm_arxiv_import"
        and resume_decision in {"approve", "reject"}
        and function_tool_harness is not None
    ):
        message, action_error = await function_tool_harness.resume_confirmed_action(
            run.user_id,
            resumed_action,
            resume_decision,
        )
        await repository.publish_agent_paragraph(
            run.id,
            0,
            message,
            [],
            "controlled_notice",
            claim_token,
        )
        tool_call_record_id = str(resumed_action.get("tool_call_record_id", ""))
        if tool_call_record_id:
            await repository.finish_agent_tool_call(
                tool_call_record_id,
                run.id,
                claim_token,
                status=(
                    "cancelled"
                    if resume_decision == "reject"
                    else "failed"
                    if action_error
                    else "succeeded"
                ),
                attempt=1,
                duration_ms=0,
                result_preview={
                    "tool": "request_import",
                    "status": "failed" if action_error else "finished",
                },
                error_code=action_error,
            )
        intent = classify_intent(
            query,
            scope=scope,
            selected_paper_count=len(snapshot.get("paper_ids", [])),
            web_enabled=bool(snapshot.get("web_enabled", False)),
        )
        await _finish_observed_run(
            repository,
            run.id,
            claim_token,
            started_at=started_at,
            status="completed",
            intent=intent,
            scope=scope,
            outcome="tool_action",
            error_code=None,
            result={"status": "completed", "answer": message, "citations": []},
            tool_steps=max(1, int(getattr(run, "tool_steps", 0) or 0)),
            result_summary={
                "answer": message,
                "citations": [],
                "tool_action_error": action_error,
            },
        )
        return
    visible_history = await repository.list_chat_messages(run.session_id, run.user_id)
    history_records = [
        item
        for item in (visible_history or [])
        if item.content.strip() and item.id != run.assistant_message_id
    ]
    context_started_at = time.perf_counter()
    harness_flags = dict(snapshot.get("harness", {}))
    budget = allocate_context_budget(
        harness_config.model_context_tokens,
        safety_ratio=harness_config.context_safety_ratio,
        compact_ratio=harness_config.context_compact_ratio,
        hard_limit_ratio=harness_config.context_hard_limit_ratio,
    )
    chat_session = await repository.get_owned_chat_session(run.session_id, run.user_id)
    compaction = compact_conversation(
        history_records,
        existing_summary=dict(getattr(chat_session, "compact_summary", {}) or {}),
        keep_recent_turns=harness_config.context_keep_recent_turns,
        compact_at_tokens=budget.compact_at,
    )
    if compaction.compacted:
        updated_session = await repository.update_session_compaction(
            run.session_id,
            run.user_id,
            compact_summary=compaction.summary,
            compacted_through_message_id=compaction.compacted_through_message_id,
            entity_state=dict(getattr(chat_session, "entity_state", {}) or {}),
        )
        if updated_session is not None:
            chat_session = updated_session
    user = await repository.get_user(run.user_id)
    user_preferences = dict(getattr(user, "preferences", {}) or {})
    memory_allowed = bool(
        harness_flags.get("memory_enabled") and user_preferences.get("memory_enabled", True)
    )
    selected_memories: list[Any] = []
    if memory_allowed:
        memories = await repository.list_memories(run.user_id, enabled_only=True)
        query_embedding = None
        from .model_runtime import build_model_router

        memory_contract = configured_embedding_contract(
            harness_config, build_model_router(harness_config)
        )
        if any(getattr(item, "embedding", None) for item in memories):
            query_embedding = await _embed_memory_text(harness_config, query)
        selected_memories = select_relevant_memories(
            query,
            memories,
            query_embedding=query_embedding,
            embedding_fingerprint=(
                memory_contract.fingerprint if memory_contract is not None else None
            ),
            limit=harness_config.context_max_memories,
        )
    history = list(compaction.recent_messages)
    cached_context: dict[str, Any] = {}
    if compaction.summary:
        cached_context["conversation_summary"] = compaction.summary
    entity_state = dict(getattr(chat_session, "entity_state", {}) or {})
    if entity_state:
        cached_context["entity_state"] = entity_state
    if selected_memories:
        cached_context["user_memories"] = [
            {"type": item.type, "value": item.value} for item in selected_memories
        ]
    if cached_context:
        history.insert(
            0,
            {
                "role": "context",
                "content": json.dumps(cached_context, ensure_ascii=False, separators=(",", ":")),
            },
        )
    verified_client_context = dict(snapshot.get("client_context", {}))
    if scope == "paper" and snapshot.get("paper_id"):
        verified_client_context.setdefault("paper_id", snapshot["paper_id"])
    if scope == "collection" and snapshot.get("collection_id"):
        verified_client_context.setdefault("collection_id", snapshot["collection_id"])
    task_frame_decision = None
    existing_active_task = entity_state.get("active_task")
    if (
        harness_flags.get("context_engine_enabled")
        and isinstance(existing_active_task, dict)
        and existing_active_task.get("name") == "find_related_papers"
    ):
        if function_tool_harness is not None:
            try:
                task_frame_decision = await function_tool_harness.resolve_task_frame(
                    query=query,
                    existing_task=existing_active_task,
                    recent_user_messages=[
                        str(item.get("content", ""))[:1000]
                        for item in history
                        if str(item.get("role", "")) == "user"
                    ],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                task_frame_decision = None
        if task_frame_decision is None or task_frame_decision.confidence < 0.55:
            task_frame_decision = fallback_task_frame_decision(query, existing_active_task)
    if harness_flags.get("context_engine_enabled"):
        resolution = resolve_context(
            query,
            verified_client_context,
            history,
            session_type=scope,
            task_frame_decision=task_frame_decision,
        )
    else:
        resolution = ContextResolution(query, query, {}, 1.0, ("legacy_agent",))
    context_snapshot = resolution.snapshot(verified_client_context)
    context_snapshot["budget"] = budget.as_dict()
    context_snapshot["usage"] = {
        "conversation_before_tokens": compaction.before_tokens,
        "conversation_after_tokens": compaction.after_tokens,
        "compacted": compaction.compacted,
    }
    context_snapshot["summary_version"] = int(getattr(chat_session, "summary_version", 1) or 1)
    context_snapshot["memory_ids"] = [item.id for item in selected_memories]
    context_ms = round((time.perf_counter() - context_started_at) * 1000)
    intent_started_at = time.perf_counter()
    intent = classify_intent(
        resolution.resolved_query,
        scope=scope,
        selected_paper_count=len(snapshot.get("paper_ids", [])),
        web_enabled=bool(snapshot.get("web_enabled", False)),
    )
    intent_ms = round((time.perf_counter() - intent_started_at) * 1000)
    selected_skill = "legacy_agent"
    skill_version = 0
    skill_instructions = ""
    route_source = "feature_flag_disabled"
    route_confidence = 1.0
    definition = None
    if harness_flags.get("skills_enabled"):
        registry = skill_registry or SkillRegistry.default()
        selected_text = str(resolution.references.get("selected_text", "")).strip()
        active_task = resolution.references.get("active_task")
        if selected_text:
            definition = route_verified_selection(registry, resolution.original_query)
            route_source = "verified_selection_override"
            route_confidence = 1.0
        elif (
            isinstance(active_task, dict)
            and active_task.get("name") == "find_related_papers"
            and bool(snapshot.get("web_enabled", False))
        ):
            definition = registry.get("find_related_papers")
            route_source = "context_task_inheritance"
            route_confidence = 0.98
        elif harness_flags.get("function_tools_enabled") and function_tool_harness is not None:
            try:
                (
                    definition,
                    route_source,
                    route_confidence,
                ) = await function_tool_harness.select_skill(
                    registry,
                    resolution.original_query,
                    intent=intent,
                    scope=scope,
                    web_enabled=bool(snapshot.get("web_enabled", False)),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                definition = registry.route(
                    resolution.original_query,
                    intent=intent,
                    scope=scope,
                    web_enabled=bool(snapshot.get("web_enabled", False)),
                )
        else:
            definition = registry.route(
                resolution.original_query,
                intent=intent,
                scope=scope,
                web_enabled=bool(snapshot.get("web_enabled", False)),
            )
        selected_skill = definition.manifest.name
        skill_version = definition.manifest.version
        skill_instructions = definition.instructions
        if route_source == "feature_flag_disabled":
            route_source = "deterministic_fallback"
            route_confidence = 0.85
        history.insert(0, {"role": "skill", "content": skill_instructions})
    context_snapshot["skill"] = {
        "name": selected_skill,
        "version": skill_version,
        "route_source": route_source,
        "route_confidence": route_confidence,
    }
    updated_run = await repository.update_agent_context(
        run.id,
        claim_token,
        context_snapshot=context_snapshot,
        resolved_query=resolution.resolved_query,
        reference_confidence=resolution.confidence,
    )
    if not updated_run:
        return
    run = updated_run
    harness_trace = {
        "context_version": int(context_snapshot.get("version", 1)),
        "selected_skill": selected_skill,
        "skill_version": skill_version,
        "skill_route_source": route_source,
        "skill_route_confidence": route_confidence,
        "tool_calls": [],
        "tool_context_entries": [],
        "tool_mode_active": False,
        "pre_retrieved_evidence": [],
        "pre_arxiv_candidates": [],
        "native_function_calling_attempted": False,
        "tool_output_used": False,
        "automatic_source_fallback_used": False,
        "task_frame_source": resolution.task_frame_source,
        "task_frame_confidence": resolution.task_frame_confidence,
        "provider_policy": {},
        "retrieval_config": retrieval_config,
    }
    updated_run = await repository.update_agent_skill(
        run.id,
        claim_token,
        selected_skill=selected_skill,
        skill_version=skill_version,
        harness_trace=harness_trace,
    )
    if not updated_run:
        return
    run = updated_run
    initial = {
        "run_id": run.id,
        "session_id": run.session_id,
        "user_id": run.user_id,
        "query": resolution.resolved_query,
        "original_query": query,
        "messages": history,
        "intent": intent,
        "scope": scope,
        "selected_paper_ids": list(snapshot.get("paper_ids", [])),
        "scope_paper_titles": [],
        "scope_paper_texts": [],
        "excluded_recommendation_entities": [],
        "provider_policy": {},
        "web_enabled": bool(snapshot.get("web_enabled", False)),
        "client_context": dict(snapshot.get("client_context", {})),
        "resolved_query": resolution.resolved_query,
        "resolved_references": resolution.references,
        "reference_confidence": resolution.confidence,
        "context_snapshot": context_snapshot,
        "context_budget": budget.as_dict(),
        "memory_ids": [item.id for item in selected_memories],
        "selected_skill": selected_skill,
        "skill_version": skill_version,
        "skill_instructions": skill_instructions,
        "skill_route_source": route_source,
        "skill_route_confidence": route_confidence,
        "tool_calls": [],
        "selection_evidence": [],
        "selection_scope_locked": False,
        "selection_physical_page": None,
        "selection_paper_id": None,
        "clarification_question": resolution.clarification_question,
        "tool_steps": 0,
        "stage_timings_ms": {"context": context_ms, "intent": intent_ms},
        "status": "pending",
    }
    if selected_skill == "find_related_papers":
        allowed_scope_ids = {str(paper_id) for paper_id in snapshot.get("paper_ids", [])}
        try:
            scoped_papers = await repository.list_papers(run.user_id)
        except Exception:
            scoped_papers = []
        initial["scope_paper_titles"] = sorted(
            dict.fromkeys(
                str(getattr(paper, "title", "") or "").strip()
                for paper in scoped_papers
                if str(getattr(paper, "id", "")) in allowed_scope_ids
                and str(getattr(paper, "title", "") or "").strip()
            ),
            key=str.casefold,
        )
        initial["scope_paper_texts"] = [
            "\n".join(
                value
                for value in (
                    str(getattr(paper, "title", "") or "").strip(),
                    str(getattr(paper, "abstract", "") or "").strip()[:3000],
                )
                if value
            )
            for paper in scoped_papers
            if str(getattr(paper, "id", "")) in allowed_scope_ids
            and str(getattr(paper, "title", "") or "").strip()
        ]
        initial["excluded_recommendation_entities"] = sorted(
            {key for paper in scoped_papers for key in entity_keys(paper)}
        )
    discovery_task = dict(
        resolution.references.get("active_task")
        if isinstance(resolution.references.get("active_task"), dict)
        else {}
    )
    if selected_skill == "find_related_papers":
        discovery_task.setdefault("requested_count", requested_paper_count(query, default=5) or 5)
        years = [int(value) for value in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", query)]
        if years:
            discovery_task["year_from"] = min(years)
            discovery_task["year_to"] = max(years)
        source_policy = academic_source_policy(query)
        if source_policy.has_explicit_source:
            discovery_task["requested_sources"] = sorted(source_policy.requested_tools)
            discovery_task["denied_sources"] = sorted(source_policy.denied_tools)
    provider_policy = build_provider_run_policy(discovery_task)
    initial["provider_policy"] = provider_policy
    harness_trace["provider_policy"] = provider_policy_snapshot(provider_policy)
    if harness_flags.get("skills_enabled"):
        await repository.append_agent_run_event(
            run_id,
            "node_started",
            {"node": "select_skill", "stage": "选择科研任务策略"},
            event_key="stage:skill:start",
            claim_token=claim_token,
        )
        await repository.append_agent_run_event(
            run_id,
            "node_finished",
            {
                "node": "select_skill",
                "stage": "选择科研任务策略",
                "skill": selected_skill,
                "skill_version": skill_version,
                "route_source": route_source,
            },
            event_key="stage:skill:finish",
            claim_token=claim_token,
        )
    with collect_model_attempts() as attempts:
        tool_mode_active = False
        tool_loop_result = None
        selected_text = str(resolution.references.get("selected_text", "")).strip()
        selection_page = resolution.references.get("physical_page")
        selection_paper_id = str(resolution.references.get("paper_id", "")).strip()
        selection_scope_locked = bool(selected_text) and _selection_scope_is_locked(
            resolution.original_query
        )
        initial["selection_scope_locked"] = selection_scope_locked
        initial["selection_physical_page"] = (
            int(selection_page) if selection_page is not None else None
        )
        initial["selection_paper_id"] = selection_paper_id or None
        harness_trace["selection_scope_locked"] = selection_scope_locked
        if selected_text and selection_paper_id and function_tool_harness is not None:
            try:
                selected_evidence = await function_tool_harness.retriever(
                    LibrarySearchInput(
                        user_id=run.user_id,
                        query=selected_text,
                        paper_ids=[selection_paper_id],
                        limit=6,
                        retrieval_config=retrieval_config,
                    )
                )
                initial["selection_evidence"] = [
                    item
                    for item in selected_evidence
                    if selection_page is None or item.physical_page == int(selection_page)
                ][:3]
                if not initial["selection_evidence"] and selection_page is not None:
                    page_loader = getattr(
                        function_tool_harness.retriever,
                        "page_selection_evidence",
                        None,
                    )
                    if page_loader is not None:
                        initial["selection_evidence"] = await page_loader(
                            user_id=run.user_id,
                            paper_id=selection_paper_id,
                            physical_page=int(selection_page),
                            selected_text=selected_text,
                            limit=3,
                        )
                harness_trace["selection_evidence_count"] = len(initial["selection_evidence"])
            except Exception:
                harness_trace["selection_evidence_count"] = 0
                harness_trace["selection_evidence_fallback_reason"] = "selection_retrieval_failed"

        compare_result: ResearchSynthesisResult | None = None
        compare_v2_used = False
        specialist_v3_used = False
        research_orchestration_attempted = False
        research_fallback_to_v1 = False
        compare_fallback_reason: str | None = None
        compare_event_epoch = hashlib.sha256(claim_token.encode("utf-8")).hexdigest()[:10]
        requested_orchestration = str(snapshot.get("orchestration_version", "single_agent_v1"))
        specialist_requested = (
            requested_orchestration == SPECIALIST_ORCHESTRATION_VERSION
            and bool(harness_flags.get("specialist_agents_enabled", False))
            and scope in {"collection", "library"}
            and 3 <= len(snapshot.get("paper_ids", [])) <= 10
        )
        compare_requested = (
            requested_orchestration == ORCHESTRATION_VERSION
            and bool(harness_flags.get("multi_agent_enabled", False))
            and scope in {"collection", "library"}
            and 3 <= len(snapshot.get("paper_ids", [])) <= 10
        )
        if specialist_requested and not resolution.needs_clarification:
            research_orchestration_attempted = True
            specialist_started_at = time.perf_counter()
            if (
                function_tool_harness is None
                or research_graph is None
                or evidence_specialist is None
            ):
                compare_fallback_reason = "internal_error"
            else:
                paper_ids = [str(value) for value in snapshot.get("paper_ids", [])]

                async def specialist_retriever(task: ResearchTask) -> list[Evidence]:
                    return await function_tool_harness.retriever(
                        LibrarySearchInput(
                            user_id=run.user_id,
                            query=(
                                f"{resolution.resolved_query}\n"
                                f"比较维度：{'、'.join(task.dimensions)}"
                            ),
                            paper_ids=list(task.paper_ids),
                            limit=min(18, max(8, len(task.paper_ids) * 5)),
                            ensure_paper_coverage=True,
                            per_paper_query_mode="paper_specific",
                            retrieval_config=retrieval_config,
                        )
                    )

                async def specialist_lease_guard() -> bool:
                    return await repository.is_agent_claim_current(run.id, claim_token)

                async def specialist_event_sink(event: str, data: dict[str, Any]) -> None:
                    event_map = {
                        "plan_started": ("node_started", "plan_comparison", "plan:start"),
                        "plan_finished": ("node_finished", "plan_comparison", "plan:finish"),
                        "subtask_started": ("node_started", "compare_subtask", "subtask:start"),
                        "subtask_finished": ("node_finished", "compare_subtask", "subtask:finish"),
                        "merge_started": ("node_started", "merge_comparison", "merge:start"),
                        "merge_finished": ("node_finished", "merge_comparison", "merge:finish"),
                    }
                    event_type, node, suffix = event_map[event]
                    ordinal = int(data.get("ordinal", 0))
                    public: dict[str, Any] = {
                        "node": node,
                        "public_stage": (
                            "comparison_planning"
                            if node == "plan_comparison"
                            else "comparison_subtask"
                            if node == "compare_subtask"
                            else "comparison_merge"
                        ),
                        "orchestration_version": SPECIALIST_ORCHESTRATION_VERSION,
                    }
                    for key in (
                        "ordinal",
                        "total",
                        "paper_count",
                        "subtask_total",
                        "status",
                        "finding_count",
                        "evidence_count",
                        "input_tokens",
                        "output_tokens",
                        "provider_input_tokens",
                        "provider_output_tokens",
                        "succeeded_subtasks",
                        "failed_subtasks",
                        "timeout_subtasks",
                        "dedup_count",
                        "conflict_count",
                        "paper_coverage_count",
                        "duration_ms",
                        "recovered",
                    ):
                        if key in data:
                            public[key] = data[key]
                    if ordinal:
                        public["subtask_id"] = f"s{ordinal}"
                    if event == "subtask_finished" and data.get("status") != "succeeded":
                        public["error_category"] = (
                            "timeout" if data.get("status") == "timeout" else "invalid_output"
                        )
                    if event == "merge_finished":
                        raw_status = str(data.get("status", "failed"))
                        public["status"] = "completed" if raw_status == "succeeded" else raw_status
                        public["partial_failure"] = raw_status == "partial"
                        public["fallback_to_v1"] = raw_status == "failed"
                    key_part = f":s{ordinal}" if ordinal else ""
                    await repository.append_agent_run_event(
                        run.id,
                        event_type,
                        public,
                        event_key=(f"stage:compare:{compare_event_epoch}:{suffix}{key_part}"),
                        claim_token=claim_token,
                    )

                specialist_initial = {
                    "run_id": run.id,
                    "parent_thread_id": run.thread_id,
                    "user_id": run.user_id,
                    "objective": resolution.resolved_query,
                    "paper_ids": paper_ids,
                    "dimensions": ["研究问题", "核心方法", "实验设置", "主要结果", "局限"],
                    "max_branches": int(harness_flags.get("multi_agent_max_branches", 3)),
                    "total_token_budget": int(harness_flags.get("multi_agent_token_budget", 12000)),
                    "branch_results": {},
                }
                try:
                    specialist_state = await _invoke_specialist_graph_with_cancel(
                        repository,
                        run,
                        claim_token,
                        research_graph,
                        specialist_initial,
                        ResearchGraphContext(
                            retriever=specialist_retriever,
                            specialist=evidence_specialist,
                            lease_guard=specialist_lease_guard,
                            event_sink=specialist_event_sink,
                            branch_timeout_seconds=float(
                                harness_flags.get("specialist_agent_timeout_seconds", 45)
                            ),
                        ),
                        timeout_seconds=float(
                            harness_flags.get("specialist_total_timeout_seconds", 150)
                        ),
                    )
                    if str(specialist_state.get("status", "")) in {"succeeded", "partial"}:
                        compare_result = specialist_result_from_state(specialist_state)
                        specialist_v3_used = not compare_result.fallback_required
                    else:
                        compare_fallback_reason = str(
                            specialist_state.get("fallback_reason") or "all_subtasks_failed"
                        )
                except asyncio.CancelledError:
                    current = await repository.get_agent_run(run.id)
                    if current and (current.cancel_requested or current.status == "cancelled"):
                        return
                    raise
                except TimeoutError:
                    compare_fallback_reason = "timeout"
                except Exception:
                    compare_fallback_reason = "internal_error"

            if specialist_v3_used and compare_result is not None:
                evidence_per_paper: dict[str, int] = {}
                bounded_specialist_evidence: list[Evidence] = []
                for item in compare_result.evidence:
                    count = evidence_per_paper.get(item.paper_id, 0)
                    if count >= 3 or len(bounded_specialist_evidence) >= 9:
                        continue
                    evidence_per_paper[item.paper_id] = count + 1
                    bounded_specialist_evidence.append(item)
                tasks_by_id = {task.subtask_id: task for task in compare_result.plan.tasks}
                synthesis_findings = []
                for item in compare_result.report.findings:
                    if item.status != "succeeded" or not item.claim.strip():
                        continue
                    task = tasks_by_id.get(item.subtask_id)
                    task_paper_ids = set(task.paper_ids) if task is not None else set()
                    synthesis_findings.append(
                        {
                            "papers": sorted(
                                {
                                    evidence.paper_title
                                    for evidence in bounded_specialist_evidence
                                    if evidence.paper_id in task_paper_ids
                                }
                            ),
                            "claim": item.claim[:1500],
                            "stance": item.stance,
                            "confidence": round(float(item.confidence), 3),
                        }
                    )
                synthesis_conflicts = []
                for conflict in compare_result.conflict_sets[:4]:
                    sides: dict[str, list[dict[str, Any]]] = {}
                    for stance in ("support", "contradict", "uncertain"):
                        rendered = []
                        for claim in list(conflict.get(stance, []))[:3]:
                            paper_ids_for_claim = set(claim.get("paper_ids", []))
                            rendered.append(
                                {
                                    "papers": sorted(
                                        {
                                            evidence.paper_title
                                            for evidence in bounded_specialist_evidence
                                            if evidence.paper_id in paper_ids_for_claim
                                        }
                                    ),
                                    "claim": str(claim.get("claim", ""))[:500],
                                    "confidence": round(float(claim.get("confidence", 0.0)), 3),
                                }
                            )
                        if rendered:
                            sides[stance] = rendered
                    if sides.get("support") and sides.get("contradict"):
                        synthesis_conflicts.append(
                            {
                                "dimension": str(conflict.get("dimension", ""))[:64],
                                "claim_key": str(conflict.get("claim_key", ""))[:160],
                                **sides,
                            }
                        )
                tool_mode_active = True
                initial["tool_mode_active"] = True
                initial["pre_retrieved_evidence"] = bounded_specialist_evidence
                initial["tool_steps"] = len(compare_result.plan.tasks)
                # v3 的最终综合器使用 fresh context：只接收已解析的问题、当前
                # Skill 与服务端复验后的合并证据，不继承聊天历史、Memory 或兄弟
                # Specialist 的自由文本。会话约束已经固化进 resolved_query。
                initial["messages"] = []
                if skill_instructions:
                    initial["messages"].append({"role": "skill", "content": skill_instructions})
                if synthesis_findings:
                    initial["messages"].append(
                        {
                            "role": "research_synthesis",
                            "content": json.dumps(
                                {
                                    "findings": synthesis_findings,
                                    "conflicts": synthesis_conflicts,
                                    "coverage_notice": (
                                        compare_result.report.coverage_notice or ""
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                initial["memory_ids"] = []
                initial["stage_timings_ms"]["retrieval"] = round(
                    (time.perf_counter() - specialist_started_at) * 1000
                )
                if compare_result.report.coverage_notice:
                    initial["tool_context_entries"] = [
                        {
                            "kind": "specialist_coverage",
                            "status": compare_result.report.status,
                            "summary": compare_result.report.coverage_notice,
                        }
                    ]
                harness_trace.update(_parallel_compare_trace(compare_result, fallback_reason=None))
                harness_trace["orchestration_version"] = SPECIALIST_ORCHESTRATION_VERSION
                harness_trace["compare_mode"] = "bounded_specialists"
                harness_trace["tool_mode_active"] = True
                harness_trace["tool_output_used"] = True
                harness_trace["tool_activation_reason"] = "specialist_evidence"
                harness_trace["function_calling"] = "specialist_subgraph"
                harness_trace["synthesis_context"] = "fresh"
                harness_trace["synthesis_evidence_count"] = len(bounded_specialist_evidence)
                harness_trace["specialist_conflict_set_count"] = len(compare_result.conflict_sets)
            else:
                research_fallback_to_v1 = True
                harness_trace.update(
                    _parallel_compare_trace(None, fallback_reason=compare_fallback_reason)
                )
                harness_trace["orchestration_version"] = SPECIALIST_ORCHESTRATION_VERSION
                harness_trace["compare_mode"] = "bounded_specialists"
                harness_trace["function_calling"] = "legacy_fallback"
                harness_trace["function_fallback_reason"] = (
                    compare_fallback_reason or "no_merged_evidence"
                )
            await repository.update_agent_skill(
                run.id,
                claim_token,
                selected_skill=selected_skill,
                skill_version=skill_version,
                harness_trace=harness_trace,
            )

        if compare_requested and not resolution.needs_clarification:
            research_orchestration_attempted = True
            compare_started_at = time.perf_counter()
            if function_tool_harness is None:
                compare_fallback_reason = "internal_error"
            else:
                try:
                    compare_result = await _invoke_parallel_compare_with_cancel(
                        repository,
                        run,
                        claim_token,
                        _execute_parallel_compare(
                            repository,
                            run,
                            claim_token,
                            query=resolution.resolved_query,
                            paper_ids=[str(value) for value in snapshot.get("paper_ids", [])],
                            retriever=function_tool_harness.retriever,
                            harness_flags=harness_flags,
                            retrieval_config=retrieval_config,
                            event_epoch=compare_event_epoch,
                        ),
                    )
                    if compare_result.fallback_required:
                        compare_fallback_reason = "all_subtasks_failed"
                    else:
                        compare_v2_used = True
                except ResearchLeaseLostError as error:
                    raise asyncio.CancelledError from error
                except TimeoutError:
                    compare_fallback_reason = "timeout"
                except (ValueError, TypeError):
                    compare_fallback_reason = "invalid_plan"
                except asyncio.CancelledError:
                    current = await repository.get_agent_run(run.id)
                    if current and (current.cancel_requested or current.status == "cancelled"):
                        return
                    raise
                except Exception:
                    compare_fallback_reason = "internal_error"

            harness_trace.update(
                _parallel_compare_trace(
                    compare_result,
                    fallback_reason=compare_fallback_reason,
                )
            )
            if compare_v2_used and compare_result is not None:
                tool_mode_active = True
                initial["tool_mode_active"] = True
                initial["pre_retrieved_evidence"] = list(compare_result.evidence)
                initial["tool_steps"] = len(compare_result.plan.tasks)
                initial["stage_timings_ms"]["retrieval"] = round(
                    (time.perf_counter() - compare_started_at) * 1000
                )
                if compare_result.report.coverage_notice:
                    initial["tool_context_entries"] = [
                        {
                            "kind": "parallel_compare_coverage",
                            "status": compare_result.report.status,
                            "summary": compare_result.report.coverage_notice,
                        }
                    ]
                harness_trace["tool_mode_active"] = True
                harness_trace["tool_output_used"] = True
                harness_trace["tool_activation_reason"] = "parallel_compare_evidence"
                harness_trace["function_calling"] = "parallel_map_reduce"
            else:
                # v2 未获得合法证据时保留安全事件和聚合轨迹，随后进入原有
                # Function Tool/legacy retrieval，而不是空回答或重复创建子 Run。
                harness_trace["function_calling"] = "legacy_fallback"
                harness_trace["function_fallback_reason"] = (
                    compare_fallback_reason or "no_merged_evidence"
                )
                await repository.append_agent_run_event(
                    run.id,
                    "node_started",
                    {
                        "node": "merge_comparison",
                        "public_stage": "comparison_merge",
                        "orchestration_version": ORCHESTRATION_VERSION,
                    },
                    event_key=f"stage:compare:{compare_event_epoch}:merge:start",
                    claim_token=claim_token,
                )
                await repository.append_agent_run_event(
                    run.id,
                    "node_finished",
                    {
                        "node": "merge_comparison",
                        "public_stage": "comparison_merge",
                        "status": "failed",
                        "fallback_to_v1": True,
                        "fallback_reason": compare_fallback_reason or "no_merged_evidence",
                        "orchestration_version": ORCHESTRATION_VERSION,
                    },
                    event_key=f"stage:compare:{compare_event_epoch}:merge:finish",
                    claim_token=claim_token,
                )
            await repository.update_agent_skill(
                run.id,
                claim_token,
                selected_skill=selected_skill,
                skill_version=skill_version,
                harness_trace=harness_trace,
            )
        if (
            not resolution.needs_clarification
            and harness_flags.get("function_tools_enabled")
            and definition is not None
            and function_tool_harness is not None
            and not research_orchestration_attempted
        ):
            tool_started_at = time.perf_counter()
            scope_paper_titles = list(initial.get("scope_paper_titles", []))
            # get_page_text 的模型参数必须使用服务端论文 ID；同时保留可信标题，
            # 以便对模型偶尔返回标题而非 ID 的情况做单论文、无歧义的受控解析。
            # 这份元数据也服务于外部学术检索，因此不能只在 web_enabled 时构建。
            try:
                tool_loop_result = await _invoke_tools_with_cancel(
                    repository,
                    run,
                    function_tool_harness,
                    resolution.resolved_query,
                    ToolExecutionContext(
                        run_id=run.id,
                        claim_token=claim_token,
                        user_id=run.user_id,
                        skill=definition,
                        allowed_paper_ids=tuple(snapshot.get("paper_ids", [])),
                        current_paper_id=str(
                            dict(snapshot.get("client_context", {})).get("paper_id") or ""
                        )
                        or (
                            str(snapshot.get("paper_ids", [""])[0])
                            if len(snapshot.get("paper_ids", [])) == 1
                            else None
                        ),
                        web_enabled=bool(snapshot.get("web_enabled", False)),
                        scope_paper_titles=tuple(scope_paper_titles),
                        scope_paper_texts=tuple(initial.get("scope_paper_texts", [])),
                        excluded_recommendation_entities=frozenset(
                            initial.get("excluded_recommendation_entities", [])
                        ),
                        previous_recommendation_entities=frozenset(
                            str(value)
                            for value in discovery_task.get("shown_entities", [])
                            if str(value).strip()
                        ),
                        discovery_task=discovery_task,
                        provider_policy=provider_policy,
                        retrieval_config=retrieval_config,
                        verified_selection_page=(
                            int(selection_page) if selection_page is not None else None
                        ),
                        selection_scope_locked=selection_scope_locked,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                tool_loop_result = ToolLoopResult(
                    provider_supported=False,
                    fallback_reason="function_tool_loop_failed",
                )
            tool_mode_active = tool_loop_result.tool_mode_active
            harness_trace["tool_mode_active"] = tool_mode_active
            harness_trace["native_function_calling_attempted"] = (
                tool_loop_result.native_function_calling_attempted
            )
            harness_trace["explicit_source_fallback_used"] = (
                tool_loop_result.explicit_source_fallback_used
            )
            harness_trace["automatic_source_fallback_used"] = (
                tool_loop_result.automatic_source_fallback_used
            )
            harness_trace["tool_output_used"] = tool_mode_active
            harness_trace["tool_activation_reason"] = tool_loop_result.activation_reason
            initial["provider_policy"] = dict(tool_loop_result.provider_policy or provider_policy)
            harness_trace["provider_policy"] = provider_policy_snapshot(
                tool_loop_result.provider_policy or provider_policy
            )
            # 即使工具结果不可用于激活 Tool Mode，也保留成对且已清洗的状态结果，
            # 让回答能准确说明“OpenAlex 缺少 Key”等降级原因；旧检索仍照常执行。
            initial["tool_context_entries"] = list(tool_loop_result.context_entries)
            initial["tool_steps"] = tool_loop_result.steps
            harness_trace["tool_calls"] = [
                {
                    "tool": str(item.get("tool", "unknown")),
                    "status": str(item.get("status", "unknown")),
                }
                for item in tool_loop_result.calls
            ]
            if tool_mode_active:
                initial["tool_mode_active"] = True
                initial["pre_retrieved_evidence"] = list(tool_loop_result.evidence)
                initial["pre_arxiv_candidates"] = list(tool_loop_result.arxiv_candidates)
                initial["tool_calls"] = list(tool_loop_result.calls)
                initial["stage_timings_ms"]["retrieval"] = round(
                    (time.perf_counter() - tool_started_at) * 1000
                )
                harness_trace["function_calling"] = (
                    "native"
                    if tool_loop_result.native_function_calling_attempted
                    and tool_loop_result.provider_supported
                    else "deterministic_harness"
                )
            else:
                harness_trace["function_calling"] = (
                    "native_unused"
                    if tool_loop_result.native_function_calling_attempted
                    else "legacy_fallback"
                )
                harness_trace["function_fallback_reason"] = (
                    tool_loop_result.fallback_reason or "model_selected_no_tool"
                )
            await repository.update_agent_skill(
                run.id,
                claim_token,
                selected_skill=selected_skill,
                skill_version=skill_version,
                harness_trace=harness_trace,
            )

        if resolution.needs_clarification:
            await repository.append_agent_run_event(
                run_id,
                "node_started",
                {"node": "resolve_context", "stage": "确认问题所指内容"},
                event_key="stage:context:start",
                claim_token=claim_token,
            )
        else:
            await repository.append_agent_run_event(
                run_id,
                "node_started",
                {"node": "retrieve_library", "stage": "检索文献证据"},
                event_key="stage:retrieve:start",
                claim_token=claim_token,
            )
            if tool_loop_result is not None:
                for index, item in enumerate(tool_loop_result.calls):
                    await repository.append_agent_run_event(
                        run_id,
                        "tool_started",
                        {
                            "tool": item.get("tool", "unknown"),
                            "call_index": index,
                        },
                        event_key=f"stage:function_tool:{index}:start",
                        claim_token=claim_token,
                    )
                    await repository.append_agent_run_event(
                        run_id,
                        "tool_finished",
                        {
                            "tool": item.get("tool", "unknown"),
                            "call_index": index,
                            "status": item.get("status", "unknown"),
                            "evidence_count": item.get("evidence_count", 0),
                        },
                        event_key=f"stage:function_tool:{index}:finish",
                        claim_token=claim_token,
                    )
            if not tool_mode_active:
                await repository.append_agent_run_event(
                    run_id,
                    "tool_started",
                    {"tool": "search_library"},
                    event_key="stage:tool:search:start",
                    claim_token=claim_token,
                )
        if tool_loop_result is not None and tool_loop_result.pending_action:
            await _finish_observed_run(
                repository,
                run_id,
                claim_token,
                started_at=started_at,
                status="interrupted",
                intent=intent,
                scope=scope,
                outcome="interrupted",
                result={
                    "status": "interrupted",
                    "tool_steps": tool_loop_result.steps,
                    "retrieved_evidence": list(tool_loop_result.evidence),
                },
                pending_action=tool_loop_result.pending_action,
                tool_steps=tool_loop_result.steps,
                result_summary={
                    "answer": "",
                    "citations": [],
                    "model_attempts": [item.as_dict() for item in attempts],
                },
            )
            return
        try:
            result = await _invoke_with_cancel(
                repository,
                graph,
                run,
                initial,
                checkpoint_namespace=(
                    "single_agent_v1/fallback"
                    if research_fallback_to_v1
                    else f"{requested_orchestration}/final"
                ),
            )
        except asyncio.CancelledError:
            # 用户取消由 cancel API 在数据库中原子落终态。租约丢失、进程退出等
            # 外部取消不能由旧 Worker 无 token 改写 Run，否则会取消新 Worker。
            current = await repository.get_agent_run(run.id)
            if current and current.cancel_requested:
                return
            raise
        except ModelRuntimeError as error:
            await _finish_observed_run(
                repository,
                run_id,
                claim_token,
                started_at=started_at,
                status="failed",
                intent=intent,
                scope=scope,
                outcome="model_failed",
                error_code=error.error_code,
                result=None,
                stage_timings_ms={"intent": intent_ms},
                result_summary={
                    "answer": "",
                    "citations": [],
                    "model_attempts": [item.as_dict() for item in attempts],
                },
            )
            return
        except Exception:
            await _finish_observed_run(
                repository,
                run_id,
                claim_token,
                started_at=started_at,
                status="failed",
                intent=intent,
                scope=scope,
                outcome="internal_failed",
                error_code="AGENT_RUN_FAILED",
                result=None,
                stage_timings_ms={"intent": intent_ms},
                result_summary={"answer": "", "citations": []},
            )
            return
    # Retrieval 配置是每个 Run 的冻结输入。LangGraph Checkpoint 或旧图状态
    # 即使没有回传该字段，终态 trace 也必须使用本次 Run 的快照，不能退回
    # 当前部署配置或写成 unknown。
    result["retrieval_config"] = retrieval_config
    model_attempts = [item.as_dict() for item in attempts]
    stage_timings = dict(result.get("stage_timings_ms", {}))
    if result.get("provider_policy"):
        harness_trace["provider_policy"] = provider_policy_snapshot(result.get("provider_policy"))
        await repository.update_agent_skill(
            run.id,
            claim_token,
            selected_skill=selected_skill,
            skill_version=skill_version,
            harness_trace=harness_trace,
        )
    final_context_usage = dict(result.get("context_usage", {}) or {})
    if final_context_usage:
        context_snapshot["usage"] = {
            **dict(context_snapshot.get("usage", {})),
            **final_context_usage,
        }
        harness_trace["context_budget"] = {
            "final_input_tokens": final_context_usage.get("final_input_tokens", 0),
            "hard_limit": final_context_usage.get("hard_limit", 0),
            "compression_actions": list(final_context_usage.get("compression_actions", [])),
            "dropped_messages": final_context_usage.get("dropped_messages", 0),
            "dropped_evidence": final_context_usage.get("dropped_evidence", 0),
            "dropped_tool_pairs": final_context_usage.get("dropped_tool_pairs", 0),
        }
        await repository.update_agent_context(
            run.id,
            claim_token,
            context_snapshot=context_snapshot,
            resolved_query=resolution.resolved_query,
            reference_confidence=resolution.confidence,
        )
        await repository.update_agent_skill(
            run.id,
            claim_token,
            selected_skill=selected_skill,
            skill_version=skill_version,
            harness_trace=harness_trace,
        )
    if resolution.needs_clarification:
        await repository.append_agent_run_event(
            run_id,
            "node_finished",
            {
                "node": "resolve_context",
                "stage": "需要补充上下文",
                "duration_ms": context_ms,
            },
            event_key="stage:context:finish",
            claim_token=claim_token,
        )
    else:
        if not tool_mode_active:
            await repository.append_agent_run_event(
                run_id,
                "tool_finished",
                {
                    "tool": "search_library",
                    "evidence_count": len(result.get("retrieved_evidence", [])),
                    "duration_ms": stage_timings.get("retrieval"),
                },
                event_key="stage:tool:search:finish",
                claim_token=claim_token,
            )
        await repository.append_agent_run_event(
            run_id,
            "node_finished",
            {
                "node": "retrieve_library",
                "stage": "检索文献证据",
                "duration_ms": stage_timings.get("retrieval"),
            },
            event_key="stage:retrieve:finish",
            claim_token=claim_token,
        )
    interrupts = result.get("__interrupt__", [])
    pending_action = result.get("pending_action")
    if interrupts:
        pending_action = getattr(interrupts[0], "value", pending_action or {})
    if pending_action or result.get("status") == "interrupted":
        await _finish_observed_run(
            repository,
            run_id,
            claim_token,
            started_at=started_at,
            status="interrupted",
            intent=intent,
            scope=scope,
            outcome="interrupted",
            result=result,
            pending_action=pending_action or {},
            tool_steps=int(result.get("tool_steps", 0)),
            result_summary={
                "answer": "",
                "citations": [],
                "evidence_quality": dict(result.get("evidence_quality", {})),
                "model_attempts": model_attempts,
            },
        )
        return

    answer = str(result.get("answer", "")).strip()
    evidence = list(result.get("retrieved_evidence", []))
    citations = list(result.get("citations", []))
    quality = dict(result.get("evidence_quality", {}))
    allowed_paper_ids = set(snapshot.get("paper_ids", []))
    if any(item.paper_id not in allowed_paper_ids for item in evidence):
        await _finish_observed_run(
            repository,
            run_id,
            claim_token,
            started_at=started_at,
            status="failed",
            intent=intent,
            scope=scope,
            outcome="scope_violation",
            error_code="EVIDENCE_SCOPE_VIOLATION",
            result={**result, "citations": []},
            result_summary={
                "answer": "",
                "citations": [],
                "evidence_quality": quality,
                "model_attempts": model_attempts,
            },
        )
        return
    paragraphs = _answer_paragraphs(answer)
    validated: list[tuple[str, str, list[CitationClaim]]] = []
    dropped_paragraphs = 0
    external_metadata_answer = bool(result.get("external_metadata_answer"))
    answerability_abstained = result.get("answerability_status") == "unanswerable"
    semantic_support_suppressed = str(
        quality.get("answer_support_grade", "")
    ) == "unsupported" and str(quality.get("reason_code", "")) not in {
        "citation_validation_failed",
        "missing_claim_citations",
    }
    await repository.append_agent_run_event(
        run_id,
        "node_started",
        {
            "node": "generate_answer",
            "stage": "生成候选回答",
            "duration_ms": stage_timings.get("generation"),
        },
        event_key="stage:generate:start",
        claim_token=claim_token,
    )
    await repository.append_agent_run_event(
        run_id,
        "node_finished",
        {"node": "generate_answer", "stage": "生成候选回答"},
        event_key="stage:generate:finish",
        claim_token=claim_token,
    )
    await repository.append_agent_run_event(
        run_id,
        "node_started",
        {"node": "validate_citations", "stage": "核验证据与引用"},
        event_key="stage:validate:start",
        claim_token=claim_token,
    )
    citation_validation_started_at = time.perf_counter()
    if external_metadata_answer:
        validated.append((answer, "external_metadata", []))
    elif answerability_abstained:
        validated.append((answer, "controlled_notice", []))
    elif semantic_support_suppressed:
        validated.append((answer, "controlled_notice", []))
    else:
        for paragraph in paragraphs:
            valid, classification, paragraph_citations = _validate_publishable_paragraph(
                paragraph,
                citations,
                evidence,
                quality,
                answer_quality_policy,
            )
            if not valid:
                # 只丢弃未带合法来源的自然段，不再让一个漏引的开场白覆盖整篇已经
                # 通过引用 ID/论文/页码校验的回答。至少需要保留一个有引用的事实段落。
                dropped_paragraphs += 1
                continue
            validated.append((paragraph, classification, paragraph_citations))
    stage_timings["citation_validation"] = round(
        (time.perf_counter() - citation_validation_started_at) * 1000
    )
    has_cited_answer = any(classification == "cited_answer" for _, classification, _ in validated)
    if (
        evidence
        and not has_cited_answer
        and not external_metadata_answer
        and not answerability_abstained
        and not semantic_support_suppressed
    ):
        await _finish_observed_run(
            repository,
            run_id,
            claim_token,
            started_at=started_at,
            status="failed",
            intent=intent,
            scope=scope,
            outcome="unverified_answer",
            error_code="UNVERIFIED_ANSWER",
            result={**result, "citations": [], "stage_timings_ms": stage_timings},
            stage_timings_ms=stage_timings,
            result_summary={
                "answer": "",
                "citations": [],
                "evidence_quality": quality,
                "model_attempts": model_attempts,
                "dropped_paragraph_count": dropped_paragraphs,
            },
        )
        return
    await repository.append_agent_run_event(
        run_id,
        "node_finished",
        {
            "node": "validate_citations",
            "stage": "核验证据与引用",
            "duration_ms": stage_timings.get("citation_validation"),
        },
        event_key="stage:validate:finish",
        claim_token=claim_token,
    )

    all_citation_dicts: dict[str, dict[str, Any]] = {}
    for index, (paragraph, classification, paragraph_citations) in enumerate(validated):
        citation_values = _citation_dicts(paragraph_citations, evidence)
        published = await repository.publish_agent_paragraph(
            run_id,
            index,
            paragraph,
            citation_values,
            classification,
            claim_token,
        )
        if not published:
            return
        for item in citation_values:
            all_citation_dicts[item["chunk_id"]] = item
    for chunk_id, citation in all_citation_dicts.items():
        await repository.append_agent_run_event(
            run_id,
            "citation",
            citation,
            event_key=f"citation:{chunk_id}",
            claim_token=claim_token,
        )
    result_status = str(result.get("status", "completed"))
    if result_status != "completed":
        result_status = "failed"
    published_answer = "\n\n".join(item[0] for item in validated)
    outcome = (
        "external_metadata"
        if external_metadata_answer
        else ("cited_answer" if all_citation_dicts else "abstained")
    )
    finished_run = await _finish_observed_run(
        repository,
        run_id,
        claim_token,
        started_at=started_at,
        status=result_status,
        intent=intent,
        scope=scope,
        outcome=outcome,
        result={
            **result,
            "citations": list(all_citation_dicts.values()),
            "stage_timings_ms": stage_timings,
        },
        stage_timings_ms=stage_timings,
        tool_steps=int(result.get("tool_steps", 0)),
        error_code=result.get("error"),
        result_summary={
            "answer": published_answer,
            "citations": list(all_citation_dicts.values()),
            "evidence_quality": quality,
            "model_attempts": model_attempts,
            "dropped_paragraph_count": dropped_paragraphs,
            "displayed_recommendations": list(result.get("displayed_recommendations", []) or []),
        },
    )
    exposed_entities_for_state = [
        str(value)
        for value in result.get("displayed_recommendation_entities", []) or []
        if str(value).strip()
    ]
    if finished_run and result_status == "completed":
        try:
            await repository.update_session_compaction(
                run.session_id,
                run.user_id,
                compact_summary=dict(getattr(chat_session, "compact_summary", {}) or {}),
                compacted_through_message_id=getattr(
                    chat_session, "compacted_through_message_id", None
                ),
                entity_state=_next_entity_state(
                    dict(getattr(chat_session, "entity_state", {}) or {}),
                    resolution,
                    query,
                    selected_skill=selected_skill,
                    web_enabled=bool(snapshot.get("web_enabled", False)),
                    exposed_recommendation_entities=(exposed_entities_for_state),
                ),
            )
        except Exception:
            # 讨论实体是可重建的上下文缓存，不能改写已经核验的回答终态。
            pass
    if result_status == "completed" and memory_allowed:
        try:
            await _save_run_memories(repository, harness_config, run, query)
        except Exception:
            # 记忆是可重建的异步增强，不能把已经核验并发布的回答改成失败。
            return
