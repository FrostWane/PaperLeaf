"""PaperLeaf Agent Harness 的真实模型闭环评测。

本模块只用于显式的本地验收命令，不进入普通 CI。它通过真实 HTTP API 提交问题，
等待 Worker 执行当前模型链路，再从 PostgreSQL 读取同一 Run 的持久化审计字段。
凭证仅从环境变量读取，报告不会包含 Cookie、密码或模型密钥。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from .agent.discovery_policy import requested_paper_count
from .config import settings
from .db import get_session_factory
from .mcp_gateway import McpGateway, McpGatewayError
from .models import AgentRun, AgentToolCall, Paper, PaperPage
from .repository import SQLAlchemyRepository
from .runtime_store import create_runtime_store

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
OPEN_ARXIV_IDS = ("1706.03762", "1810.04805", "2005.11401")
COLLECTION_NAME = "[系统验收] Harness 真实闭环"
_RECOMMENDATION_ROW_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*\*\*(.+?)\*\*\s*\|\s*(\d{4}|未提供)\s*\|",
    re.MULTILINE,
)


@dataclass(frozen=True)
class LiveScenario:
    index: int
    category: str
    title: str
    session_type: str
    question: str
    expected_skills: tuple[str, ...]
    paper_id: str | None = None
    collection_id: str | None = None
    physical_page: int | None = None
    selected_text: str | None = None
    web_enabled: bool = False
    require_citations: bool = True
    require_native_tools: bool = False
    expected_tools: tuple[str, ...] = ()
    expected_attempted_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    group: str | None = None
    library_titles: tuple[str, ...] = ()


@dataclass
class LiveRunResult:
    index: int
    category: str
    title: str
    question: str
    session_id: str | None = None
    run_id: str | None = None
    status: str = "not_started"
    error_code: str | None = None
    selected_skill: str | None = None
    native_function_calling_attempted: bool = False
    tool_mode_active: bool = False
    selection_scope_locked: bool = False
    selection_evidence_count: int = 0
    tool_fallback_reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_input_tokens: int | None = None
    hard_limit: int | None = None
    citation_count: int = 0
    citation_pages: list[int] = field(default_factory=list)
    vector_fallback_reasons: list[str] = field(default_factory=list)
    external_provider_degradations: list[str] = field(default_factory=list)
    active_task: dict[str, Any] = field(default_factory=dict)
    provider_policy: dict[str, Any] = field(default_factory=dict)
    displayed_recommendations: list[dict[str, Any]] = field(default_factory=list)
    library_titles: list[str] = field(default_factory=list)
    answer: str = ""
    duration_ms: int | None = None
    structural_pass: bool = False
    failures: list[str] = field(default_factory=list)


class SubmissionLimiter:
    """在客户端主动低于服务端限流，避免把 429 误计为业务失败。"""

    def __init__(self, limit: int = 10, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.timestamps: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.timestamps and now - self.timestamps[0] >= self.window_seconds:
                    self.timestamps.popleft()
                if len(self.timestamps) < self.limit:
                    self.timestamps.append(now)
                    return
                await asyncio.sleep(max(0.05, self.window_seconds - (now - self.timestamps[0])))


def _recommendation_rows(answer: str) -> list[tuple[str, str]]:
    return [
        (" ".join(title.split()).casefold(), year)
        for title, year in _RECOMMENDATION_ROW_RE.findall(answer)
    ]


def _normalized_title(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", value.casefold()))


def _grade_recommendation_sequence(results: list[LiveRunResult]) -> None:
    """验证数量、去重、年份、来源与机器相关性代理，禁止空结果真空通过。"""

    seen_titles: set[str] = set()
    inherited_count: int | None = None
    for item in results:
        rows = _recommendation_rows(item.answer)
        current_titles = {title for title, _year in rows}
        task = dict(item.active_task or {})
        requested_count = _as_int(task.get("requested_count"))
        if requested_count is None:
            requested_count = requested_paper_count(item.question, default=inherited_count)
        if requested_count is not None:
            inherited_count = requested_count
        if not rows:
            item.failures.append("recommendation_empty")
        elif requested_count is not None and len(rows) != requested_count:
            item.failures.append(
                f"recommendation_count:{len(rows)}/{requested_count}"
            )
        if current_titles & seen_titles:
            item.failures.append("recommendation_batch_repeated")
        year_from = _as_int(task.get("year_from"))
        year_to = _as_int(task.get("year_to"))
        requested_years = {
            value for value in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", item.question)
        }
        if year_from is not None:
            year_to = year_to if year_to is not None else year_from
            if any(
                not year.isdigit() or not year_from <= int(year) <= year_to
                for _title, year in rows
            ):
                item.failures.append("recommendation_year_constraint_lost")
        elif requested_years and any(year not in requested_years for _title, year in rows):
            item.failures.append("recommendation_year_constraint_lost")
        library_titles = {_normalized_title(value) for value in item.library_titles if value}
        if any(_normalized_title(title) in library_titles for title in current_titles):
            item.failures.append("recommendation_contains_library_paper")
        structured = list(item.displayed_recommendations or [])
        if len(structured) != len(rows):
            item.failures.append("recommendation_structured_output_mismatch")
        elif structured:
            relevant_proxy = sum(
                bool(candidate.get("matched_scope_title"))
                or float(candidate.get("lexical_score") or 0) > 0
                or float(candidate.get("relevance_score") or 0) > 0
                for candidate in structured
            )
            if relevant_proxy / len(structured) < 0.8:
                item.failures.append("recommendation_topic_proxy_below_threshold")
        policy = dict(item.provider_policy or {})
        denied = {str(value) for value in policy.get("denied", [])}
        attempted = {
            str(key): int(value)
            for key, value in dict(policy.get("attempted", {}) or {}).items()
        }
        if any(value > 1 for value in attempted.values()):
            item.failures.append("provider_budget_exceeded")
        if denied & {provider for provider, count in attempted.items() if count > 0}:
            item.failures.append("denied_provider_attempted")
        item.structural_pass = not item.failures
        seen_titles.update(current_titles)


class LiveHarness:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        concurrency: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.concurrency = max(1, min(concurrency, 6))
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(60.0),
            follow_redirects=False,
        )
        self.csrf = ""
        self.user_id = ""
        self.limiter = SubmissionLimiter()

    async def close(self) -> None:
        await self.client.aclose()

    async def login(self) -> None:
        email = os.getenv("PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL")
        password = os.getenv("PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD")
        if not email or not password:
            raise RuntimeError("缺少本地管理员环境变量，无法执行真实闭环")
        response = await self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        response.raise_for_status()
        self.csrf = self.client.cookies.get(settings.csrf_cookie) or ""
        self.user_id = str(response.json().get("id", ""))
        if not self.csrf or not self.user_id:
            raise RuntimeError("登录成功但缺少 CSRF 或用户标识")

    def headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        value = {"X-CSRF-Token": self.csrf}
        if idempotency_key:
            value["Idempotency-Key"] = idempotency_key
        return value

    async def ensure_collection_and_papers(
        self, *, import_papers: bool
    ) -> tuple[str, list[dict[str, Any]]]:
        collections = (await self.client.get("/api/v1/collections")).json()
        collection = next(
            (item for item in collections if item.get("name") == COLLECTION_NAME), None
        )
        if collection is None:
            response = await self.client.post(
                "/api/v1/collections",
                headers=self.headers(),
                json={"name": COLLECTION_NAME, "description": "本地真实 Harness 验收数据"},
            )
            response.raise_for_status()
            collection = response.json()
        collection_id = str(collection["id"])

        papers = (await self.client.get("/api/v1/papers")).json()
        if import_papers:
            existing_arxiv = {str(item.get("arxiv_id")): item for item in papers}
            for arxiv_id in OPEN_ARXIV_IDS:
                if arxiv_id in existing_arxiv:
                    existing = existing_arxiv[arxiv_id]
                    if existing.get("status") == "failed":
                        response = await self.client.post(
                            f"/api/v1/papers/{existing['id']}/retry",
                            headers=self.headers(),
                        )
                        response.raise_for_status()
                    continue
                response: httpx.Response | None = None
                for attempt in range(3):
                    response = await self.client.post(
                        "/api/v1/discover/arxiv/import",
                        headers=self.headers(),
                        json={"arxiv_id": arxiv_id},
                    )
                    if response.status_code != 502:
                        break
                    await asyncio.sleep(2**attempt)
                assert response is not None
                if response.status_code not in {201, 409}:
                    raise RuntimeError(
                        f"导入 arXiv {arxiv_id} 失败：HTTP {response.status_code}"
                    )
            papers = await self.wait_for_imported_papers()

        ready = [item for item in papers if item.get("status") in {"ready", "partial"}]
        if len(ready) < 2:
            raise RuntimeError("真实评测至少需要两篇已建立索引的论文")
        for paper in ready:
            response = await self.client.post(
                f"/api/v1/collections/{collection_id}/papers/{paper['id']}",
                headers=self.headers(),
            )
            if response.status_code not in {200, 404}:
                response.raise_for_status()
        return collection_id, ready

    async def wait_for_imported_papers(self) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(self.timeout_seconds, 900)
        while time.monotonic() < deadline:
            papers = (await self.client.get("/api/v1/papers")).json()
            imported = {
                str(item.get("arxiv_id")): item
                for item in papers
                if str(item.get("arxiv_id")) in OPEN_ARXIV_IDS
            }
            if len(imported) == len(OPEN_ARXIV_IDS) and all(
                item.get("status") in {"ready", "partial"} for item in imported.values()
            ):
                return papers
            failed = [item for item in imported.values() if item.get("status") == "failed"]
            if failed:
                raise RuntimeError("至少一篇真实 arXiv 论文解析失败")
            await asyncio.sleep(3)
        raise RuntimeError("等待真实 arXiv 论文建立索引超时")

    async def page_samples(self, paper_ids: list[str]) -> dict[str, list[tuple[int, str]]]:
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(PaperPage.paper_id, PaperPage.physical_page, PaperPage.text)
                    .where(PaperPage.paper_id.in_(paper_ids), PaperPage.text != "")
                    .order_by(PaperPage.paper_id, PaperPage.physical_page)
                )
            ).all()
        samples: dict[str, list[tuple[int, str]]] = {paper_id: [] for paper_id in paper_ids}
        for paper_id, page, text in rows:
            normalized = " ".join(str(text).split())
            if len(normalized) < 30:
                continue
            start = min(max(0, len(normalized) // 8), max(0, len(normalized) - 180))
            excerpt = normalized[start : start + 180].strip()
            samples[str(paper_id)].append((int(page), excerpt))
        return {key: value for key, value in samples.items() if value}

    async def create_session(self, scenario: LiveScenario) -> str:
        unique_suffix = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        payload: dict[str, Any] = {
            "title": f"{scenario.title} · {unique_suffix} · {secrets.token_hex(2)}",
            "type": scenario.session_type,
        }
        if scenario.paper_id:
            payload["paper_id"] = scenario.paper_id
        if scenario.collection_id:
            payload["collection_id"] = scenario.collection_id
        response = await self.client.post(
            "/api/v1/chat/sessions", headers=self.headers(), json=payload
        )
        response.raise_for_status()
        return str(response.json()["id"])

    async def submit(self, session_id: str, scenario: LiveScenario) -> str:
        payload: dict[str, Any] = {
            "content": scenario.question,
            "web_enabled": scenario.web_enabled,
        }
        if scenario.paper_id:
            context: dict[str, Any] = {
                "route": f"/library/{scenario.paper_id}",
                "paper_id": scenario.paper_id,
                "physical_page": scenario.physical_page or 1,
                "active_panel": "chat",
            }
            if scenario.selected_text:
                normalized = " ".join(scenario.selected_text.split())
                context["selected_text"] = scenario.selected_text
                context["selected_text_hash"] = hashlib.sha256(
                    normalized.encode("utf-8")
                ).hexdigest()
            payload["client_context"] = context
        await self.limiter.wait()
        response = await self.client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            headers=self.headers(
                idempotency_key=f"harness-live-{scenario.index}-{secrets.token_hex(8)}"
            ),
            json=payload,
        )
        if response.status_code == 429:
            await asyncio.sleep(float(response.headers.get("Retry-After", "5")))
            return await self.submit(session_id, scenario)
        response.raise_for_status()
        return str(response.json()["run_id"])

    async def wait_run(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = await self.client.get(f"/api/v1/agent/runs/{run_id}")
            response.raise_for_status()
            last = response.json()
            if last.get("status") in TERMINAL_STATUSES:
                return last
            await asyncio.sleep(1.5)
        raise TimeoutError(f"Agent Run {run_id} 未在预算内进入终态")

    async def audit_run(self, run_id: str) -> tuple[AgentRun, list[AgentToolCall]]:
        async with get_session_factory()() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise RuntimeError("HTTP Run 已返回，但数据库不存在对应记录")
            calls = list(
                await session.scalars(
                    select(AgentToolCall)
                    .where(AgentToolCall.run_id == run_id)
                    .order_by(AgentToolCall.created_at)
                )
            )
            session.expunge(run)
            for item in calls:
                session.expunge(item)
            return run, calls

    async def run_scenario(
        self, scenario: LiveScenario, *, session_id: str | None = None
    ) -> LiveRunResult:
        result = LiveRunResult(
            index=scenario.index,
            category=scenario.category,
            title=scenario.title,
            question=scenario.question,
            library_titles=list(scenario.library_titles),
        )
        started = time.perf_counter()
        try:
            result.session_id = session_id or await self.create_session(scenario)
            result.run_id = await self.submit(result.session_id, scenario)
            public_run = await self.wait_run(result.run_id)
            run, calls = await self.audit_run(result.run_id)
            result.status = run.status
            result.error_code = run.error_code
            result.selected_skill = run.selected_skill
            trace = dict(run.harness_trace or {})
            result.provider_policy = dict(trace.get("provider_policy", {}) or {})
            result.active_task = dict(
                (run.context_snapshot or {}).get("resolved_references", {}).get(
                    "active_task", {}
                )
                or {}
            )
            result.native_function_calling_attempted = bool(
                trace.get("native_function_calling_attempted")
            )
            result.tool_mode_active = bool(trace.get("tool_mode_active"))
            result.selection_scope_locked = bool(trace.get("selection_scope_locked"))
            result.selection_evidence_count = int(
                trace.get("selection_evidence_count", 0) or 0
            )
            result.tool_fallback_reason = str(
                trace.get("function_fallback_reason") or trace.get("fallback_reason") or ""
            ) or None
            result.tool_calls = [
                {
                    "tool": item.tool_name,
                    "status": item.status,
                    "error_code": item.error_code,
                    "duration_ms": item.duration_ms,
                }
                for item in calls
            ]
            usage = dict((run.context_snapshot or {}).get("usage", {}) or {})
            result.final_input_tokens = _as_int(usage.get("final_input_tokens"))
            result.hard_limit = _as_int(usage.get("hard_limit"))
            citations = list(public_run.get("citations", []) or [])
            result.citation_count = len(citations)
            result.citation_pages = sorted(
                {
                    int(item["physical_page"])
                    for item in citations
                    if item.get("physical_page") is not None
                }
            )
            result.answer = str(public_run.get("answer", ""))
            result.displayed_recommendations = list(
                (run.result_summary or {}).get("displayed_recommendations", []) or []
            )
            rag_trace = dict((run.result_summary or {}).get("rag_trace", {}) or {})
            result.vector_fallback_reasons = [
                str(item) for item in rag_trace.get("vector_fallback_reasons", []) or []
            ]
            result.duration_ms = run.duration_ms
            self._grade(scenario, run, citations, result)
        except Exception as exc:
            result.status = "runner_error"
            result.failures.append(f"runner:{type(exc).__name__}:{str(exc)[:180]}")
        finally:
            if result.duration_ms is None:
                result.duration_ms = round((time.perf_counter() - started) * 1000)
            result.structural_pass = not result.failures
        return result

    def _grade(
        self,
        scenario: LiveScenario,
        run: AgentRun,
        citations: list[dict[str, Any]],
        result: LiveRunResult,
    ) -> None:
        if run.status != "completed":
            result.failures.append(f"terminal_status:{run.status}:{run.error_code}")
        if scenario.expected_skills and run.selected_skill not in scenario.expected_skills:
            result.failures.append(f"skill:{run.selected_skill}")
        if (
            result.final_input_tokens is None
            or result.hard_limit is None
            or result.final_input_tokens > result.hard_limit
        ):
            result.failures.append("context_budget")
        if scenario.require_native_tools and not result.native_function_calling_attempted:
            result.failures.append("native_function_calling")
        if scenario.require_native_tools and not result.tool_calls:
            result.failures.append("external_tool_call_missing")
        expected_attempts = [
            call
            for call in result.tool_calls
            if str(call.get("tool", ""))
            in (scenario.expected_tools or scenario.expected_attempted_tools)
        ]
        controlled_external_failure = bool(
            expected_attempts
            and not any(call.get("status") == "succeeded" for call in expected_attempts)
            and any(
                any(
                    marker in str(call.get("error_code") or "").casefold()
                    for marker in (
                        "rate_limit",
                        "timeout",
                        "key_required",
                        "disabled",
                        "circuit_open",
                    )
                )
                for call in expected_attempts
            )
            and "没有返回可核验的候选论文" in result.answer
        )
        if controlled_external_failure:
            result.external_provider_degradations = sorted(
                {
                    str(call.get("error_code"))
                    for call in expected_attempts
                    if call.get("error_code")
                }
            )
        if (
            scenario.require_native_tools
            and not any(call.get("status") == "succeeded" for call in result.tool_calls)
            and not controlled_external_failure
        ):
            result.failures.append("external_tool_call_not_succeeded")
        succeeded_tools = {
            str(call.get("tool", ""))
            for call in result.tool_calls
            if call.get("status") == "succeeded"
        }
        attempted_tools = {str(call.get("tool", "")) for call in result.tool_calls}
        for expected_tool in scenario.expected_attempted_tools:
            if expected_tool not in attempted_tools:
                result.failures.append(f"expected_tool_not_attempted:{expected_tool}")
        for expected_tool in scenario.expected_tools:
            if expected_tool not in succeeded_tools and not controlled_external_failure:
                result.failures.append(f"expected_tool_missing:{expected_tool}")
        for forbidden_tool in scenario.forbidden_tools:
            if forbidden_tool in succeeded_tools:
                result.failures.append(f"forbidden_tool_used:{forbidden_tool}")
        if (
            scenario.require_native_tools
            and not result.tool_mode_active
            and not controlled_external_failure
        ):
            result.failures.append("usable_external_output_missing")
        if result.tool_mode_active and not result.tool_calls:
            result.failures.append("active_tool_output_not_persisted")
        if scenario.require_citations and not citations:
            result.failures.append("citations_missing")
        scope_ids = set((run.scope_snapshot or {}).get("paper_ids", []) or [])
        for citation in citations:
            if str(citation.get("paper_id", "")) not in scope_ids:
                result.failures.append("citation_out_of_scope")
                break
            if int(citation.get("physical_page", 0) or 0) < 1:
                result.failures.append("citation_page_invalid")
                break
        if scenario.selected_text:
            client = dict((run.context_snapshot or {}).get("client_context", {}) or {})
            if not client.get("selected_text"):
                result.failures.append("selection_missing_from_snapshot")
            if not result.selection_scope_locked:
                result.failures.append("selection_scope_not_locked")
            if result.selection_evidence_count < 1:
                result.failures.append("selection_evidence_missing")
            if any(
                int(item.get("physical_page", 0) or 0) != scenario.physical_page
                for item in citations
            ):
                result.failures.append("selection_expanded_outside_page")
        if (
            scenario.category == "degradation"
            and "embedding_contract_mismatch" not in result.vector_fallback_reasons
        ):
            result.failures.append("vector_fallback_reason_missing")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_scenarios(
    papers: list[dict[str, Any]],
    collection_id: str,
    page_samples: dict[str, list[tuple[int, str]]],
) -> list[list[LiveScenario]]:
    usable = [item for item in papers if str(item["id"]) in page_samples]
    if len(usable) < 2:
        raise RuntimeError("没有足够的真实页文本用于评测")
    groups: list[list[LiveScenario]] = []
    index = 1

    for offset in range(25):
        paper = usable[offset % len(usable)]
        samples = page_samples[str(paper["id"])]
        page, excerpt = samples[offset % len(samples)]
        scenario = LiveScenario(
            index=index,
            category="selection",
            title=f"[实测][选文] {index:03d} {str(paper['title'])[:50]}",
            session_type="paper",
            paper_id=str(paper["id"]),
            physical_page=page,
            selected_text=excerpt,
            question="请只解释本轮选中的原文，用中文说明其含义，不要扩展成全文总结。",
            expected_skills=("trace_original", "paper_qa"),
        )
        groups.append([scenario])
        index += 1

    for pair in range(10):
        paper = usable[pair % len(usable)]
        first = LiveScenario(
            index=index,
            category="multiturn",
            title=f"[实测][指代] {index:03d} {str(paper['title'])[:50]}",
            session_type="paper",
            paper_id=str(paper["id"]),
            question="请说明这篇论文如何处理蛋白质或研究对象的表示。",
            expected_skills=("paper_qa", "trace_original"),
            group=f"entity-{pair}",
        )
        index += 1
        follow = LiveScenario(
            index=index,
            category="multiturn",
            title=first.title,
            session_type="paper",
            paper_id=str(paper["id"]),
            question="那药物呢？",
            expected_skills=("paper_qa", "trace_original"),
            group=first.group,
        )
        index += 1
        groups.append([first, follow])

    qa_questions = (
        "这篇论文的核心研究问题是什么？",
        "请概括论文采用的主要方法，并给出页码引用。",
        "论文使用了哪些实验或评估设置？",
        "论文报告的主要结论是什么？",
        "论文明确提到了哪些局限？",
    )
    for offset in range(20):
        paper = usable[offset % len(usable)]
        groups.append(
            [
                LiveScenario(
                    index=index,
                    category="paper_qa",
                    title=f"[实测][单篇] {index:03d} {str(paper['title'])[:50]}",
                    session_type="paper",
                    paper_id=str(paper["id"]),
                    question=qa_questions[offset % len(qa_questions)],
                    expected_skills=("paper_qa", "trace_original", "summarize_paper"),
                )
            ]
        )
        index += 1

    compare_questions = (
        "比较集合中论文的研究问题和方法差异，并分别给出引用。",
        "这些论文有哪些共同假设与不同的实验设计？",
        "比较集合内论文的主要结论和局限。",
    )
    for offset in range(15):
        groups.append(
            [
                LiveScenario(
                    index=index,
                    category="collection",
                    title=f"[实测][跨文献] {index:03d}",
                    session_type="collection",
                    collection_id=collection_id,
                    question=compare_questions[offset % len(compare_questions)],
                    expected_skills=("compare_papers", "paper_qa"),
                )
            ]
        )
        index += 1

    discovery_pairs = (
        (
            "请根据当前集合主题联网推荐 5 篇尚未入库的相关论文。",
            "有没有更近的论文，如 2026 年的？",
            ("mcp__academic__search_openalex",),
            ("mcp__academic__search_openalex",),
            (),
            (),
        ),
        (
            "请只使用 OpenAlex 推荐五篇尚未入库的 2026 年相关论文。",
            "改用 Semantic Scholar",
            ("mcp__academic__search_openalex",),
            ("mcp__academic__search_semantic_scholar",),
            ("mcp__academic__search_semantic_scholar", "search_arxiv"),
            ("mcp__academic__search_openalex", "search_arxiv"),
        ),
        (
            "请只使用 OpenAlex 推荐 five papers，限定 2026 年。",
            "改成三篇",
            ("mcp__academic__search_openalex",),
            ("mcp__academic__search_openalex",),
            ("mcp__academic__search_semantic_scholar", "search_arxiv"),
            ("mcp__academic__search_semantic_scholar", "search_arxiv"),
        ),
        (
            "不要使用 OpenAlex，请用 Semantic Scholar 推荐五篇相关论文。",
            "换一批 3 篇，限定 2025 年。",
            ("mcp__academic__search_semantic_scholar",),
            ("mcp__academic__search_semantic_scholar",),
            ("mcp__academic__search_openalex",),
            ("mcp__academic__search_openalex",),
        ),
        (
            "请只使用 arXiv 推荐 5 篇相关论文。",
            "再推荐三篇 2026 年的。",
            ("search_arxiv",),
            ("search_arxiv",),
            (
                "mcp__academic__search_openalex",
                "mcp__academic__search_semantic_scholar",
            ),
            (
                "mcp__academic__search_openalex",
                "mcp__academic__search_semantic_scholar",
            ),
        ),
    )
    collection_library_titles = tuple(
        str(item.get("title") or "").strip()
        for item in papers
        if str(item.get("title") or "").strip()
    )
    for pair_index, (
        first_question,
        followup_question,
        first_expected_tools,
        followup_expected_tools,
        first_forbidden_tools,
        followup_forbidden_tools,
    ) in enumerate(discovery_pairs):
        # 默认的“联网推荐”要求 Harness 尝试 OpenAlex，但允许 Provider 超时后
        # 自动降级至 arXiv；显式指定数据源时才要求该来源成功或受控失败。
        first_required_success = () if pair_index == 0 else first_expected_tools
        followup_required_success = () if pair_index == 0 else followup_expected_tools
        group_id = f"discovery-followup-{pair_index}"
        first = LiveScenario(
            index=index,
            category="function_mcp",
            title=f"[实测][工具上下文] {index:03d}",
            session_type="collection",
            collection_id=collection_id,
            question=first_question,
            expected_skills=("find_related_papers",),
            web_enabled=True,
            require_citations=False,
            require_native_tools=True,
            expected_tools=first_required_success,
            expected_attempted_tools=first_expected_tools,
            forbidden_tools=first_forbidden_tools,
            group=group_id,
            library_titles=collection_library_titles,
        )
        index += 1
        followup = LiveScenario(
            index=index,
            category="function_mcp",
            title=first.title,
            session_type="collection",
            collection_id=collection_id,
            question=followup_question,
            expected_skills=("find_related_papers",),
            web_enabled=True,
            require_citations=False,
            require_native_tools=True,
            expected_tools=followup_required_success,
            expected_attempted_tools=followup_expected_tools,
            forbidden_tools=followup_forbidden_tools,
            group=group_id,
            library_titles=collection_library_titles,
        )
        index += 1
        groups.append([first, followup])

    for _offset in range(5):
        groups.append(
            [
                LiveScenario(
                    index=index,
                    category="memory_long_context",
                    title=f"[实测][记忆] {index:03d}",
                    session_type="collection",
                    collection_id=collection_id,
                    question="结合我的研究偏好，比较集合中的方法并指出最值得继续阅读的一篇。",
                    expected_skills=("compare_papers", "paper_qa"),
                )
            ]
        )
        index += 1

    for offset in range(5):
        paper = usable[offset % len(usable)]
        groups.append(
            [
                LiveScenario(
                    index=index,
                    category="degradation",
                    title=f"[实测][降级] {index:03d}",
                    session_type="paper",
                    paper_id=str(paper["id"]),
                    question=(
                        "请检索当前论文原文中英文词 model 附近的具体句子，并用中文解释；"
                        "若向量不可用，请使用关键词证据并明确说明证据边界。"
                        "回答仅限检索到的具体句子。"
                    ),
                    expected_skills=("paper_qa", "trace_original"),
                )
            ]
        )
        index += 1
    assert index == 101
    return groups


async def _create_test_memories(harness: LiveHarness) -> None:
    existing = (await harness.client.get("/api/v1/memories")).json()
    values = {str(item.get("value")) for item in existing.get("items", [])}
    for index in range(5):
        value = f"[实测] 我关注可解释科研 Agent、可靠引用与药物靶点建模（偏好 {index + 1}）"
        if value in values:
            continue
        response = await harness.client.post(
            "/api/v1/memories",
            headers=harness.headers(),
            json={"type": "research_interest", "value": value, "pinned": index == 0},
        )
        response.raise_for_status()


async def _set_one_paper_stale(paper_id: str) -> None:
    async with get_session_factory()() as session:
        paper = await session.get(Paper, paper_id, with_for_update=True)
        if paper is not None:
            paper.embedding_status = "stale"
            await session.commit()


async def _restore_paper_index(harness: LiveHarness, paper_id: str) -> None:
    response = await harness.client.post(
        "/api/v1/papers/bulk",
        headers=harness.headers(),
        json={"paper_ids": [paper_id], "action": "reindex"},
    )
    response.raise_for_status()
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        paper = (await harness.client.get(f"/api/v1/papers/{paper_id}")).json()
        if paper.get("status") in {"ready", "partial"} and paper.get(
            "embedding_status"
        ) == "ready":
            return
        if paper.get("status") == "failed":
            raise RuntimeError("降级验收后的重新索引失败")
        await asyncio.sleep(3)
    raise RuntimeError("等待降级验收论文恢复向量索引超时")


async def _probe_mcp_cache_disable() -> dict[str, Any]:
    """用真实 PostgreSQL、Redis 与 MCP 服务验证停用检查先于缓存读取。"""

    if not settings.mcp_enabled:
        return {"executed": False, "passed": None, "reason": "MCP_GLOBAL_DISABLED"}
    repository = SQLAlchemyRepository(settings.session_secret)
    runtime_store = create_runtime_store(settings)
    gateway = McpGateway(repository, runtime_store, settings)
    original_enabled = False
    try:
        server = await gateway.ensure_config()
        original_enabled = bool(server.enabled)
        if not original_enabled:
            return {"executed": False, "passed": None, "reason": "MCP_SERVER_DISABLED"}
        # 缓存键需要每次不同，但查询本身必须命中真实论文。MCP 服务会规范化连续
        # 空白，因此这里用随机空白生成唯一缓存键，同时始终查询稳定的 DeepDTA 主题。
        arguments = {
            "query": (
                "DeepDTA"
                + " " * (1 + secrets.randbelow(24))
                + "drug target"
                + " " * (1 + secrets.randbelow(24))
                + "binding affinity"
            ),
            "limit": 1,
        }
        first = await gateway.call("mcp__academic__search_openalex", arguments)
        second = await gateway.call("mcp__academic__search_openalex", arguments)
        await gateway.set_enabled(False)
        disabled_code: str | None = None
        try:
            await gateway.call("mcp__academic__search_openalex", arguments)
        except McpGatewayError as error:
            disabled_code = error.code
        passed = (
            first.get("cached") is False
            and second.get("cached") is True
            and first.get("available") is True
            and bool(first.get("results"))
            and disabled_code == "MCP_DISABLED"
        )
        return {
            "executed": True,
            "passed": passed,
            "first_cached": first.get("cached"),
            "second_cached": second.get("cached"),
            "provider_available": first.get("available"),
            "result_count": len(first.get("results", [])),
            "disabled_call_code": disabled_code,
        }
    except Exception as error:
        return {
            "executed": True,
            "passed": False,
            "reason": getattr(error, "code", error.__class__.__name__),
        }
    finally:
        if original_enabled:
            await gateway.set_enabled(True)
            try:
                await gateway.test()
            except Exception:
                pass
        await gateway.close()
        await runtime_store.close()


async def run_live(
    *,
    base_url: str,
    timeout_seconds: float,
    concurrency: int,
    run_limit: int,
    import_papers: bool,
    categories: set[str] | None = None,
) -> dict[str, Any]:
    harness = LiveHarness(
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
    )
    started = datetime.now(timezone.utc)
    results: list[LiveRunResult] = []
    try:
        await harness.login()
        collection_id, papers = await harness.ensure_collection_and_papers(
            import_papers=import_papers
        )
        await _create_test_memories(harness)
        samples = await harness.page_samples([str(item["id"]) for item in papers])
        groups = build_scenarios(papers, collection_id, samples)
        if categories:
            groups = [group for group in groups if group[0].category in categories]
        selected_groups: list[list[LiveScenario]] = []
        remaining = max(1, min(run_limit, 100))
        for group in groups:
            if remaining <= 0:
                break
            chosen = group[:remaining]
            selected_groups.append(chosen)
            remaining -= len(chosen)

        normal_groups = [
            group for group in selected_groups if group[0].category != "degradation"
        ]
        degradation_groups = [
            group for group in selected_groups if group[0].category == "degradation"
        ]
        semaphore = asyncio.Semaphore(harness.concurrency)

        async def run_group(group: list[LiveScenario]) -> list[LiveRunResult]:
            async with semaphore:
                session_id: str | None = None
                group_results: list[LiveRunResult] = []
                for scenario in group:
                    item = await harness.run_scenario(scenario, session_id=session_id)
                    session_id = item.session_id
                    group_results.append(item)
            if group and group[0].category == "function_mcp":
                _grade_recommendation_sequence(group_results)
            return group_results

        for batch_start in range(0, len(normal_groups), harness.concurrency):
            batch = normal_groups[batch_start : batch_start + harness.concurrency]
            batch_results = await asyncio.gather(*(run_group(group) for group in batch))
            results.extend(item for group in batch_results for item in group)

        if degradation_groups:
            for group in degradation_groups:
                paper_id = str(group[0].paper_id)
                await _set_one_paper_stale(paper_id)
                try:
                    results.extend(await run_group(group))
                finally:
                    await _restore_paper_index(harness, paper_id)
    finally:
        await harness.close()

    mcp_cache_disable_probe = await _probe_mcp_cache_disable()

    results.sort(key=lambda item: item.index)
    passed = sum(item.structural_pass for item in results)
    successful_native_tool_runs = sum(
        item.native_function_calling_attempted
        and any(call.get("status") == "succeeded" for call in item.tool_calls)
        for item in results
    )
    function_cases = sum(item.category == "function_mcp" for item in results)
    provider = "DeepSeek/OpenAI-compatible + Ollama embedding"
    return {
        "mode": "live",
        "evidence_level": "real_model_real_infrastructure",
        "provider": provider,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "collection_name": COLLECTION_NAME,
        "case_count": len(results),
        "structural": {
            "passed": passed,
            "total": len(results),
            "rate": round(passed / len(results), 6) if results else None,
            "target": "至少 99/100（仅完整 100 次运行适用）",
        },
        "suite_guards": {
            "successful_native_tool_runs": successful_native_tool_runs,
            "native_tool_persistence_satisfied": (
                successful_native_tool_runs >= 1 if function_cases else None
            ),
            "mcp_cache_disable_probe": mcp_cache_disable_probe,
        },
        "category_counts": {
            category: sum(item.category == category for item in results)
            for category in sorted({item.category for item in results})
        },
        "results": [asdict(item) for item in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperLeaf Harness 真实模型评测")
    parser.add_argument(
        "--base-url",
        default=os.getenv("PAPERLEAF_LIVE_API_URL", "http://api:8000"),
    )
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument(
        "--categories",
        help="逗号分隔的场景类型；为空时执行完整矩阵",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(
        run_live(
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
            concurrency=args.concurrency,
            run_limit=args.runs,
            import_papers=not args.skip_import,
            categories=(
                {item.strip() for item in args.categories.split(",") if item.strip()}
                if args.categories
                else None
            ),
        )
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "evidence_level": report["evidence_level"],
                "case_count": report["case_count"],
                "structural": report["structural"],
                "output": str(args.output) if args.output else None,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
