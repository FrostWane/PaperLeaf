"""使用 LangGraph Send 与确定性 reducer 的有界 Specialist Research Graph。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from ..rag.citations import Evidence
from .research_specialists import (
    EvidenceSpecialist,
    SpecialistAnalysis,
    SpecialistBudgetError,
    SpecialistOutputError,
)
from .research_synthesis import (
    FindingPacket,
    MergeReport,
    ResearchPlan,
    ResearchSynthesisResult,
    ResearchTask,
    build_deterministic_research_plan,
    merge_findings,
)

SPECIALIST_ORCHESTRATION_VERSION = "specialist_subgraph_v3"
_CLAIM_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)

EvidenceRetriever = Callable[[ResearchTask], Awaitable[Sequence[Evidence]]]
LeaseGuard = Callable[[], bool | Awaitable[bool]]
EventSink = Callable[[str, dict[str, Any]], None | Awaitable[None]]


def _payload_hash(value: Mapping[str, Any]) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def merge_branch_results(
    current: dict[str, dict[str, Any]] | None,
    update: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """可交换、结合且幂等的分支 reducer，避免并行 last-write-wins。"""

    merged = dict(current or {})
    status_rank = {"failed": 0, "timeout": 1, "succeeded": 2}
    for subtask_id, candidate in sorted((update or {}).items()):
        incoming = dict(candidate)
        existing = merged.get(subtask_id)
        if existing is None:
            merged[subtask_id] = incoming
            continue
        existing_rank = (
            int(existing.get("generation", 0)),
            status_rank.get(str(existing.get("status", "failed")), -1),
            _payload_hash(existing),
        )
        incoming_rank = (
            int(incoming.get("generation", 0)),
            status_rank.get(str(incoming.get("status", "failed")), -1),
            _payload_hash(incoming),
        )
        if incoming_rank > existing_rank:
            merged[subtask_id] = incoming
    return {key: merged[key] for key in sorted(merged)}


class ResearchGraphState(TypedDict, total=False):
    run_id: str
    parent_thread_id: str
    user_id: str
    objective: str
    paper_ids: list[str]
    dimensions: list[str]
    max_branches: int
    total_token_budget: int
    plan: dict[str, Any]
    branch_results: Annotated[dict[str, dict[str, Any]], merge_branch_results]
    merge_report: dict[str, Any]
    conflict_sets: list[dict[str, Any]]
    branch_metrics: list[dict[str, Any]]
    merged_evidence: list[dict[str, Any]]
    status: Literal["planning", "running", "succeeded", "partial", "failed"]
    fallback_reason: str | None


class ResearchBranchState(TypedDict):
    run_id: str
    parent_thread_id: str
    subtask: dict[str, Any]
    generation: int
    ordinal: int
    total: int


class ResearchBranchCheckpointState(TypedDict, total=False):
    run_id: str
    parent_thread_id: str
    subtask: dict[str, Any]
    generation: int
    ordinal: int
    total: int
    envelope: dict[str, Any]
    status: Literal["pending", "terminal"]


@dataclass(frozen=True)
class ResearchGraphContext:
    retriever: EvidenceRetriever
    specialist: EvidenceSpecialist
    lease_guard: LeaseGuard | None = None
    event_sink: EventSink | None = None
    branch_timeout_seconds: float = 45.0


def _evidence_to_dict(item: Evidence) -> dict[str, Any]:
    return {
        "chunk_id": item.chunk_id,
        "paper_id": item.paper_id,
        "paper_title": item.paper_title,
        "physical_page": item.physical_page,
        "text": item.text,
        "retrieval_score": item.retrieval_score,
        "retrieval_channels": list(item.retrieval_channels),
        "channel_scores": [list(value) for value in item.channel_scores],
        "retrieval_query": item.retrieval_query,
        "chunking_strategy": item.chunking_strategy,
        "vector_fallback_reason": item.vector_fallback_reason,
        "retrieval_processors": list(item.retrieval_processors),
        "query_rewrite_reasons": list(item.query_rewrite_reasons),
        "reranker_fallback_reason": item.reranker_fallback_reason,
    }


def _evidence_from_dict(value: Mapping[str, Any]) -> Evidence:
    return Evidence(
        chunk_id=str(value.get("chunk_id", "")),
        paper_id=str(value.get("paper_id", "")),
        paper_title=str(value.get("paper_title", "")),
        physical_page=int(value.get("physical_page", 0)),
        text=str(value.get("text", "")),
        retrieval_score=float(value.get("retrieval_score", 0.0)),
        retrieval_channels=tuple(str(item) for item in value.get("retrieval_channels", [])),
        channel_scores=tuple(
            (str(item[0]), float(item[1]))
            for item in value.get("channel_scores", [])
            if isinstance(item, list | tuple) and len(item) == 2
        ),
        retrieval_query=str(value.get("retrieval_query", "")),
        chunking_strategy=str(value.get("chunking_strategy", "unknown")),
        vector_fallback_reason=(
            str(value["vector_fallback_reason"])
            if value.get("vector_fallback_reason") is not None
            else None
        ),
        retrieval_processors=tuple(
            str(item) for item in value.get("retrieval_processors", [])
        ),
        query_rewrite_reasons=tuple(
            str(item) for item in value.get("query_rewrite_reasons", [])
        ),
        reranker_fallback_reason=(
            str(value["reranker_fallback_reason"])
            if value.get("reranker_fallback_reason") is not None
            else None
        ),
    )


async def _ensure_lease(guard: LeaseGuard | None) -> None:
    if guard is None:
        return
    current = guard()
    allowed = await current if inspect.isawaitable(current) else current
    if not bool(allowed):
        raise asyncio.CancelledError


async def _emit(context: ResearchGraphContext, event: str, data: dict[str, Any]) -> None:
    if context.event_sink is None:
        return
    result = context.event_sink(event, data)
    if inspect.isawaitable(result):
        await result


def _failure_envelope(
    task: ResearchTask,
    generation: int,
    status: Literal["timeout", "failed"],
    error_code: str,
) -> dict[str, Any]:
    finding = FindingPacket(
        subtask_id=task.subtask_id,
        status=status,
        error_code=error_code,
    )
    return {
        "subtask_id": task.subtask_id,
        "generation": generation,
        "status": status,
        "finding": finding.model_dump(mode="json"),
        "claims": [],
        "evidence": [],
        "usage": {},
    }


def _success_envelope(
    task: ResearchTask, generation: int, analysis: SpecialistAnalysis
) -> dict[str, Any]:
    return {
        "subtask_id": task.subtask_id,
        "generation": generation,
        "status": "succeeded",
        "finding": analysis.finding.model_dump(mode="json"),
        "claims": [item.model_dump(mode="json") for item in analysis.claims],
        "evidence": [_evidence_to_dict(item) for item in analysis.evidence],
        "usage": analysis.usage.model_dump(mode="json"),
    }


def _claim_tokens(value: str) -> set[str]:
    raw = _CLAIM_TOKEN_RE.findall(value.casefold())
    latin = {item for item in raw if len(item) > 1 and item.isascii()}
    chinese = [item for item in raw if not item.isascii()]
    bigrams = {"".join(chinese[index : index + 2]) for index in range(max(0, len(chinese) - 1))}
    return latin | bigrams | set(chinese if len(chinese) <= 2 else [])


def _claim_similarity(left: str, right: str) -> float:
    left_tokens = _claim_tokens(left)
    right_tokens = _claim_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _build_conflict_sets(
    branch_results: Mapping[str, Mapping[str, Any]],
    *,
    allowed_paper_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """保留相反证据，不让 reducer 或合并器用单一主张覆盖冲突。"""

    allowed = set(allowed_paper_ids)
    candidates: list[dict[str, Any]] = []
    for subtask_id, envelope in sorted(branch_results.items()):
        evidence = {
            str(item.get("chunk_id", "")): item
            for item in envelope.get("evidence", [])
            if isinstance(item, Mapping)
        }
        for raw_claim in envelope.get("claims", []):
            if not isinstance(raw_claim, Mapping):
                continue
            chunk_ids = tuple(
                chunk_id
                for chunk_id in dict.fromkeys(str(item) for item in raw_claim.get("chunk_ids", []))
                if chunk_id in evidence and str(evidence[chunk_id].get("paper_id", "")) in allowed
            )
            paper_ids = tuple(
                dict.fromkeys(str(evidence[chunk_id].get("paper_id", "")) for chunk_id in chunk_ids)
            )
            stance = str(raw_claim.get("stance", "unclear"))
            if not chunk_ids or not paper_ids or stance not in {"support", "contradict", "unclear"}:
                continue
            candidates.append(
                {
                    "subtask_id": subtask_id,
                    "dimension": str(raw_claim.get("dimension", ""))[:64],
                    "claim_key": str(raw_claim.get("claim_key", ""))[:160],
                    "claim": str(raw_claim.get("claim", ""))[:1000],
                    "stance": stance,
                    "confidence": max(0.0, min(1.0, float(raw_claim.get("confidence", 0.0)))),
                    "chunk_ids": list(chunk_ids),
                    "paper_ids": list(paper_ids),
                }
            )

    groups: list[dict[str, Any]] = []
    for candidate in candidates:
        best_index: int | None = None
        best_score = 0.0
        for index, group in enumerate(groups):
            if candidate["dimension"] != group["dimension"]:
                continue
            exact = bool(candidate["claim_key"]) and candidate["claim_key"] == group["claim_key"]
            score = (
                1.0
                if exact
                else _claim_similarity(
                    candidate["claim_key"] or candidate["claim"],
                    group["claim_key"] or group["representative_claim"],
                )
            )
            if score >= 0.42 and score > best_score:
                best_index, best_score = index, score
        if best_index is None:
            groups.append(
                {
                    "dimension": candidate["dimension"],
                    "claim_key": candidate["claim_key"],
                    "representative_claim": candidate["claim"],
                    "items": [candidate],
                }
            )
        else:
            groups[best_index]["items"].append(candidate)

    conflicts: list[dict[str, Any]] = []
    for group in groups:
        items = sorted(
            group["items"],
            key=lambda item: (
                item["stance"],
                item["paper_ids"],
                item["chunk_ids"],
                item["claim"],
            ),
        )
        support = [item for item in items if item["stance"] == "support"]
        contradict = [item for item in items if item["stance"] == "contradict"]
        if not support or not contradict:
            continue
        support_papers = {paper for item in support for paper in item["paper_ids"]}
        contradict_papers = {paper for item in contradict for paper in item["paper_ids"]}
        if len(support_papers | contradict_papers) < 2:
            continue
        key_source = f"{group['dimension']}|{group['claim_key'] or group['representative_claim']}"
        conflicts.append(
            {
                "conflict_id": (f"conflict-{hashlib.sha256(key_source.encode()).hexdigest()[:12]}"),
                "dimension": group["dimension"],
                "claim_key": group["claim_key"] or group["representative_claim"][:160],
                "support": support,
                "contradict": contradict,
                "uncertain": [item for item in items if item["stance"] == "unclear"],
                "paper_count": len(support_papers | contradict_papers),
            }
        )
    return sorted(conflicts, key=lambda item: item["conflict_id"])


def _validate_initial_scope(state: ResearchGraphState) -> tuple[list[str], list[str]]:
    paper_ids = sorted(
        {str(value).strip() for value in state.get("paper_ids", []) if str(value).strip()}
    )
    dimensions = sorted(
        {str(value).strip() for value in state.get("dimensions", []) if str(value).strip()}
    )
    if len(paper_ids) != len(state.get("paper_ids", [])):
        raise ValueError("SPECIALIST_SCOPE_DUPLICATE_OR_EMPTY")
    return paper_ids, dimensions


async def _plan_node(
    state: ResearchGraphState, runtime: Runtime[ResearchGraphContext]
) -> dict[str, Any]:
    await _ensure_lease(runtime.context.lease_guard)
    await _emit(runtime.context, "plan_started", {})
    try:
        paper_ids, dimensions = _validate_initial_scope(state)
        plan = build_deterministic_research_plan(
            str(state.get("objective", "")),
            paper_ids,
            dimensions,
            max_branches=int(state.get("max_branches", 3)),
            total_token_budget=int(state.get("total_token_budget", 6144)),
        )
    except (TypeError, ValueError):
        await _emit(
            runtime.context,
            "plan_finished",
            {"status": "failed", "subtask_total": 0},
        )
        return {"status": "failed", "fallback_reason": "invalid_plan"}
    await _ensure_lease(runtime.context.lease_guard)
    await _emit(
        runtime.context,
        "plan_finished",
        {"status": "completed", "subtask_total": len(plan.tasks)},
    )
    return {
        "plan": plan.model_dump(mode="json"),
        "branch_results": {},
        "status": "running",
        "fallback_reason": None,
    }


def _fan_out(state: ResearchGraphState) -> Any:
    if state.get("status") == "failed" or not state.get("plan"):
        return END
    plan = ResearchPlan.model_validate(state["plan"])
    return [
        Send(
            "specialist_scout",
            {
                "run_id": str(state.get("run_id", "")),
                "parent_thread_id": str(state.get("parent_thread_id", "")),
                "subtask": task.model_dump(mode="json"),
                "generation": 1,
                "ordinal": index,
                "total": len(plan.tasks),
            },
        )
        for index, task in enumerate(plan.tasks, start=1)
    ]


async def _execute_scout(
    state: ResearchBranchState, runtime: Runtime[ResearchGraphContext]
) -> dict[str, Any]:
    task = ResearchTask.model_validate(state["subtask"])
    generation = int(state.get("generation", 1))
    started_at = time.perf_counter()

    async def execute() -> SpecialistAnalysis:
        await _ensure_lease(runtime.context.lease_guard)
        evidence = await runtime.context.retriever(task)
        await _ensure_lease(runtime.context.lease_guard)
        return await runtime.context.specialist.analyze(
            task,
            evidence,
            lease_guard=runtime.context.lease_guard,
        )

    try:
        analysis = await asyncio.wait_for(execute(), timeout=runtime.context.branch_timeout_seconds)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        envelope = _failure_envelope(task, generation, "timeout", "SPECIALIST_TIMEOUT")
    except SpecialistBudgetError:
        envelope = _failure_envelope(task, generation, "failed", "SPECIALIST_BUDGET_EXCEEDED")
    except SpecialistOutputError as error:
        envelope = _failure_envelope(task, generation, "failed", str(error))
    except Exception:
        envelope = _failure_envelope(task, generation, "failed", "SPECIALIST_FAILED")
    else:
        envelope = _success_envelope(task, generation, analysis)
    duration_ms = max(1, round((time.perf_counter() - started_at) * 1000))
    finding = FindingPacket.model_validate(envelope["finding"]).model_copy(
        update={"duration_ms": duration_ms}
    )
    envelope["finding"] = finding.model_dump(mode="json")
    envelope["duration_ms"] = duration_ms
    return envelope


async def _branch_checkpoint_node(
    state: ResearchBranchCheckpointState,
    runtime: Runtime[ResearchGraphContext],
) -> dict[str, Any]:
    envelope = await _execute_scout(state, runtime)  # type: ignore[arg-type]
    return {"envelope": envelope, "status": "terminal"}


async def _emit_scout_finished(
    state: ResearchBranchState,
    context: ResearchGraphContext,
    envelope: Mapping[str, Any],
    *,
    recovered: bool,
) -> None:
    ordinal = int(state.get("ordinal", 1))
    total = int(state.get("total", 1))
    await _emit(
        context,
        "subtask_finished",
        {
            "ordinal": ordinal,
            "total": total,
            "status": envelope["status"],
            "finding_count": len(envelope.get("claims", [])),
            "evidence_count": len(envelope.get("evidence", [])),
            "input_tokens": int(envelope.get("usage", {}).get("input_tokens", 0)),
            "output_tokens": int(envelope.get("usage", {}).get("output_tokens", 0)),
            "provider_input_tokens": envelope.get("usage", {}).get("provider_input_tokens"),
            "provider_output_tokens": envelope.get("usage", {}).get("provider_output_tokens"),
            "duration_ms": int(envelope.get("duration_ms", 0)),
            "recovered": recovered,
        },
    )


async def _scout_node(
    state: ResearchBranchState, runtime: Runtime[ResearchGraphContext]
) -> dict[str, Any]:
    task = ResearchTask.model_validate(state["subtask"])
    ordinal = int(state.get("ordinal", 1))
    total = int(state.get("total", 1))
    await _emit(
        runtime.context,
        "subtask_started",
        {"ordinal": ordinal, "total": total, "paper_count": len(task.paper_ids)},
    )
    envelope = await _execute_scout(state, runtime)
    await _emit_scout_finished(state, runtime.context, envelope, recovered=False)
    return {"branch_results": {task.subtask_id: envelope}}


async def _merge_node(
    state: ResearchGraphState, runtime: Runtime[ResearchGraphContext]
) -> dict[str, Any]:
    await _ensure_lease(runtime.context.lease_guard)
    await _emit(runtime.context, "merge_started", {})
    started_at = time.perf_counter()
    plan = ResearchPlan.model_validate(state["plan"])
    packets: list[FindingPacket] = []
    evidence_by_subtask: dict[str, tuple[Evidence, ...]] = {}
    results = state.get("branch_results", {})
    for task in plan.tasks:
        envelope = results.get(task.subtask_id)
        if envelope is None:
            packets.append(
                FindingPacket(
                    subtask_id=task.subtask_id,
                    status="failed",
                    error_code="SPECIALIST_RESULT_MISSING",
                )
            )
            continue
        packet = FindingPacket.model_validate(envelope.get("finding", {}))
        packets.append(packet)
        evidence_by_subtask[task.subtask_id] = tuple(
            _evidence_from_dict(item) for item in envelope.get("evidence", [])
        )
    report, evidence = merge_findings(
        plan,
        packets,
        evidence_by_subtask,
        allowed_paper_ids=state.get("paper_ids", []),
    )
    conflict_sets = _build_conflict_sets(
        results,
        allowed_paper_ids=state.get("paper_ids", []),
    )
    report = report.model_copy(
        update={
            "merge_duration_ms": max(1, round((time.perf_counter() - started_at) * 1000)),
            "conflict_count": len(conflict_sets),
        }
    )
    branch_metrics = []
    for ordinal, task in enumerate(plan.tasks, start=1):
        task_result = results.get(task.subtask_id, {})
        usage = task_result.get("usage", {})
        branch_metrics.append(
            {
                "subtask_id": f"s{ordinal}",
                "status": str(task_result.get("status", "failed")),
                "duration_ms": int(task_result.get("duration_ms", 0)),
                "evidence_count": len(task_result.get("evidence", [])),
                "claim_count": len(task_result.get("claims", [])),
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "provider_input_tokens": usage.get("provider_input_tokens"),
                "provider_output_tokens": usage.get("provider_output_tokens"),
                "error_code": task_result.get("finding", {}).get("error_code"),
            }
        )
    await _ensure_lease(runtime.context.lease_guard)
    status: Literal["succeeded", "partial", "failed"] = report.status
    await _emit(
        runtime.context,
        "merge_finished",
        {
            "status": status,
            "succeeded_subtasks": sum(item.status == "succeeded" for item in report.findings),
            "failed_subtasks": len(report.failed_subtasks),
            "timeout_subtasks": sum(item.status == "timeout" for item in report.findings),
            "finding_count": sum(len(value.get("claims", [])) for value in results.values()),
            "dedup_count": report.dedup_count,
            "conflict_count": report.conflict_count,
            "paper_coverage_count": len(report.evidence_paper_ids),
            "duration_ms": report.merge_duration_ms,
        },
    )
    return {
        "merge_report": report.model_dump(mode="json"),
        "conflict_sets": conflict_sets,
        "branch_metrics": branch_metrics,
        "merged_evidence": [_evidence_to_dict(item) for item in evidence],
        "status": status,
        "fallback_reason": "all_specialists_failed" if status == "failed" else None,
    }


def build_research_specialist_graph(checkpointer: Any | None = None) -> Any:
    """编译独立 Research Graph；调用方负责传入版本化 checkpoint namespace。"""

    scout_node: Any = _scout_node
    if checkpointer is not None:
        branch_builder = StateGraph(
            ResearchBranchCheckpointState,
            context_schema=ResearchGraphContext,
        )
        branch_builder.add_node("execute", _branch_checkpoint_node)
        branch_builder.add_edge(START, "execute")
        branch_builder.add_edge("execute", END)
        branch_graph = branch_builder.compile(checkpointer=checkpointer)

        async def resumable_scout(
            state: ResearchBranchState,
            runtime: Runtime[ResearchGraphContext],
        ) -> dict[str, Any]:
            task = ResearchTask.model_validate(state["subtask"])
            ordinal = int(state.get("ordinal", 1))
            total = int(state.get("total", 1))
            parent_thread_id = str(state.get("parent_thread_id", "")).strip()
            if not parent_thread_id:
                raise ValueError("SPECIALIST_PARENT_THREAD_REQUIRED")
            await _emit(
                runtime.context,
                "subtask_started",
                {"ordinal": ordinal, "total": total, "paper_count": len(task.paper_ids)},
            )
            config = {
                "configurable": {
                    "thread_id": specialist_branch_thread_id(parent_thread_id, ordinal),
                    "checkpoint_ns": "",
                }
            }
            checkpoint = await checkpointer.aget_tuple(config)
            checkpoint_values = (
                dict(checkpoint.checkpoint.get("channel_values", {}))
                if checkpoint is not None
                else {}
            )
            recovered = (
                checkpoint_values.get("status") == "terminal"
                and isinstance(checkpoint_values.get("envelope"), dict)
            )
            if recovered:
                branch_state = checkpoint_values
            else:
                branch_state = await branch_graph.ainvoke(
                    dict(state), config, context=runtime.context
                )
            envelope = branch_state.get("envelope")
            if not isinstance(envelope, dict):
                raise ValueError("SPECIALIST_BRANCH_CHECKPOINT_INVALID")
            await _ensure_lease(runtime.context.lease_guard)
            await _emit_scout_finished(
                state,
                runtime.context,
                envelope,
                recovered=recovered,
            )
            return {"branch_results": {task.subtask_id: envelope}}

        scout_node = resumable_scout

    graph = StateGraph(ResearchGraphState, context_schema=ResearchGraphContext)
    graph.add_node("plan", _plan_node)
    graph.add_node("specialist_scout", scout_node, input_schema=ResearchBranchState)
    graph.add_node("merge", _merge_node)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", _fan_out)
    graph.add_edge("specialist_scout", "merge")
    graph.add_edge("merge", END)
    return graph.compile(checkpointer=checkpointer)


def specialist_result_from_state(state: Mapping[str, Any]) -> ResearchSynthesisResult:
    """把图输出转换为现有最终回答链可直接消费的结果。"""

    plan = ResearchPlan.model_validate(state.get("plan", {}))
    report = MergeReport.model_validate(state.get("merge_report", {}))
    evidence = tuple(_evidence_from_dict(item) for item in state.get("merged_evidence", []))
    return ResearchSynthesisResult(
        plan=plan,
        report=report,
        evidence=evidence,
        branch_metrics=tuple(
            dict(item) for item in state.get("branch_metrics", []) if isinstance(item, Mapping)
        ),
        conflict_sets=tuple(
            dict(item) for item in state.get("conflict_sets", []) if isinstance(item, Mapping)
        ),
    )


def research_checkpoint_namespace() -> str:
    return f"{SPECIALIST_ORCHESTRATION_VERSION}/research"


def research_checkpoint_thread_id(parent_thread_id: str) -> str:
    """为 Research Graph 提供独立持久线程；PostgresSaver 顶层图不保留自定义 namespace。"""

    return f"{parent_thread_id}:specialist-research-v3"


def specialist_branch_thread_id(parent_thread_id: str, ordinal: int) -> str:
    """每个分支使用独立持久线程，使 Send 屏障前完成的分支也可恢复。"""

    if ordinal < 1 or ordinal > 3:
        raise ValueError("SPECIALIST_BRANCH_ORDINAL_INVALID")
    return f"{parent_thread_id}:specialist-branch-s{ordinal}"
