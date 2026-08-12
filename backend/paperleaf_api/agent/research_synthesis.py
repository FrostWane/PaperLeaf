"""复杂跨论文任务的确定性 Map-Reduce 研究综合核心。

本模块刻意不创建子 ``AgentRun``，也不让分支自由通信。Coordinator 只生成
强类型计划；Scout 只接收冻结后的论文子集并返回候选证据；合并器再次执行
scope 校验和确定性去重。最终回答生成、引用校验与语义支持门禁仍由现有
Agent Graph 负责。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..rag.citations import Evidence

MAX_RESEARCH_BRANCHES = 3
ORCHESTRATION_VERSION = "compare_map_reduce_v2"

LeaseGuard = Callable[[], bool | Awaitable[bool]]
EventSink = Callable[[str, dict[str, Any]], None | Awaitable[None]]


class ResearchSynthesisError(RuntimeError):
    """研究综合无法安全继续。"""


class ResearchLeaseLostError(ResearchSynthesisError):
    """父 Worker 已失去租约，所有分支必须停止。"""


class ResearchScopeViolationError(ResearchSynthesisError):
    """计划尝试访问冻结作用域之外的论文。"""


class ResearchTask(BaseModel):
    """Coordinator 生成的单个只读检索任务。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subtask_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    role: Literal["evidence_scout"] = "evidence_scout"
    objective: str = Field(min_length=3, max_length=2000)
    paper_ids: tuple[str, ...] = Field(min_length=1, max_length=10)
    dimensions: tuple[str, ...] = Field(min_length=1, max_length=8)
    max_tool_steps: int = Field(default=2, ge=1, le=2)
    token_budget: int = Field(ge=256, le=16_384)

    @model_validator(mode="after")
    def validate_unique_values(self) -> ResearchTask:
        if len(self.paper_ids) != len(set(self.paper_ids)):
            raise ValueError("ResearchTask.paper_ids 不能重复")
        normalized_dimensions = [" ".join(item.split()).casefold() for item in self.dimensions]
        if any(not item for item in normalized_dimensions):
            raise ValueError("ResearchTask.dimensions 不能为空")
        if len(normalized_dimensions) != len(set(normalized_dimensions)):
            raise ValueError("ResearchTask.dimensions 不能重复")
        return self


class ResearchPlan(BaseModel):
    """最多三个分支的冻结研究计划。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    objective: str = Field(min_length=3, max_length=2000)
    tasks: tuple[ResearchTask, ...] = Field(min_length=1, max_length=MAX_RESEARCH_BRANCHES)
    orchestration_version: Literal["compare_map_reduce_v2"] = ORCHESTRATION_VERSION

    @model_validator(mode="after")
    def validate_tasks(self) -> ResearchPlan:
        subtask_ids = [item.subtask_id for item in self.tasks]
        if len(subtask_ids) != len(set(subtask_ids)):
            raise ValueError("ResearchPlan.subtask_id 不能重复")
        assigned_papers = [paper_id for item in self.tasks for paper_id in item.paper_ids]
        if len(assigned_papers) != len(set(assigned_papers)):
            raise ValueError("确定性 Map-Reduce 计划不能把同一论文分配给多个分支")
        return self


class FindingPacket(BaseModel):
    """Scout 的结构化结果。

    ``claim`` 只是分支摘要，始终按不可信模型输出处理；下游只能依据已通过
    scope 复验的 ``chunk_ids`` 重新组织最终回答。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subtask_id: str = Field(min_length=1, max_length=100)
    status: Literal["succeeded", "timeout", "failed"]
    claim: str = Field(default="", max_length=4000)
    chunk_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=24)
    stance: Literal["support", "contradict", "unclear"] = "unclear"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    error_code: str | None = Field(default=None, max_length=100)
    duration_ms: int = Field(default=0, ge=0)
    rejected_evidence_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_status_payload(self) -> FindingPacket:
        if len(self.chunk_ids) != len(set(self.chunk_ids)):
            raise ValueError("FindingPacket.chunk_ids 不能重复")
        if self.status == "succeeded":
            if not self.chunk_ids:
                raise ValueError("成功的 FindingPacket 必须包含证据")
            if self.error_code is not None:
                raise ValueError("成功的 FindingPacket 不能包含 error_code")
        else:
            if self.chunk_ids:
                raise ValueError("失败或超时的 FindingPacket 不能包含证据")
            if not self.error_code:
                raise ValueError("失败或超时的 FindingPacket 必须包含 error_code")
        return self


class MergeReport(BaseModel):
    """确定性合并报告；不包含用户问题或证据正文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["succeeded", "partial", "failed"]
    findings: tuple[FindingPacket, ...] = Field(max_length=MAX_RESEARCH_BRANCHES)
    evidence_chunk_ids: tuple[str, ...] = Field(default_factory=tuple)
    evidence_paper_ids: tuple[str, ...] = Field(default_factory=tuple)
    dedup_count: int = Field(default=0, ge=0)
    conflict_count: int = Field(default=0, ge=0)
    rejected_scope_count: int = Field(default=0, ge=0)
    dropped_limit_count: int = Field(default=0, ge=0)
    merge_duration_ms: int = Field(default=0, ge=0)
    failed_subtasks: tuple[str, ...] = Field(default_factory=tuple)
    coverage_notice: str | None = Field(default=None, max_length=1000)


@dataclass(frozen=True)
class ScoutResult:
    """只读 Scout 的进程内返回值。"""

    evidence: tuple[Evidence, ...]
    claim: str = ""
    stance: Literal["support", "contradict", "unclear"] = "unclear"
    confidence: float = 0.0


class EvidenceScout(Protocol):
    async def __call__(self, task: ResearchTask) -> ScoutResult: ...


@dataclass(frozen=True)
class ResearchSynthesisResult:
    plan: ResearchPlan
    report: MergeReport
    evidence: tuple[Evidence, ...]

    @property
    def fallback_required(self) -> bool:
        return self.report.status == "failed" or not self.evidence


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def build_deterministic_research_plan(
    objective: str,
    paper_ids: Sequence[str],
    dimensions: Sequence[str],
    *,
    max_branches: int = MAX_RESEARCH_BRANCHES,
    total_token_budget: int = 6144,
) -> ResearchPlan:
    """按稳定论文 ID 轮转分配，生成最多三个互不重叠的只读任务。"""

    normalized_objective = _clean_text(objective)
    normalized_papers = sorted({_clean_text(value) for value in paper_ids if _clean_text(value)})
    normalized_dimensions = sorted(
        {_clean_text(value) for value in dimensions if _clean_text(value)}, key=str.casefold
    )
    if not normalized_objective:
        raise ValueError("研究目标不能为空")
    if not normalized_papers:
        raise ValueError("研究计划至少需要一篇论文")
    if len(normalized_papers) > 10:
        raise ValueError("Phase 1 最多比较 10 篇论文")
    if not normalized_dimensions:
        raise ValueError("研究计划至少需要一个比较维度")
    if not 1 <= max_branches <= MAX_RESEARCH_BRANCHES:
        raise ValueError("max_branches 必须在 1 到 3 之间")
    branch_count = min(max_branches, len(normalized_papers))
    if total_token_budget < branch_count * 256:
        raise ValueError("父任务 Token 预算不足以分配给所有分支")
    if total_token_budget > branch_count * 16_384:
        raise ValueError("父任务 Token 预算超过分支允许上限")

    canonical = json.dumps(
        {
            "objective": normalized_objective,
            "paper_ids": normalized_papers,
            "dimensions": normalized_dimensions,
            "branches": branch_count,
            "version": ORCHESTRATION_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    task_id = f"research-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"
    buckets: list[list[str]] = [[] for _ in range(branch_count)]
    for index, paper_id in enumerate(normalized_papers):
        buckets[index % branch_count].append(paper_id)
    base_budget, remainder = divmod(total_token_budget, branch_count)
    tasks = tuple(
        ResearchTask(
            subtask_id=f"{task_id}:s{index + 1}",
            objective=normalized_objective,
            paper_ids=tuple(bucket),
            dimensions=tuple(normalized_dimensions),
            max_tool_steps=2,
            token_budget=base_budget + (1 if index < remainder else 0),
        )
        for index, bucket in enumerate(buckets)
    )
    return ResearchPlan(task_id=task_id, objective=normalized_objective, tasks=tasks)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _ensure_lease(lease_guard: LeaseGuard | None) -> None:
    if lease_guard is not None and not bool(await _maybe_await(lease_guard())):
        raise ResearchLeaseLostError("RESEARCH_PARENT_LEASE_LOST")


async def _emit(event_sink: EventSink | None, event: str, data: dict[str, Any]) -> None:
    if event_sink is not None:
        await _maybe_await(event_sink(event, data))


def _normalize_claim(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", value.casefold()))


def _conflict_count(findings: Sequence[FindingPacket]) -> int:
    grouped: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in findings:
        if item.status != "succeeded" or not item.claim.strip():
            continue
        key = _normalize_claim(item.claim)
        if key:
            grouped[key][item.stance] += 1
    return sum(
        min(counts.get("support", 0), counts.get("contradict", 0)) for counts in grouped.values()
    )


def merge_findings(
    plan: ResearchPlan,
    findings: Sequence[FindingPacket],
    evidence_by_subtask: Mapping[str, Sequence[Evidence]],
    *,
    allowed_paper_ids: Sequence[str],
    max_evidence: int = 18,
    max_evidence_per_paper: int = 4,
) -> tuple[MergeReport, tuple[Evidence, ...]]:
    """复验分支 scope，并按 Chunk、物理页和论文多样性确定性合并。"""

    if max_evidence < 1 or max_evidence_per_paper < 1:
        raise ValueError("证据上限必须为正整数")
    allowed_scope = set(allowed_paper_ids)
    task_by_id = {item.subtask_id: item for item in plan.tasks}
    if any(not set(item.paper_ids) <= allowed_scope for item in plan.tasks):
        raise ResearchScopeViolationError("RESEARCH_PLAN_SCOPE_VIOLATION")
    finding_by_id: dict[str, FindingPacket] = {}
    for item in findings:
        if item.subtask_id not in task_by_id:
            raise ValueError("FindingPacket 不属于当前 ResearchPlan")
        if item.subtask_id in finding_by_id:
            raise ValueError("同一 subtask_id 只能返回一个 FindingPacket")
        finding_by_id[item.subtask_id] = item
    unknown_evidence_keys = set(evidence_by_subtask) - set(task_by_id)
    if unknown_evidence_keys:
        raise ValueError("证据包含未知 subtask_id")

    adjusted: list[FindingPacket] = []
    candidates: list[Evidence] = []
    rejected_scope_count = 0
    for task in sorted(plan.tasks, key=lambda item: item.subtask_id):
        packet = finding_by_id.get(task.subtask_id)
        if packet is None:
            adjusted.append(
                FindingPacket(
                    subtask_id=task.subtask_id,
                    status="failed",
                    error_code="SCOUT_RESULT_MISSING",
                )
            )
            continue
        if packet.status != "succeeded":
            adjusted.append(packet)
            continue
        declared = set(packet.chunk_ids)
        valid: list[Evidence] = []
        rejected = 0
        raw_evidence = evidence_by_subtask.get(task.subtask_id, ())
        raw_chunk_ids = {item.chunk_id for item in raw_evidence}
        for item in raw_evidence:
            in_scope = (
                bool(item.chunk_id)
                and item.chunk_id in declared
                and item.paper_id in allowed_scope
                and item.paper_id in task.paper_ids
                and item.physical_page >= 1
            )
            if in_scope:
                valid.append(item)
            else:
                rejected += 1
        # 已返回但越权/越界的证据在上面的逐条复验中已经计数；这里只补记
        # Scout 声明过、实际却完全没有返回的证据，避免同一候选重复计数。
        rejected += len(declared - raw_chunk_ids)
        rejected_scope_count += rejected
        if not valid:
            adjusted.append(
                FindingPacket(
                    subtask_id=packet.subtask_id,
                    status="failed",
                    claim="",
                    stance="unclear",
                    confidence=0.0,
                    error_code="SCOUT_NO_VALID_EVIDENCE",
                    duration_ms=packet.duration_ms,
                    rejected_evidence_count=rejected,
                )
            )
            continue
        valid_ids = tuple(sorted({item.chunk_id for item in valid}))
        adjusted.append(
            packet.model_copy(
                update={
                    "chunk_ids": valid_ids,
                    "rejected_evidence_count": rejected,
                }
            )
        )
        candidates.extend(valid)

    # 先固定顺序，再按 chunk 和 (paper, physical_page) 双重去重。
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.paper_id,
            item.physical_page,
            -float(item.retrieval_score),
            item.chunk_id,
        ),
    )
    unique: list[Evidence] = []
    seen_chunks: set[str] = set()
    seen_pages: set[tuple[str, int]] = set()
    dedup_count = 0
    for item in ordered:
        page_key = (item.paper_id, item.physical_page)
        if item.chunk_id in seen_chunks or page_key in seen_pages:
            dedup_count += 1
            continue
        seen_chunks.add(item.chunk_id)
        seen_pages.add(page_key)
        unique.append(item)

    # 按论文轮转，防止单篇高分证据占满最终上下文。
    by_paper: dict[str, list[Evidence]] = defaultdict(list)
    for item in unique:
        by_paper[item.paper_id].append(item)
    for items in by_paper.values():
        items.sort(
            key=lambda item: (-float(item.retrieval_score), item.physical_page, item.chunk_id)
        )
    selected: list[Evidence] = []
    paper_ids = sorted(by_paper)
    round_index = 0
    while len(selected) < max_evidence:
        added = False
        for paper_id in paper_ids:
            items = by_paper[paper_id]
            if round_index < min(len(items), max_evidence_per_paper):
                selected.append(items[round_index])
                added = True
                if len(selected) >= max_evidence:
                    break
        if not added:
            break
        round_index += 1
    dropped_limit_count = max(0, len(unique) - len(selected))

    adjusted_tuple = tuple(sorted(adjusted, key=lambda item: item.subtask_id))
    failed_subtasks = tuple(
        item.subtask_id for item in adjusted_tuple if item.status != "succeeded"
    )
    if not selected:
        status: Literal["succeeded", "partial", "failed"] = "failed"
        coverage_notice = "所有只读证据分支均失败或未返回作用域内的合法证据，需回退单 Agent。"
    elif failed_subtasks:
        status = "partial"
        coverage_notice = (
            f"{len(failed_subtasks)}/{len(plan.tasks)} 个证据分支未完成；当前回答只能覆盖其余分支。"
        )
    else:
        status = "succeeded"
        coverage_notice = (
            f"已过滤 {rejected_scope_count} 条未通过作用域复验的候选证据。"
            if rejected_scope_count
            else None
        )
    report = MergeReport(
        status=status,
        findings=adjusted_tuple,
        evidence_chunk_ids=tuple(item.chunk_id for item in selected),
        evidence_paper_ids=tuple(dict.fromkeys(item.paper_id for item in selected)),
        dedup_count=dedup_count,
        conflict_count=_conflict_count(adjusted_tuple),
        rejected_scope_count=rejected_scope_count,
        dropped_limit_count=dropped_limit_count,
        failed_subtasks=failed_subtasks,
        coverage_notice=coverage_notice,
    )
    return report, tuple(selected)


async def execute_research_plan(
    plan: ResearchPlan,
    scout: EvidenceScout,
    *,
    allowed_paper_ids: Sequence[str],
    branch_timeout_seconds: float,
    lease_guard: LeaseGuard | None = None,
    event_sink: EventSink | None = None,
    max_concurrency: int = MAX_RESEARCH_BRANCHES,
    max_evidence: int = 18,
    max_evidence_per_paper: int = 4,
) -> ResearchSynthesisResult:
    """并行执行只读 Scout；正常分支失败被结构化，租约丢失则取消父任务。"""

    if branch_timeout_seconds <= 0:
        raise ValueError("branch_timeout_seconds 必须大于 0")
    if not 1 <= max_concurrency <= MAX_RESEARCH_BRANCHES:
        raise ValueError("max_concurrency 必须在 1 到 3 之间")
    allowed_scope = set(allowed_paper_ids)
    if any(not set(task.paper_ids) <= allowed_scope for task in plan.tasks):
        raise ResearchScopeViolationError("RESEARCH_PLAN_SCOPE_VIOLATION")
    await _ensure_lease(lease_guard)
    semaphore = asyncio.Semaphore(min(max_concurrency, len(plan.tasks)))

    async def run_branch(task: ResearchTask) -> tuple[FindingPacket, tuple[Evidence, ...]]:
        async with semaphore:
            await _ensure_lease(lease_guard)
            await _emit(
                event_sink,
                "subtask_started",
                {
                    "task_id": plan.task_id,
                    "subtask_id": task.subtask_id,
                    "orchestration_version": plan.orchestration_version,
                    "paper_count": len(task.paper_ids),
                },
            )
            started_at = time.perf_counter()
            try:
                raw = await asyncio.wait_for(scout(task), timeout=branch_timeout_seconds)
                if not isinstance(raw, ScoutResult):
                    raise TypeError("Scout 必须返回 ScoutResult")
                await _ensure_lease(lease_guard)
                duration_ms = round((time.perf_counter() - started_at) * 1000)
                unique_ids = tuple(
                    sorted({item.chunk_id for item in raw.evidence if item.chunk_id})
                )
                if not unique_ids:
                    packet = FindingPacket(
                        subtask_id=task.subtask_id,
                        status="failed",
                        error_code="SCOUT_NO_EVIDENCE",
                        duration_ms=duration_ms,
                    )
                    evidence: tuple[Evidence, ...] = ()
                else:
                    packet = FindingPacket(
                        subtask_id=task.subtask_id,
                        status="succeeded",
                        claim=raw.claim,
                        chunk_ids=unique_ids,
                        stance=raw.stance,
                        confidence=min(1.0, max(0.0, float(raw.confidence))),
                        duration_ms=duration_ms,
                    )
                    evidence = tuple(raw.evidence)
            except ResearchLeaseLostError:
                raise
            except TimeoutError:
                packet = FindingPacket(
                    subtask_id=task.subtask_id,
                    status="timeout",
                    error_code="SCOUT_TIMEOUT",
                    duration_ms=round((time.perf_counter() - started_at) * 1000),
                )
                evidence = ()
            except asyncio.CancelledError:
                raise
            except Exception:
                packet = FindingPacket(
                    subtask_id=task.subtask_id,
                    status="failed",
                    error_code="SCOUT_FAILED",
                    duration_ms=round((time.perf_counter() - started_at) * 1000),
                )
                evidence = ()
            await _emit(
                event_sink,
                "subtask_finished",
                {
                    "task_id": plan.task_id,
                    "subtask_id": task.subtask_id,
                    "orchestration_version": plan.orchestration_version,
                    "status": packet.status,
                    "evidence_count": len(evidence),
                    "duration_ms": packet.duration_ms,
                    **({"error_code": packet.error_code} if packet.error_code else {}),
                },
            )
            return packet, evidence

    # gather 遇到父取消或租约丢失会取消尚未完成的只读分支；一般超时和异常已在
    # run_branch 内转成 FindingPacket，因此不会覆盖其他成功结果。
    outcomes = await asyncio.gather(*(run_branch(task) for task in plan.tasks))
    await _ensure_lease(lease_guard)
    findings = [item[0] for item in outcomes]
    evidence_by_subtask = {item[0].subtask_id: item[1] for item in outcomes}
    await _emit(
        event_sink,
        "merge_started",
        {
            "orchestration_version": plan.orchestration_version,
            "subtask_count": len(plan.tasks),
        },
    )
    merge_started_at = time.perf_counter()
    report, evidence = merge_findings(
        plan,
        findings,
        evidence_by_subtask,
        allowed_paper_ids=allowed_paper_ids,
        max_evidence=max_evidence,
        max_evidence_per_paper=max_evidence_per_paper,
    )
    merge_duration_ms = round((time.perf_counter() - merge_started_at) * 1000)
    report = report.model_copy(update={"merge_duration_ms": merge_duration_ms})
    await _ensure_lease(lease_guard)
    await _emit(
        event_sink,
        "merge_finished",
        {
            "task_id": plan.task_id,
            "orchestration_version": plan.orchestration_version,
            "status": report.status,
            "evidence_count": len(evidence),
            "succeeded_subtask_count": sum(item.status == "succeeded" for item in report.findings),
            "failed_subtask_count": len(report.failed_subtasks),
            "timeout_subtask_count": sum(item.status == "timeout" for item in report.findings),
            "dedup_count": report.dedup_count,
            "conflict_count": report.conflict_count,
            "duration_ms": merge_duration_ms,
        },
    )
    return ResearchSynthesisResult(plan=plan, report=report, evidence=evidence)
