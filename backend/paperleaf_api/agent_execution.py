"""持久化 Agent Run 执行与经核验段落发布。"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from .model_runtime import ModelRuntimeError, collect_model_attempts
from .rag.answer_quality import AnswerQualityPolicy
from .rag.citations import CitationClaim, Evidence, validate_citations
from .rag_observability import (
    build_rag_trace,
    classify_intent,
    record_rag_run,
)

_CITATION_RE = re.compile(r"\[chunk:([^\]]+)\]")
_CONTROLLED_NOTICE_RE = re.compile(r"^\s*>?\s*证据说明[：:]", re.IGNORECASE)
_STRUCTURAL_MARKDOWN_RE = re.compile(r"^\s*(?:#{1,6}\s+[^\n]+|[-*_]{3,})\s*$")


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
) -> dict[str, Any]:
    graph_config = {
        "recursion_limit": 8,
        "configurable": {"thread_id": run.thread_id},
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
    scope = str(snapshot.get("type", "library"))
    intent_started_at = time.perf_counter()
    intent = classify_intent(
        query,
        scope=scope,
        selected_paper_count=len(snapshot.get("paper_ids", [])),
        web_enabled=bool(snapshot.get("web_enabled", False)),
    )
    intent_ms = round((time.perf_counter() - intent_started_at) * 1000)
    visible_history = await repository.list_chat_messages(run.session_id, run.user_id)
    initial = {
        "run_id": run.id,
        "session_id": run.session_id,
        "user_id": run.user_id,
        "query": query,
        "messages": [
            {
                "role": item.role,
                "content": item.content,
            }
            for item in (visible_history or [])
            if item.content.strip() and item.id != run.assistant_message_id
        ][-9:],
        "intent": intent,
        "scope": scope,
        "selected_paper_ids": list(snapshot.get("paper_ids", [])),
        "web_enabled": bool(snapshot.get("web_enabled", False)),
        "tool_steps": 0,
        "stage_timings_ms": {"intent": intent_ms},
        "status": "pending",
    }
    await repository.append_agent_run_event(
        run_id,
        "node_started",
        {"node": "retrieve_library", "stage": "检索文献证据"},
        event_key="stage:retrieve:start",
        claim_token=claim_token,
    )
    await repository.append_agent_run_event(
        run_id,
        "tool_started",
        {"tool": "search_library"},
        event_key="stage:tool:search:start",
        claim_token=claim_token,
    )
    with collect_model_attempts() as attempts:
        try:
            result = await _invoke_with_cancel(repository, graph, run, initial)
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
    model_attempts = [item.as_dict() for item in attempts]
    stage_timings = dict(result.get("stage_timings_ms", {}))
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
    if evidence and not has_cited_answer:
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
    outcome = "cited_answer" if all_citation_dicts else "abstained"
    await _finish_observed_run(
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
        },
    )
