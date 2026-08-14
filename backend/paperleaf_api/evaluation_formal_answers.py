"""在冻结语料上采集真实 Agent 回答与可核验端到端指标。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .config import settings
from .db import get_session_factory
from .evaluation_dataset import read_manifest
from .evaluation_formal_protocol import (
    FormalEvaluationLock,
    matches_locked_text_sha,
    sha256_file,
    verify_formal_lock,
)
from .evaluation_formal_run import VARIANTS, _corpus_snapshot, _variant_settings
from .evaluation_holdout import merge_questions_and_oracle, read_oracle, read_questions
from .evaluation_multi_agent_live import _harness_flags
from .evaluation_production import preflight_production_corpus
from .models import AgentRun, AgentRunEvent, AgentToolCall, ChatMessage, Paper, PaperChunk
from .rag.retrieval_config import freeze_retrieval_config
from .repository import SQLAlchemyRepository

TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
ANSWER_PROTOCOL_STATUS = "frozen_before_first_answer_run"
ORCHESTRATION_VERSION = "single_agent_v1"


def build_evaluation_repository() -> SQLAlchemyRepository:
    """使用与 API/Worker 相同的会话密钥构造真实仓库。"""

    return SQLAlchemyRepository(settings.session_secret)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: Sequence[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _metric(numerator: int | float, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def validate_answer_protocol(protocol: dict[str, Any], *, lock_path: Path) -> None:
    if protocol.get("status") != ANSWER_PROTOCOL_STATUS:
        raise ValueError("回答协议尚未在首次运行前冻结")
    if not matches_locked_text_sha(lock_path, str(protocol.get("dataset_lock_sha256", ""))):
        raise ValueError("回答协议引用的数据集 lock SHA-256 不匹配")
    if protocol.get("case_count") != 100:
        raise ValueError("回答协议必须完整覆盖 100 题")
    if protocol.get("orchestration_version") != ORCHESTRATION_VERSION:
        raise ValueError("正式端到端回答必须固定使用 single_agent_v1")
    if protocol.get("retrieval_variant") != "final_combined":
        raise ValueError("正式端到端回答必须固定使用 final_combined 检索")
    if int(protocol.get("max_concurrency", 0)) not in {1, 2, 3}:
        raise ValueError("正式端到端回答并发必须冻结在 1～3")
    review = dict(protocol.get("human_review", {}))
    if int(review.get("minimum_cases", 0)) < 30:
        raise ValueError("人工盲评预注册样本不得少于 30")


async def _wait_for_run(
    repository: SQLAlchemyRepository,
    *,
    run_id: str,
    owner_id: str,
    timeout_seconds: float,
) -> AgentRun | None:
    deadline = time.monotonic() + timeout_seconds
    last: AgentRun | None = None
    while time.monotonic() < deadline:
        last = await repository.get_owned_agent_run(run_id, owner_id)
        if last is None or last.status in TERMINAL_RUN_STATUSES:
            return last
        await asyncio.sleep(1)
    return last


async def _load_run_facts(
    run_id: str,
) -> tuple[AgentRun | None, ChatMessage | None, list[AgentRunEvent], list[AgentToolCall]]:
    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
        message = (
            await session.get(ChatMessage, run.assistant_message_id)
            if run is not None and run.assistant_message_id
            else None
        )
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
        for item in [run, message, *events, *calls]:
            if item is not None:
                session.expunge(item)
    return run, message, events, calls


async def _load_citation_chunks(chunk_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
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
                    Paper.title,
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
            "owner_id": row.owner_id,
            "paper_title": row.title,
            "quote": row.text[:500],
        }
        for row in rows
    }


def audit_answer_citations(
    *,
    citations: Sequence[dict[str, Any]],
    chunks: dict[str, dict[str, Any]],
    owner_id: str,
    local_scope: Sequence[str],
    local_to_logical: dict[str, str],
    gold_groups: Sequence[set[tuple[str, int]]],
) -> dict[str, Any]:
    """逐条验证 Chunk、论文、用户、物理页和 Gold 有用性。"""

    legal = page_legal = gold_useful = 0
    rows: list[dict[str, Any]] = []
    allowed = set(local_scope)
    for citation in citations:
        chunk_id = str(citation.get("chunk_id", ""))
        record = chunks.get(chunk_id)
        reasons: list[str] = []
        if record is None:
            reasons.append("chunk_not_found")
        else:
            if record["owner_id"] != owner_id:
                reasons.append("cross_user_chunk")
            if record["paper_id"] not in allowed:
                reasons.append("scope_violation")
            cited_paper = str(citation.get("paper_id") or "")
            if cited_paper and cited_paper != record["paper_id"]:
                reasons.append("paper_mismatch")
            try:
                cited_page = int(citation.get("physical_page", 0))
            except (TypeError, ValueError):
                cited_page = 0
            if cited_page != int(record["physical_page"]):
                reasons.append("physical_page_mismatch")
        is_legal = not reasons
        legal += int(is_legal)
        page_legal += int(is_legal and record is not None)
        logical_id = local_to_logical.get(str(record["paper_id"])) if record else None
        pair = (logical_id, int(record["physical_page"])) if logical_id and record else None
        useful = bool(is_legal and pair and any(pair in group for group in gold_groups))
        gold_useful += int(useful)
        rows.append(
            {
                "chunk_id": chunk_id,
                "logical_paper_id": logical_id,
                "physical_page": int(record["physical_page"]) if record else None,
                "legal": is_legal,
                "gold_useful": useful,
                "reasons": reasons,
                "paper_title": record.get("paper_title") if record else None,
                "quote": record.get("quote") if record else None,
            }
        )
    return {
        "citation_count": len(citations),
        "legal_count": legal,
        "physical_page_legal_count": page_legal,
        "gold_useful_count": gold_useful,
        "citations": rows,
    }


def _gold_groups(case: Any) -> list[set[tuple[str, int]]]:
    return [
        {(item.paper_id, item.physical_page) for item in group.items}
        for group in case.acceptable_evidence_groups
    ]


def _is_abstained(outcome: str, answer: str) -> bool:
    if outcome in {"abstained", "model_failed", "unverified_answer", "scope_violation"}:
        return True
    return not answer.strip()


def _evaluation_client_message_id(prefix: str, case_id: str) -> str:
    """生成满足数据库 varchar(100) 约束、且每次提交唯一的评测消息 ID。"""

    stable = hashlib.sha256(f"{prefix}\0{case_id}".encode()).hexdigest()[:20]
    return f"eval-{stable}-{secrets.token_hex(6)}"


async def _run_case(
    *,
    repository: SQLAlchemyRepository,
    case: Any,
    owner_id: str,
    paper_id_map: dict[str, str],
    timeout_seconds: float,
    title_prefix: str = "[正式评测]",
    idempotency_prefix: str = "formal-answer",
) -> dict[str, Any]:
    local_scope = [paper_id_map[paper_id] for paper_id in case.paper_ids]
    chat_session = await repository.create_chat_session(
        owner_id, f"{title_prefix} {case.id}", "library", None, None
    )
    scope_snapshot = {
        "type": "library",
        "paper_id": None,
        "collection_id": None,
        "paper_ids": local_scope,
        "web_enabled": False,
        "client_context": {},
        "harness": _harness_flags(settings),
        "retrieval_config": freeze_retrieval_config(settings),
        "orchestration_version": ORCHESTRATION_VERSION,
    }
    request_hash = _sha256_json(
        {"content": case.query, "scope_snapshot": scope_snapshot, "protocol": 1}
    )
    submission = await repository.submit_chat_message(
        chat_session.id,
        owner_id,
        case.query,
        _evaluation_client_message_id(idempotency_prefix, case.id),
        request_hash,
        scope_snapshot,
    )
    if submission is None:
        raise RuntimeError(f"{case.id}: repository 拒绝提交")
    finished = await _wait_for_run(
        repository,
        run_id=submission.run.id,
        owner_id=owner_id,
        timeout_seconds=timeout_seconds,
    )
    if finished is None or finished.status not in TERMINAL_RUN_STATUSES:
        raise RuntimeError(f"{case.id}: Run 未在时限内进入合法终态")
    run, message, events, calls = await _load_run_facts(submission.run.id)
    if run is None or message is None:
        raise RuntimeError(f"{case.id}: Run 或回答消息缺失")
    summary = dict(run.result_summary or {})
    quality = dict(summary.get("evidence_quality", {}) or {})
    rag_trace = dict(summary.get("rag_trace", {}) or {})
    citations = list(message.citations or summary.get("citations", []) or [])
    chunks = await _load_citation_chunks(
        [str(item.get("chunk_id", "")) for item in citations if item.get("chunk_id")]
    )
    local_to_logical = {local: logical for logical, local in paper_id_map.items()}
    citation_audit = audit_answer_citations(
        citations=citations,
        chunks=chunks,
        owner_id=owner_id,
        local_scope=local_scope,
        local_to_logical=local_to_logical,
        gold_groups=_gold_groups(case),
    )
    outcome = str(rag_trace.get("outcome", ""))
    answer = str(message.content or summary.get("answer", ""))
    abstained = _is_abstained(outcome, answer)
    first_delta = next((event for event in events if event.event == "message_delta"), None)
    return {
        "case_id": case.id,
        "query": case.query,
        "category": case.category,
        "answerable": case.answerable,
        "scope_paper_ids": list(case.paper_ids),
        "gold_evidence_groups": [
            sorted([list(pair) for pair in group]) for group in _gold_groups(case)
        ],
        "run_id": run.id,
        "run_status": run.status,
        "error_code": run.error_code,
        "orchestration_version": run.orchestration_version,
        "answer": answer,
        "outcome": outcome,
        "abstained": abstained,
        "citations": citations,
        "citation_audit": citation_audit,
        "claim_count": int(quality.get("claim_count", 0) or 0),
        "cited_claim_count": int(quality.get("cited_claim_count", 0) or 0),
        "supported_claim_count": int(quality.get("supported_claim_count", 0) or 0),
        "answer_support_grade": quality.get("answer_support_grade"),
        "duration_ms": run.duration_ms,
        "time_to_first_verified_delta_ms": (
            max(0, round((first_delta.created_at - run.created_at).total_seconds() * 1000))
            if first_delta is not None
            else None
        ),
        "model_call_count": len(list(summary.get("model_attempts", []) or [])),
        "tool_call_count": len(calls),
    }


def aggregate_answer_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    total_citations = sum(row["citation_audit"]["citation_count"] for row in rows)
    legal = sum(row["citation_audit"]["legal_count"] for row in rows)
    page_legal = sum(row["citation_audit"]["physical_page_legal_count"] for row in rows)
    useful = sum(row["citation_audit"]["gold_useful_count"] for row in rows)
    claims = sum(row["claim_count"] for row in rows)
    cited_claims = sum(row["cited_claim_count"] for row in rows)
    supported_claims = sum(row["supported_claim_count"] for row in rows)
    unanswerable = [row for row in rows if not row["answerable"]]
    answerable = [row for row in rows if row["answerable"]]
    wrong_unanswerable = sum(not row["abstained"] for row in unanswerable)
    over_refused = sum(row["abstained"] for row in answerable)
    completed = sum(row["run_status"] == "completed" for row in rows)
    durations = sorted(int(row["duration_ms"] or 0) for row in rows)
    p95_index = max(0, min(len(durations) - 1, (len(durations) * 95 + 99) // 100 - 1))
    return {
        "case_completion_rate": _metric(completed, len(rows)),
        "citation_legality_rate": _metric(legal, total_citations),
        "citation_physical_page_legality_rate": _metric(page_legal, total_citations),
        "citation_gold_usefulness_rate": _metric(useful, total_citations),
        "claim_citation_coverage": _metric(cited_claims, claims),
        "claim_support_rate": _metric(supported_claims, claims),
        "unsupported_claim_rate": _metric(max(0, claims - supported_claims), claims),
        "unanswerable_wrong_answer_rate": _metric(wrong_unanswerable, len(unanswerable)),
        "answerable_over_refusal_rate": _metric(over_refused, len(answerable)),
        "latency_ms": {
            "p50": durations[len(durations) // 2] if durations else None,
            "p95": durations[p95_index] if durations else None,
        },
        "model_call_count": sum(row["model_call_count"] for row in rows),
        "tool_call_count": sum(row["tool_call_count"] for row in rows),
        "human_review_status": "human_review_pending",
    }


def build_human_review_packet(
    rows: Sequence[dict[str, Any]], *, minimum_cases: int
) -> list[dict[str, Any]]:
    candidates = [row for row in rows if row["run_status"] == "completed"]
    selected = sorted(
        candidates,
        key=lambda row: hashlib.sha256(str(row["case_id"]).encode()).hexdigest(),
    )[:minimum_cases]
    if len(selected) != minimum_cases:
        raise RuntimeError("完成回答不足，无法生成预注册数量的人工盲评包")
    return [
        {
            "review_id": f"HR-{index:03d}",
            "query": row["query"],
            "answer": row["answer"],
            "citation_evidence": [
                {
                    "paper_title": item["paper_title"],
                    "physical_page": item["physical_page"],
                    "quote": item["quote"],
                }
                for item in row["citation_audit"]["citations"]
                if item["legal"]
            ],
            "ratings": {
                "factuality_1_to_5": None,
                "completeness_1_to_5": None,
                "citation_usefulness_1_to_5": None,
                "overall_1_to_5": None,
                "human_annotator": "",
                "notes": "",
            },
        }
        for index, row in enumerate(selected, 1)
    ]


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.output_dir.exists():
        raise FileExistsError("结果目录已存在，禁止覆盖正式回答证据")
    args.output_dir.mkdir(parents=True)
    protocol = json.loads(args.answer_protocol.read_text(encoding="utf-8"))
    validate_answer_protocol(protocol, lock_path=args.lock)
    lock = FormalEvaluationLock.model_validate_json(args.lock.read_text(encoding="utf-8"))
    verification = verify_formal_lock(
        lock,
        manifest_path=args.manifest,
        questions_path=args.questions,
        oracle_path=args.oracle,
        exclusion_manifest_paths=args.exclude_manifest,
    )
    cases = merge_questions_and_oracle(read_questions(args.questions), read_oracle(args.oracle))
    if len(cases) != int(protocol["case_count"]):
        raise RuntimeError("正式回答题目不完整，拒绝缩小分母")
    config = _variant_settings(VARIANTS["final_combined"])
    required_ids = {paper_id for case in cases for paper_id in case.paper_ids}
    preflight = await preflight_production_corpus(
        read_manifest(args.manifest),
        user_email=args.user_email,
        required_paper_ids=required_ids,
        config=config,
    )
    manifest_record = {
        "schema_version": 1,
        "status": "not_executed",
        "created_at": datetime.now(UTC).isoformat(),
        "git_sha": args.git_sha,
        "docker_image_digest": args.docker_image_digest,
        "dataset_lock_sha256": sha256_file(args.lock),
        "answer_protocol_sha256": sha256_file(args.answer_protocol),
        "protocol_verification": verification,
        "preflight": {
            key: value for key, value in preflight.items() if key not in {"user_id", "paper_id_map"}
        },
    }
    if preflight["status"] != "ready":
        _write_json(args.output_dir / "run_manifest.json", manifest_record)
        raise RuntimeError(f"正式回答预检失败：{preflight['reason']}")
    paper_id_map = dict(preflight["paper_id_map"])
    snapshot_before = await _corpus_snapshot(paper_id_map)
    repository = build_evaluation_repository()
    semaphore = asyncio.Semaphore(int(protocol["max_concurrency"]))

    async def run_bounded(case: Any) -> dict[str, Any]:
        async with semaphore:
            return await _run_case(
                repository=repository,
                case=case,
                owner_id=str(preflight["user_id"]),
                paper_id_map=paper_id_map,
                timeout_seconds=float(protocol["timeout_seconds_per_run"]),
            )

    rows = list(await asyncio.gather(*(run_bounded(case) for case in cases)))
    if len(rows) != len(cases):
        raise RuntimeError("逐题回答不完整，拒绝生成指标")
    snapshot_after = await _corpus_snapshot(paper_id_map)
    if snapshot_after != snapshot_before:
        raise RuntimeError("正式回答期间 Chunk 或 Embedding 快照发生漂移")
    metrics = aggregate_answer_metrics(rows)
    blind = build_human_review_packet(
        rows, minimum_cases=int(protocol["human_review"]["minimum_cases"])
    )
    _write_jsonl(args.output_dir / "per_query_answers.jsonl", rows)
    _write_json(args.output_dir / "metrics.json", metrics)
    _write_jsonl(args.output_dir / "human_blind_review.jsonl", blind)
    manifest_record.update(
        {
            "status": "completed_human_review_pending",
            "completed_at": datetime.now(UTC).isoformat(),
            "corpus": snapshot_before,
            "artifacts": {
                name: sha256_file(args.output_dir / name)
                for name in (
                    "per_query_answers.jsonl",
                    "metrics.json",
                    "human_blind_review.jsonl",
                )
            },
        }
    )
    _write_json(args.output_dir / "run_manifest.json", manifest_record)
    return manifest_record


def main() -> None:
    parser = argparse.ArgumentParser(description="执行 PaperLeaf 正式端到端回答评测")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--oracle", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--answer-protocol", required=True, type=Path)
    parser.add_argument("--exclude-manifest", action="append", type=Path, default=[])
    parser.add_argument("--user-email", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--docker-image-digest", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(
        json.dumps(
            {"status": result["status"], "output": str(args.output_dir)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
