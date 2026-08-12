"""PaperLeaf 多 Agent A/B 的最小真实采集适配器。

这个模块只做三件事：

1. 在提交任何模型请求前，核对冻结数据集、本地论文映射和 Chunk 快照；
2. 使用同一输入与快照依次提交 v1/v2 生产 Run，并从 PostgreSQL 读取事实记录；
3. 将能够证明的指标标为 ``measured``，无法从生产轨迹证明的指标标为
   ``not_measured``。

它不会生成模拟 Run，也不会把 draft 数据集包装成质量提升结论。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import secrets
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select

from .agent.context_budget import estimate_tokens
from .config import Settings, settings
from .db import get_session_factory
from .evaluation_multi_agent import (
    MultiAgentCase,
    MultiAgentManifest,
    expected_ab_order,
    query_hash,
    read_jsonl,
    scope_hash,
    validate_dataset,
    validate_source_hashes,
)
from .models import AgentRun, AgentRunEvent, AgentToolCall, Paper, PaperChunk, PaperStatus
from .repository import SQLAlchemyRepository

TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
RAW_V1 = "single_agent_v1"
RAW_V2 = "compare_map_reduce_v2"
DEFAULT_OUTPUT_ROOT = Path("outputs/private")
_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def measured(value: Any, *, basis: str) -> dict[str, Any]:
    return {"status": "measured", "value": value, "basis": basis}


def not_measured(reason: str) -> dict[str, Any]:
    return {"status": "not_measured", "value": None, "reason": reason}


def normalize_arxiv_id(value: str | None) -> str:
    """把数据集和本地记录统一到不含版本号的 arXiv ID。"""

    normalized = str(value or "").strip().casefold()
    normalized = normalized.removeprefix("arxiv:")
    normalized = normalized.removeprefix("https://arxiv.org/abs/")
    normalized = normalized.removeprefix("http://arxiv.org/abs/")
    normalized = normalized.removeprefix("https://arxiv.org/pdf/")
    normalized = normalized.removeprefix("http://arxiv.org/pdf/")
    normalized = normalized.removesuffix(".pdf")
    return _ARXIV_VERSION_RE.sub("", normalized)


def normalize_anchor_text(value: str) -> str:
    """对 anchor 与 Chunk 使用同一套保守规范化，不做模糊语义猜测。"""

    return _SPACE_RE.sub(" ", value.casefold().replace("\u00ad", "")).strip()


def normalize_production_version(raw: str) -> Literal["v1", "v2"] | None:
    if raw in {RAW_V1, "v1"}:
        return "v1"
    if raw in {RAW_V2, "v2"}:
        return "v2"
    return None


def normalize_execution_path(raw_version: str, trace: dict[str, Any]) -> str:
    """区分请求的编排版本与最终真正使用的执行路径。"""

    normalized = normalize_production_version(raw_version)
    if normalized == "v1":
        return "v1"
    if normalized != "v2":
        return "not_measured"
    if bool(trace.get("fallback_to_v1")):
        return "v1"
    if (
        trace.get("compare_mode") == "parallel_map_reduce"
        and bool(trace.get("tool_output_used"))
        and int(trace.get("planned_subtasks", 0) or 0) > 0
    ):
        return "v2"
    # v2 配置可能因 scope 少于三篇而没有进入并行路径，这是合法 v1 对照路径。
    return "v1"


def normalize_branch_counts(trace: dict[str, Any]) -> dict[str, int]:
    """修正生产 trace 中 timeout 同时计入 failed 的历史语义。"""

    planned = max(0, int(trace.get("planned_subtasks", 0) or 0))
    succeeded = max(0, int(trace.get("succeeded_subtasks", 0) or 0))
    raw_failed = max(0, int(trace.get("failed_subtasks", 0) or 0))
    timed_out = max(0, int(trace.get("timeout_subtasks", 0) or 0))
    failed = max(0, raw_failed - timed_out)
    terminal = succeeded + failed + timed_out
    if terminal > planned:
        # 不悄悄提高 planned；保留尽可能保守且满足终态计数不变量的失败数。
        failed = max(0, planned - succeeded - timed_out)
    return {
        "planned": planned,
        "succeeded": succeeded,
        "failed": failed,
        "timed_out": min(timed_out, max(0, planned - succeeded - failed)),
        "raw_failed_including_timeout": raw_failed,
    }


def quality_decision(manifest: MultiAgentManifest) -> str:
    """live capture 没有人工盲评；draft 永远不能输出 Go。"""

    if manifest.annotation_status == "draft" or not manifest.quality_claims_allowed:
        return "quality_pending"
    return "not_evaluated"


def resolve_private_output_path(value: Path, *, now: datetime | None = None) -> Path:
    """只允许写入显式 ``outputs/private`` 路径，避免真实回答误入仓库。"""

    candidate = value.expanduser()
    parts = [part.casefold() for part in candidate.parts]
    is_private = any(
        parts[index : index + 2] == ["outputs", "private"]
        for index in range(max(0, len(parts) - 1))
    )
    if not is_private:
        raise ValueError("--output 必须位于 outputs/private 目录")
    if candidate.suffix.casefold() == ".json":
        return candidate
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return candidate / f"multi-agent-live-{stamp}.json"


def _source_case_index(source_cases: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id", "")): item for item in source_cases if item.get("id")}


def audit_citation_records(
    *,
    citations: Sequence[dict[str, Any]],
    chunk_records: dict[str, dict[str, Any]],
    logical_to_local: dict[str, str],
    owner_id: str,
    source_case_ids: Sequence[str],
    source_cases: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """用数据库 Chunk 事实核对 scope、物理页和冻结 anchor。"""

    local_to_logical = {local: logical for logical, local in logical_to_local.items()}
    allowed_local_ids = set(local_to_logical)
    valid_chunks: list[dict[str, Any]] = []
    correct_pages = 0
    illegal = 0
    scope_violations = 0
    cross_user_leaks = 0
    cited_papers: list[str] = []

    for citation in citations:
        chunk_id = str(citation.get("chunk_id", ""))
        record = chunk_records.get(chunk_id)
        if record is None:
            illegal += 1
            continue
        actual_paper_id = str(record["paper_id"])
        actual_page = int(record["physical_page"])
        actual_owner = str(record["owner_id"])
        if actual_owner != owner_id:
            cross_user_leaks += 1
            illegal += 1
            continue
        if actual_paper_id not in allowed_local_ids:
            scope_violations += 1
            illegal += 1
            continue
        try:
            cited_page = int(citation.get("physical_page", 0) or 0)
        except (TypeError, ValueError):
            cited_page = 0
        cited_paper_id = str(citation.get("paper_id", ""))
        if cited_page != actual_page or cited_paper_id != actual_paper_id:
            illegal += 1
            continue
        correct_pages += 1
        logical_id = local_to_logical[actual_paper_id]
        if logical_id not in cited_papers:
            cited_papers.append(logical_id)
        valid_chunks.append({**record, "logical_paper_id": logical_id})

    source_index = _source_case_index(source_cases)
    covered_source_ids: list[str] = []
    for source_id in source_case_ids:
        source = source_index.get(source_id, {})
        expected = list(source.get("expected_evidence", []) or [])
        if not expected:
            continue
        all_anchors_present = True
        for item in expected:
            logical_id = str(item.get("paper_id", ""))
            page = int(item.get("physical_page", 0) or 0)
            anchor = normalize_anchor_text(str(item.get("anchor", "")))
            matched = any(
                chunk["logical_paper_id"] == logical_id
                and int(chunk["physical_page"]) == page
                and bool(anchor)
                and anchor in normalize_anchor_text(str(chunk["text"]))
                for chunk in valid_chunks
            )
            if not matched:
                all_anchors_present = False
                break
        if all_anchors_present:
            covered_source_ids.append(source_id)

    return {
        "total_citations": len(citations),
        "correct_page_citations": correct_pages,
        "illegal_citation_count": illegal,
        "scope_violation_count": scope_violations,
        "cross_user_leak_count": cross_user_leaks,
        "cited_paper_ids": cited_papers,
        "covered_source_case_ids": covered_source_ids,
    }


def build_not_executed_variant(label: Literal["v1", "v2"], reason: str) -> dict[str, Any]:
    """缺少运行条件时明确没有 Run ID，禁止创建伪 UUID。"""

    return {
        "variant": label,
        "execution_status": "not_executed",
        "run_id": None,
        "reason": reason,
        "measurements": {
            "covered_dimensions": not_measured("生产轨迹不证明最终回答覆盖了哪些语义维度"),
            "presented_conflicts": not_measured("生产 Scout 未生成可审计的 claim/stance"),
            "partial_failure_notice": not_measured("没有真实 Run，无法核对用户可见提示"),
        },
    }


def build_case_readiness_matrix(
    cases: Sequence[MultiAgentCase],
    logical_to_local: dict[str, str],
    *,
    missing_paper_ids: Sequence[str] = (),
    invalid_paper_ids: Sequence[str] = (),
    skills_enabled: bool,
    multi_agent_enabled: bool,
    answer_model_configured: bool,
) -> dict[str, Any]:
    """逐题说明 live A/B 是否具备真实执行条件。

    该矩阵只是基础设施门禁，不代表答案质量；越权题需要独立 HTTP fixture，
    因此即使论文齐全也不能由普通生产 Run 适配器执行。
    """

    missing = set(missing_paper_ids)
    invalid = set(invalid_paper_ids)
    rows: list[dict[str, Any]] = []
    for case in cases:
        reasons: list[str] = []
        missing_for_case = sorted(
            paper_id
            for paper_id in case.scope_paper_ids
            if paper_id in missing
            or (paper_id not in logical_to_local and paper_id not in invalid)
        )
        invalid_for_case = sorted(
            paper_id for paper_id in case.scope_paper_ids if paper_id in invalid
        )
        if case.expected_path == "pregraph_reject":
            reasons.append("requires_http_pregraph_fixture")
        else:
            if missing_for_case:
                reasons.append("required_papers_missing")
            if invalid_for_case:
                reasons.append("required_papers_not_ready_or_ambiguous")
            if case.expected_path == "v2" and not skills_enabled:
                reasons.append("skills_feature_disabled")
            if case.expected_path == "v2" and not multi_agent_enabled:
                reasons.append("multi_agent_feature_disabled")
            if not answer_model_configured:
                reasons.append("answer_model_not_configured")
        rows.append(
            {
                "case_id": case.id,
                "split": case.split,
                "expected_path": case.expected_path,
                "ready": not reasons,
                "missing_paper_ids": missing_for_case,
                "invalid_paper_ids": invalid_for_case,
                "reasons": reasons,
            }
        )
    return {
        "case_count": len(rows),
        "ready_case_count": sum(bool(row["ready"]) for row in rows),
        "ready_v1_case_count": sum(
            bool(row["ready"]) and row["expected_path"] == "v1" for row in rows
        ),
        "ready_v2_case_count": sum(
            bool(row["ready"]) and row["expected_path"] == "v2" for row in rows
        ),
        "fixture_required_case_count": sum(
            row["expected_path"] == "pregraph_reject" for row in rows
        ),
        "cases": rows,
    }


def _model_config_hash(config: Settings) -> str:
    return _sha256_json(
        {
            "chat_model": config.chat_model,
            "fallback_chat_model": config.fallback_chat_model,
            "primary_base_url_sha256": hashlib.sha256(
                config.openai_base_url.encode("utf-8")
            ).hexdigest(),
            "fallback_base_url_sha256": hashlib.sha256(
                config.fallback_openai_base_url.encode("utf-8")
            ).hexdigest(),
            "primary_configured": bool(config.openai_api_key),
            "fallback_chat_configured": bool(
                config.fallback_openai_api_key and config.fallback_chat_model
            ),
            "model_timeout_seconds": config.model_timeout_seconds,
            "answer_timeout_seconds": config.agent_answer_timeout_seconds,
            "context_tokens": config.model_context_tokens,
            "multi_agent_max_branches": config.multi_agent_max_branches,
            "multi_agent_branch_timeout_seconds": config.multi_agent_branch_timeout_seconds,
            "multi_agent_total_timeout_seconds": config.multi_agent_total_timeout_seconds,
            "multi_agent_token_budget": config.multi_agent_token_budget,
        }
    )


def _harness_flags(config: Settings) -> dict[str, Any]:
    return {
        "context_engine_enabled": config.context_engine_enabled,
        "memory_enabled": config.memory_enabled,
        "skills_enabled": config.skills_enabled,
        "function_tools_enabled": config.function_tools_enabled,
        "mcp_enabled": config.mcp_enabled,
        "multi_agent_enabled": config.multi_agent_enabled,
        "multi_agent_max_branches": config.multi_agent_max_branches,
        "multi_agent_branch_timeout_seconds": config.multi_agent_branch_timeout_seconds,
        "multi_agent_total_timeout_seconds": config.multi_agent_total_timeout_seconds,
        "multi_agent_token_budget": config.multi_agent_token_budget,
    }


async def _paper_mapping(
    owner_id: str, logical_ids: Iterable[str]
) -> tuple[dict[str, str], list[str], list[str]]:
    required = sorted(set(logical_ids))
    required_by_base: dict[str, list[str]] = {}
    for logical_id in required:
        required_by_base.setdefault(normalize_arxiv_id(logical_id), []).append(logical_id)

    async with get_session_factory()() as session:
        papers = list(
            await session.scalars(
                select(Paper).where(Paper.owner_id == owner_id, Paper.arxiv_id.is_not(None))
            )
        )
        chunk_counts = dict(
            (
                await session.execute(
                    select(PaperChunk.paper_id, PaperChunk.id).where(
                        PaperChunk.paper_id.in_([paper.id for paper in papers])
                    )
                )
            ).all()
        )

    candidates: dict[str, list[Paper]] = {}
    for paper in papers:
        candidates.setdefault(normalize_arxiv_id(paper.arxiv_id), []).append(paper)

    mapping: dict[str, str] = {}
    missing: list[str] = []
    invalid: list[str] = []
    for base_id, aliases in required_by_base.items():
        matches = candidates.get(base_id, [])
        if len(matches) != 1:
            (missing if not matches else invalid).extend(aliases)
            continue
        paper = matches[0]
        status_value = (
            paper.status.value if isinstance(paper.status, PaperStatus) else str(paper.status)
        )
        if (
            status_value not in {PaperStatus.ready.value, PaperStatus.partial.value}
            or paper.archived_at is not None
            or paper.id not in chunk_counts
        ):
            invalid.extend(aliases)
            continue
        for logical_id in aliases:
            mapping[logical_id] = paper.id
    return mapping, sorted(missing), sorted(invalid)


async def _snapshot_hash(owner_id: str, logical_to_local: dict[str, str]) -> str:
    local_ids = sorted(set(logical_to_local.values()))
    async with get_session_factory()() as session:
        papers = list(
            await session.scalars(
                select(Paper).where(Paper.owner_id == owner_id, Paper.id.in_(local_ids))
            )
        )
        chunks = (
            await session.execute(
                select(
                    PaperChunk.id,
                    PaperChunk.paper_id,
                    PaperChunk.physical_page,
                    PaperChunk.chunk_index,
                    PaperChunk.text,
                )
                .where(PaperChunk.paper_id.in_(local_ids))
                .order_by(PaperChunk.paper_id, PaperChunk.physical_page, PaperChunk.chunk_index)
            )
        ).all()
    inverse = {local: logical for logical, local in logical_to_local.items()}
    payload = {
        "papers": sorted(
            [
                {
                    "logical_id": inverse[paper.id],
                    "local_id": paper.id,
                    "status": paper.status.value
                    if isinstance(paper.status, PaperStatus)
                    else str(paper.status),
                    "updated_at": paper.updated_at.isoformat(),
                    "embedding_fingerprint": paper.embedding_fingerprint,
                }
                for paper in papers
            ],
            key=lambda item: item["logical_id"],
        ),
        "chunks": [
            {
                "logical_id": inverse[row.paper_id],
                "id": row.id,
                "physical_page": row.physical_page,
                "chunk_index": row.chunk_index,
                "text_sha256": hashlib.sha256(row.text.encode("utf-8")).hexdigest(),
            }
            for row in chunks
        ],
    }
    return _sha256_json(payload)


async def _load_chunk_records(chunk_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not chunk_ids:
        return {}
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    PaperChunk.id,
                    PaperChunk.paper_id,
                    PaperChunk.physical_page,
                    PaperChunk.text,
                    Paper.owner_id,
                )
                .join(Paper, Paper.id == PaperChunk.paper_id)
                .where(PaperChunk.id.in_(list(dict.fromkeys(chunk_ids))))
            )
        ).all()
    return {
        row.id: {
            "chunk_id": row.id,
            "paper_id": row.paper_id,
            "physical_page": row.physical_page,
            "text": row.text,
            "owner_id": row.owner_id,
        }
        for row in rows
    }


async def _load_run_facts(
    run_id: str,
) -> tuple[AgentRun | None, list[AgentRunEvent], list[AgentToolCall]]:
    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
        events = list(
            await session.scalars(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run_id)
                .order_by(AgentRunEvent.sequence)
            )
        )
        calls = list(
            await session.scalars(
                select(AgentToolCall)
                .where(AgentToolCall.run_id == run_id)
                .order_by(AgentToolCall.created_at)
            )
        )
        if run is not None:
            session.expunge(run)
        for item in events:
            session.expunge(item)
        for item in calls:
            session.expunge(item)
    return run, events, calls


async def _wait_for_run(
    repository: SQLAlchemyRepository,
    run_id: str,
    owner_id: str,
    *,
    timeout_seconds: float,
) -> AgentRun | None:
    deadline = time.monotonic() + timeout_seconds
    last: AgentRun | None = None
    while time.monotonic() < deadline:
        last = await repository.get_owned_agent_run(run_id, owner_id)
        if last is None or last.status in TERMINAL_RUN_STATUSES:
            return last
        await asyncio.sleep(1.0)
    return last


def _first_verified_delta_ms(run: AgentRun, events: Sequence[AgentRunEvent]) -> int | None:
    deltas = [item for item in events if item.event == "message_delta"]
    if not deltas:
        return None
    first = min(deltas, key=lambda item: item.created_at)
    return max(0, round((first.created_at - run.created_at).total_seconds() * 1000))


def _unapproved_write_measurement(calls: Sequence[AgentToolCall]) -> dict[str, Any]:
    approved_writes = [
        item for item in calls if item.requires_approval and item.status == "succeeded"
    ]
    if approved_writes:
        return not_measured("成功的审批型工具需要额外审批事件才能证明是否已获用户确认")
    return measured(0, basis="本 Run 不存在成功的 requires_approval 工具调用")


async def _capture_variant(
    *,
    label: Literal["v1", "v2"],
    run_id: str,
    owner_id: str,
    case: MultiAgentCase,
    logical_to_local: dict[str, str],
    source_cases: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    run, events, calls = await _load_run_facts(run_id)
    if run is None:
        return build_not_executed_variant(label, "提交返回 Run ID，但数据库未找到对应记录")
    summary = dict(run.result_summary or {})
    trace = dict(run.harness_trace or {})
    citations = list(summary.get("citations", []) or [])
    chunk_records = await _load_chunk_records(
        [str(item.get("chunk_id", "")) for item in citations if item.get("chunk_id")]
    )
    citation_audit = audit_citation_records(
        citations=citations,
        chunk_records=chunk_records,
        logical_to_local=logical_to_local,
        owner_id=owner_id,
        source_case_ids=case.source_case_ids,
        source_cases=source_cases,
    )
    quality = dict(summary.get("evidence_quality", {}) or {})
    model_attempts = list(summary.get("model_attempts", []) or [])
    context_budget = dict(trace.get("context_budget", {}) or {})
    final_input = context_budget.get("final_input_tokens")
    hard_limit = context_budget.get("hard_limit")
    if final_input is None or hard_limit is None:
        budget_measurement = not_measured("生产 Run 未持久化最终输入与 hard limit")
    else:
        budget_measurement = measured(
            int(final_input) > int(hard_limit),
            basis="harness_trace.context_budget",
        )

    supported_claims = quality.get("supported_claim_count")
    total_claims = quality.get("claim_count")
    claim_measurement = (
        measured(
            {"supported": int(supported_claims), "total": int(total_claims)},
            basis="result_summary.evidence_quality",
        )
        if supported_claims is not None and total_claims is not None
        else not_measured("回答未产生可审计的主张支持计数")
    )
    rag_outcome = str(dict(summary.get("rag_trace", {}) or {}).get("outcome", ""))
    wrong_unanswerable = (
        measured(
            rag_outcome not in {"abstained", "scope_violation"},
            basis="result_summary.rag_trace.outcome",
        )
        if not case.answerable and rag_outcome
        else not_measured("该题可回答，或 Run 未持久化 RAG outcome")
    )

    return {
        "variant": label,
        "execution_status": "executed",
        "run_id": run.id,
        "run_status": run.status,
        "error_code": run.error_code,
        "raw_orchestration_version": run.orchestration_version,
        "orchestration_version": normalize_production_version(run.orchestration_version),
        "executed_path": normalize_execution_path(run.orchestration_version, trace),
        "duration_ms": run.duration_ms,
        "first_verified_delta_ms": _first_verified_delta_ms(run, events),
        "branches": normalize_branch_counts(trace),
        "fallback_to_v1": bool(trace.get("fallback_to_v1", False)),
        "fallback_reason": trace.get("fallback_reason") or trace.get("function_fallback_reason"),
        "trace_partial_failure": bool(trace.get("partial_failure", False)),
        "estimated_input_tokens": measured(
            int(final_input), basis="harness_trace.context_budget.final_input_tokens"
        )
        if final_input is not None
        else not_measured("最终输入 Token 估算未持久化"),
        "estimated_output_tokens": measured(
            estimate_tokens(str(summary.get("answer", ""))),
            basis="PaperLeaf 确定性 estimate_tokens，不是 Provider 账单",
        ),
        "model_call_count": measured(len(model_attempts), basis="result_summary.model_attempts"),
        "tool_call_count": measured(len(calls), basis="agent_tool_calls"),
        "measurements": {
            "citation_audit": measured(citation_audit, basis="数据库 PaperChunk/owner/scope/page"),
            "claim_support": claim_measurement,
            "context_budget_exceeded": budget_measurement,
            "wrong_answer_on_unanswerable": wrong_unanswerable,
            "unapproved_write_count": _unapproved_write_measurement(calls),
            "prompt_injection_success_count": not_measured(
                "结构轨迹无法证明回答是否泄漏系统提示或服从了恶意文本"
            ),
            "covered_dimensions": not_measured("计划维度不等于最终回答的语义覆盖"),
            "presented_conflicts": not_measured(
                "当前生产 Scout 未生成可审计的 claim/stance，conflict_count 不能代表召回"
            ),
            "partial_failure_notice": not_measured(
                "trace 只证明发生 partial failure，未结构化记录用户是否看见提示"
            ),
        },
    }


async def _submit_variant(
    *,
    repository: SQLAlchemyRepository,
    owner_id: str,
    case: MultiAgentCase,
    label: Literal["v1", "v2"],
    logical_to_local: dict[str, str],
    config: Settings,
    timeout_seconds: float,
) -> tuple[str | None, str | None]:
    raw_version = RAW_V1 if label == "v1" else RAW_V2
    local_ids = [logical_to_local[item] for item in case.scope_paper_ids]
    chat_session = await repository.create_chat_session(
        owner_id,
        f"[实测] 多 Agent A/B · {case.id} · {label}",
        "library",
        None,
        None,
    )
    scope_snapshot = {
        "type": "library",
        "paper_id": None,
        "collection_id": None,
        "paper_ids": local_ids,
        "web_enabled": False,
        "client_context": {},
        "harness": _harness_flags(config),
        "orchestration_version": raw_version,
    }
    request_hash = _sha256_json(
        {"session_id": chat_session.id, "content": case.query, "scope_snapshot": scope_snapshot}
    )
    submission = await repository.submit_chat_message(
        chat_session.id,
        owner_id,
        case.query,
        f"multi-agent-live-{case.id}-{label}-{secrets.token_hex(6)}",
        request_hash,
        scope_snapshot,
    )
    if submission is None:
        return None, "repository_rejected_submission"
    run_id = submission.run.id
    finished = await _wait_for_run(
        repository,
        run_id,
        owner_id,
        timeout_seconds=timeout_seconds,
    )
    if finished is None:
        return run_id, "run_missing_after_submission"
    if finished.status not in TERMINAL_RUN_STATUSES:
        return run_id, "run_timeout"
    return run_id, None


def _case_input_hash(case: MultiAgentCase, snapshot_hash: str, model_hash: str) -> str:
    return _sha256_json(
        {
            "query_hash": query_hash(case),
            "scope_hash": scope_hash(case),
            "collection_snapshot_hash": snapshot_hash,
            "model_config_hash": model_hash,
        }
    )


async def _preflight(
    *,
    manifest: MultiAgentManifest,
    cases: Sequence[MultiAgentCase],
    config: Settings,
) -> tuple[dict[str, Any], str | None, dict[str, str]]:
    repository = SQLAlchemyRepository(config.session_secret)
    user = await repository.find_user_by_email(config.bootstrap_admin_email)
    if user is None:
        return (
            {"status": "failed", "reasons": ["bootstrap_admin_not_found"]},
            None,
            {},
        )
    executable = [case for case in cases if case.expected_path != "pregraph_reject"]
    required_ids = [paper_id for case in executable for paper_id in case.scope_paper_ids]
    mapping, missing, invalid = await _paper_mapping(user.id, required_ids)
    answer_model_configured = bool(config.openai_api_key) or bool(
        config.fallback_openai_api_key and config.fallback_chat_model
    )
    reasons: list[str] = []
    if missing:
        reasons.append("required_papers_missing")
    if invalid:
        reasons.append("required_papers_not_ready_or_ambiguous")
    if any(case.expected_path == "v2" for case in executable):
        if not config.multi_agent_enabled:
            reasons.append("multi_agent_feature_disabled")
        if not config.skills_enabled:
            reasons.append("skills_feature_disabled")
    if not answer_model_configured:
        reasons.append("answer_model_not_configured")
    snapshot_hash = None
    if not missing and not invalid and mapping:
        snapshot_hash = await _snapshot_hash(user.id, mapping)
    report = {
        "status": "failed" if reasons else "passed",
        "dataset_id": manifest.dataset_id,
        "owner_id": user.id,
        "required_paper_count": len(set(required_ids)),
        "mapped_paper_count": len(mapping),
        "paper_mapping": dict(sorted(mapping.items())),
        "corpus_snapshot_hash": snapshot_hash,
        "missing_paper_ids": missing,
        "invalid_paper_ids": invalid,
        "case_readiness": build_case_readiness_matrix(
            cases,
            mapping,
            missing_paper_ids=missing,
            invalid_paper_ids=invalid,
            skills_enabled=config.skills_enabled,
            multi_agent_enabled=config.multi_agent_enabled,
            answer_model_configured=answer_model_configured,
        ),
        "feature_flags": {
            "skills_enabled": config.skills_enabled,
            "multi_agent_enabled": config.multi_agent_enabled,
        },
        "reasons": reasons,
    }
    return report, user.id, mapping


async def run_live_capture(
    *,
    manifest_path: Path,
    cases_path: Path,
    source_manifest_path: Path,
    source_cases_path: Path,
    output_path: Path,
    split: Literal["dev", "test"] = "test",
    case_ids: Sequence[str] = (),
    limit: int | None = None,
    preflight_only: bool = False,
    timeout_seconds: float = 300,
    config: Settings = settings,
) -> dict[str, Any]:
    """执行真实采集；所有失败都写入报告，而不是补造 Run。"""

    manifest = MultiAgentManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    validate_source_hashes(
        manifest,
        source_manifest_path=source_manifest_path,
        source_cases_path=source_cases_path,
    )
    cases = read_jsonl(cases_path, MultiAgentCase)
    source_cases = [
        json.loads(line)
        for line in source_cases_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    validate_dataset(manifest, cases, source_cases)
    selected = [case for case in cases if case.split == split]
    if case_ids:
        requested = set(case_ids)
        selected = [case for case in selected if case.id in requested]
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ValueError("没有符合 split/case-id 的评测题")

    base_report: dict[str, Any] = {
        "schema_version": 1,
        "capture_kind": "paperleaf_multi_agent_live",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "id": manifest.dataset_id,
            "version": manifest.version,
            "annotation_status": manifest.annotation_status,
            "quality_claims_allowed": manifest.quality_claims_allowed,
            "split": split,
            "selected_case_count": len(selected),
        },
        "evidence_level": "not_executed",
        "quality_decision": quality_decision(manifest),
        "quality_note": "未完成人工盲评；本报告不得用于声称 v2 质量优于 v1",
        "token_measurement": "estimated_not_provider_billed_usage",
        "offline_evaluator_compatible": False,
        "offline_evaluator_note": (
            "维度、冲突和用户可见 partial 提示仍为 not_measured，禁止填零后送入 Go/No-Go"
        ),
        "pairs": [],
    }
    try:
        preflight, owner_id, mapping = await _preflight(
            manifest=manifest,
            cases=selected,
            config=config,
        )
    except Exception as exc:
        base_report["execution_status"] = "not_executed"
        base_report["preflight"] = {
            "status": "failed",
            "reasons": ["infrastructure_preflight_error"],
            "error_type": type(exc).__name__,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(base_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return base_report

    base_report["preflight"] = preflight
    if preflight["status"] != "passed" or owner_id is None:
        base_report["execution_status"] = "not_executed"
        for case in selected:
            base_report["pairs"].append(
                {
                    "case_id": case.id,
                    "order": expected_ab_order(case.id),
                    "v1": build_not_executed_variant("v1", "preflight_failed"),
                    "v2": build_not_executed_variant("v2", "preflight_failed"),
                }
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(base_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return base_report

    base_report["evidence_level"] = "real_infrastructure_preflight"
    if preflight_only:
        base_report["execution_status"] = "preflight_passed"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(base_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return base_report

    repository = SQLAlchemyRepository(config.session_secret)
    model_hash = _model_config_hash(config)
    executed_runs = 0
    for case in selected:
        order = expected_ab_order(case.id)
        if case.expected_path == "pregraph_reject":
            base_report["pairs"].append(
                {
                    "case_id": case.id,
                    "order": order,
                    "v1": build_not_executed_variant(
                        "v1", "pregraph_reject 需要独立 HTTP 越权 fixture"
                    ),
                    "v2": build_not_executed_variant(
                        "v2", "pregraph_reject 需要独立 HTTP 越权 fixture"
                    ),
                }
            )
            continue
        case_mapping = {paper_id: mapping[paper_id] for paper_id in case.scope_paper_ids}
        before_hash = await _snapshot_hash(owner_id, case_mapping)
        pair: dict[str, Any] = {
            "case_id": case.id,
            "order": order,
            "query_hash": query_hash(case),
            "scope_hash": scope_hash(case),
            "collection_snapshot_hash": before_hash,
            "model_config_hash": model_hash,
            "input_hash": _case_input_hash(case, before_hash, model_hash),
        }
        labels: tuple[Literal["v1", "v2"], Literal["v1", "v2"]] = (
            ("v1", "v2") if order == "v1_v2" else ("v2", "v1")
        )
        snapshot_drifted = False
        for label in labels:
            if snapshot_drifted:
                pair[label] = build_not_executed_variant(label, "collection_snapshot_drift")
                continue
            try:
                run_id, error = await _submit_variant(
                    repository=repository,
                    owner_id=owner_id,
                    case=case,
                    label=label,
                    logical_to_local=case_mapping,
                    config=config,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:
                pair[label] = build_not_executed_variant(
                    label, f"submission_error:{type(exc).__name__}"
                )
                continue
            if run_id is None:
                pair[label] = build_not_executed_variant(label, error or "submission_failed")
            else:
                executed_runs += 1
                pair[label] = await _capture_variant(
                    label=label,
                    run_id=run_id,
                    owner_id=owner_id,
                    case=case,
                    logical_to_local=case_mapping,
                    source_cases=source_cases,
                )
                if error:
                    pair[label]["execution_warning"] = error
            after_hash = await _snapshot_hash(owner_id, case_mapping)
            if after_hash != before_hash:
                snapshot_drifted = True
                pair["snapshot_drift"] = {
                    "status": "detected",
                    "before": before_hash,
                    "after": after_hash,
                }
        base_report["pairs"].append(pair)

    base_report["execution_status"] = "completed" if executed_runs else "not_executed"
    base_report["evidence_level"] = (
        "real_infrastructure_and_model_runs" if executed_runs else "real_infrastructure_preflight"
    )
    base_report["executed_run_count"] = executed_runs
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(base_report, ensure_ascii=False, indent=2), encoding="utf-8")
    return base_report


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperLeaf 多 Agent 真实 A/B 采集器")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--source-cases", required=True, type=Path)
    parser.add_argument("--split", choices=("dev", "test"), default="test")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds 必须大于 0")
    try:
        output = resolve_private_output_path(args.output)
    except ValueError as exc:
        parser.error(str(exc))
    report = asyncio.run(
        run_live_capture(
            manifest_path=args.manifest,
            cases_path=args.cases,
            source_manifest_path=args.source_manifest,
            source_cases_path=args.source_cases,
            output_path=output,
            split=args.split,
            case_ids=args.case_id,
            limit=args.limit,
            preflight_only=args.preflight_only,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(
        json.dumps(
            {
                "execution_status": report["execution_status"],
                "quality_decision": report["quality_decision"],
                "executed_run_count": report.get("executed_run_count", 0),
                "output": str(output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
