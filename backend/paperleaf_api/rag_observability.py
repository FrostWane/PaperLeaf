"""RAG 运行轨迹、低基数 Prometheus 指标与管理员聚合。

这里刻意只处理稳定枚举、数量和耗时。问题文本、证据正文、用户/论文/运行 ID
都不得写入指标标签或管理员聚合响应。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from prometheus_client import Counter as PromCounter
from prometheus_client import Histogram

RAG_TRACE_VERSION = 1
RAG_METRIC_STAGES = (
    "intent",
    "retrieval",
    "evidence_grading",
    "generation",
    "answer_support",
    "citation_validation",
)
KNOWN_CHANNELS = {
    "keyword",
    "keyword_rewrite",
    "vector",
    "sentence_reranker",
    "scoped_overview",
    "page_text",
    "verified_selection",
    "selection_neighbor",
    "multigranular_reranker",
    "legacy_sentence_reranker",
    "demo",
}
KNOWN_SCOPES = {"paper", "selection", "collection", "library"}
KNOWN_PROCESSORS = {
    "per_paper_balance",
    "weak_query_rewrite",
    "sentence_window_rerank",
    "multigranular_page_rerank",
    "legacy_sentence_window_rerank",
    "page_chunk_resolution",
    "paper_subquery_merge_1_1_1_plus_2",
}
KNOWN_REWRITE_REASONS = {
    "no_candidates",
    "low_lexical_coverage",
    "ambiguous_ranking",
    "cross_language",
    "broad_or_comparison_intent",
}
KNOWN_RERANKER_FALLBACK_REASONS = {"reranker_unavailable"}
KNOWN_INTENTS = {
    "paper_overview",
    "comparison",
    "method",
    "experiment_result",
    "limitation",
    "literature_discovery",
    "fact_lookup",
}

INTENT_LABELS = {
    "paper_overview": "论文概览",
    "comparison": "比较分析",
    "method": "方法与实现",
    "experiment_result": "实验与结果",
    "limitation": "局限与展望",
    "literature_discovery": "文献发现",
    "fact_lookup": "事实查询",
    "unknown": "未识别",
}
CHANNEL_LABELS = {
    "keyword": "关键词检索",
    "keyword_rewrite": "改写后关键词检索",
    "vector": "向量检索",
    "sentence_reranker": "短句窗重排",
    "scoped_overview": "单篇跨页概览",
    "page_text": "指定页原文",
    "verified_selection": "已核对选文",
    "selection_neighbor": "选文相邻证据",
    "multigranular_reranker": "页级多粒度重排",
    "legacy_sentence_reranker": "旧短句窗重排",
    "demo": "演示检索",
    "none": "未命中通道",
    "other": "其他通道",
}
PROCESSOR_LABELS = {
    "per_paper_balance": "逐论文取证",
    "weak_query_rewrite": "弱结果补充查询",
    "sentence_window_rerank": "短句窗重排",
    "multigranular_page_rerank": "页级多粒度重排",
    "legacy_sentence_window_rerank": "旧短句窗重排",
    "page_chunk_resolution": "页文本映射真实 Chunk",
    "paper_subquery_merge_1_1_1_plus_2": "论文子问题独立取证与 1+1+1+2 合并",
    "other": "其他处理",
}
REWRITE_REASON_LABELS = {
    "no_candidates": "初次未召回",
    "low_lexical_coverage": "关键词覆盖较低",
    "ambiguous_ranking": "候选分差较小",
    "cross_language": "中英文跨语言",
    "broad_or_comparison_intent": "宽泛或比较问题",
    "other": "其他原因",
}
RERANKER_FALLBACK_LABELS = {
    "reranker_unavailable": "重排器不可用",
    "other": "其他原因",
}
FAILURE_LABELS = {
    "no_evidence": "没有召回证据",
    "insufficient_evidence": "证据质量不足",
    "unverified_answer": "回答引用未通过",
    "model_timeout": "模型响应超时",
    "model_unavailable": "模型不可用",
    "scope_violation": "证据超出权限范围",
    "cancelled": "用户取消",
    "internal": "运行异常",
    "none": "无失败",
}

_OVERVIEW_RE = re.compile(
    r"讲了?什么|主要内容|总结|概括|概览|介绍一下|研究内容|"
    r"\b(?:summari[sz]e|overview|what\s+is\s+(?:this|the)\s+(?:paper|article)\s+about)\b",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(
    r"比较|区别|差异|优劣|对比|\b(?:compare|comparison|versus|vs\.?|difference)\b", re.IGNORECASE
)
_METHOD_RE = re.compile(
    r"方法|模型|架构|算法|实现|训练|损失函数|\b(?:method|model|architecture|algorithm|implementation|training)\b",
    re.IGNORECASE,
)
_RESULT_RE = re.compile(
    r"实验|结果|性能|指标|数据集|消融|\b(?:experiment|result|performance|metric|dataset|ablation)\b",
    re.IGNORECASE,
)
_LIMITATION_RE = re.compile(
    r"局限|不足|限制|未来工作|适用范围|\b(?:limitation|weakness|future\s+work)\b", re.IGNORECASE
)
_DISCOVERY_RE = re.compile(
    r"找文献|搜索论文|相关论文|arxiv|\b(?:find|search)\s+(?:papers?|literature)\b", re.IGNORECASE
)


AGENT_RUNS = PromCounter(
    "paperleaf_agent_runs_total",
    "PaperLeaf Agent 终态运行次数",
    ("status", "outcome", "failure_category", "intent", "scope"),
)
AGENT_DURATION = Histogram(
    "paperleaf_agent_run_duration_seconds",
    "PaperLeaf Agent 端到端运行耗时",
    ("outcome", "intent", "scope"),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 60, 90, 120, 180, 240),
)
RAG_STAGE_DURATION = Histogram(
    "paperleaf_rag_stage_duration_seconds",
    "PaperLeaf RAG 各阶段耗时",
    ("stage", "intent", "scope"),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 60, 120),
)
RAG_RETRIEVALS = PromCounter(
    "paperleaf_rag_retrieval_channel_total",
    "PaperLeaf RAG 召回通道参与次数",
    ("channel", "retrieval_outcome", "intent", "scope"),
)
RAG_EVIDENCE_COUNT = Histogram(
    "paperleaf_rag_evidence_count",
    "PaperLeaf RAG 每次运行召回证据数量",
    ("retrieval_outcome", "intent", "scope"),
    buckets=(0, 1, 2, 3, 5, 8, 12, 20),
)


def classify_intent(query: str, *, scope: str, selected_paper_count: int, web_enabled: bool) -> str:
    """不增加模型调用的稳定意图分类；只保存枚举，不保存原问题。"""

    if web_enabled and _DISCOVERY_RE.search(query):
        return "literature_discovery"
    if scope == "paper" and selected_paper_count == 1 and _OVERVIEW_RE.search(query):
        return "paper_overview"
    if _COMPARISON_RE.search(query):
        return "comparison"
    if _LIMITATION_RE.search(query):
        return "limitation"
    if _RESULT_RE.search(query):
        return "experiment_result"
    if _METHOD_RE.search(query):
        return "method"
    return "fact_lookup"


def failure_category(error_code: str | None, trace: dict[str, Any] | None = None) -> str:
    code = (error_code or "").upper()
    if code == "UNVERIFIED_ANSWER":
        return "unverified_answer"
    if code == "EVIDENCE_SCOPE_VIOLATION":
        return "scope_violation"
    if "TIMEOUT" in code:
        return "model_timeout"
    if code.startswith("MODEL_"):
        return "model_unavailable"
    if code in {"CANCELLED", "AGENT_CANCELLED"}:
        return "cancelled"
    if code:
        return "internal"
    if trace:
        retrieval_outcome = trace.get("retrieval_outcome")
        if retrieval_outcome == "empty":
            return "no_evidence"
        if retrieval_outcome == "insufficient":
            return "insufficient_evidence"
    return "none"


def build_rag_trace(
    *,
    intent: str,
    scope: str,
    result: dict[str, Any] | None,
    stage_timings_ms: dict[str, int] | None = None,
    outcome: str,
    error_code: str | None = None,
) -> dict[str, Any]:
    result = result or {}
    evidence = list(result.get("retrieved_evidence", []))
    quality = dict(result.get("evidence_quality", {}))
    timings = {
        key: max(0, int(value))
        for key, value in (stage_timings_ms or result.get("stage_timings_ms", {}) or {}).items()
        if key in RAG_METRIC_STAGES and isinstance(value, (int, float))  # noqa: UP038
    }
    channels = sorted(
        {
            channel if channel in KNOWN_CHANNELS else "other"
            for item in evidence
            for channel in getattr(item, "retrieval_channels", ())
        }
    )
    strategies = sorted(
        {
            str(getattr(item, "chunking_strategy", "unknown"))[:48]
            for item in evidence
            if getattr(item, "chunking_strategy", None)
        }
    )
    vector_fallback_reasons = sorted(
        {
            str(getattr(item, "vector_fallback_reason", ""))[:64]
            for item in evidence
            if getattr(item, "vector_fallback_reason", None)
        }
    )
    processors = sorted(
        {
            processor if processor in KNOWN_PROCESSORS else "other"
            for item in evidence
            for processor in getattr(item, "retrieval_processors", ())
        }
    )
    rewrite_reasons = sorted(
        {
            reason if reason in KNOWN_REWRITE_REASONS else "other"
            for item in evidence
            for reason in getattr(item, "query_rewrite_reasons", ())
        }
    )
    reranker_fallback_reasons = sorted(
        {
            reason if reason in KNOWN_RERANKER_FALLBACK_REASONS else "other"
            for item in evidence
            if (reason := getattr(item, "reranker_fallback_reason", None))
        }
    )
    retrieval_config = dict(result.get("retrieval_config", {}) or {})
    grade = str(quality.get("grade") or result.get("evidence_grade") or "unknown")
    retrieval_outcome = (
        "empty" if not evidence else "sufficient" if grade == "sufficient" else "insufficient"
    )
    trace = {
        "version": RAG_TRACE_VERSION,
        "intent": intent if intent in KNOWN_INTENTS else "unknown",
        "scope": scope if scope in KNOWN_SCOPES else "library",
        "outcome": outcome,
        "retrieval_outcome": retrieval_outcome,
        "retrieval_channels": channels or ["none"],
        "evidence_count": len(evidence),
        "evidence_grade": grade,
        "evidence_reason_code": str(quality.get("reason_code", "unknown"))[:80],
        "citation_count": len(result.get("citations", [])),
        "stage_timings_ms": timings,
        "chunking_strategies": strategies or ["unknown"],
        "vector_fallback_reasons": vector_fallback_reasons,
        "retrieval_processors": processors,
        "query_rewrite_reasons": rewrite_reasons,
        "reranker_fallback_reasons": reranker_fallback_reasons,
        "retrieval_config_fingerprint": str(retrieval_config.get("fingerprint", ""))[:64]
        or None,
        "git_sha": str(retrieval_config.get("git_sha", "unknown"))[:40],
        "git_sha_verified": bool(retrieval_config.get("git_sha_verified", False)),
    }
    trace["failure_category"] = failure_category(error_code, trace)
    return trace


def record_rag_run(trace: dict[str, Any], *, status: str, duration_ms: int | None) -> None:
    """把一条终态轨迹写入进程内 Prometheus；所有标签均为有界枚举。"""

    intent = str(trace.get("intent", "unknown"))
    scope = str(trace.get("scope", "library"))
    outcome = str(trace.get("outcome", "unknown"))[:40]
    failure = str(trace.get("failure_category", "none"))
    AGENT_RUNS.labels(status[:24], outcome, failure, intent, scope).inc()
    if duration_ms is not None:
        AGENT_DURATION.labels(outcome, intent, scope).observe(max(0, duration_ms) / 1000)
    for stage, value in dict(trace.get("stage_timings_ms", {})).items():
        if stage in RAG_METRIC_STAGES:
            RAG_STAGE_DURATION.labels(stage, intent, scope).observe(max(0, float(value)) / 1000)
    retrieval_outcome = str(trace.get("retrieval_outcome", "unknown"))
    for channel in trace.get("retrieval_channels", ["none"]):
        normalized = channel if channel in KNOWN_CHANNELS | {"none", "other"} else "other"
        RAG_RETRIEVALS.labels(normalized, retrieval_outcome, intent, scope).inc()
    RAG_EVIDENCE_COUNT.labels(retrieval_outcome, intent, scope).observe(
        max(0, int(trace.get("evidence_count", 0)))
    )


def _percentile(values: Iterable[int | float], percentile: float) -> int | None:
    ordered = sorted(max(0, float(value)) for value in values)
    if not ordered:
        return None
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[rank])


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def aggregate_rag_runs(
    runs: Iterable[Any], *, window_hours: int, limit_reached: bool = False
) -> dict[str, Any]:
    records = list(runs)
    traces: list[tuple[Any, dict[str, Any]]] = []
    for run in records:
        summary = getattr(run, "result_summary", None) or {}
        trace = summary.get("rag_trace")
        if isinstance(trace, dict) and trace.get("version") == RAG_TRACE_VERSION:
            traces.append((run, trace))

    terminal = [
        run
        for run in records
        if getattr(run, "status", "") in {"completed", "failed", "cancelled", "interrupted"}
    ]
    completed = sum(getattr(run, "status", "") == "completed" for run in terminal)
    failed = sum(getattr(run, "status", "") == "failed" for run in terminal)
    cited = sum(int(trace.get("citation_count", 0)) > 0 for _, trace in traces)
    sufficient = sum(trace.get("retrieval_outcome") == "sufficient" for _, trace in traces)
    retrieved = sum(int(trace.get("evidence_count", 0)) > 0 for _, trace in traces)
    grounded = sum(
        trace.get("retrieval_outcome") == "sufficient" and int(trace.get("citation_count", 0)) > 0
        for _, trace in traces
    )
    rag_issue_runs = sum(
        str(trace.get("failure_category", "none")) != "none" for _, trace in traces
    )

    overall_durations = [getattr(run, "duration_ms", None) for run in terminal]
    overall_durations = [
        value
        for value in overall_durations
        if isinstance(value, (int, float))  # noqa: UP038
    ]
    stage_values: dict[str, list[int]] = defaultdict(list)
    for _, trace in traces:
        for stage, value in dict(trace.get("stage_timings_ms", {})).items():
            if stage in RAG_METRIC_STAGES and isinstance(  # noqa: UP038
                value, (int, float)
            ):
                stage_values[stage].append(int(value))

    channel_stats: dict[str, dict[str, Any]] = {}
    for channel in sorted(KNOWN_CHANNELS | {"none", "other"}):
        selected = [
            (run, trace) for run, trace in traces if channel in trace.get("retrieval_channels", [])
        ]
        if not selected:
            continue
        successes = sum(int(trace.get("citation_count", 0)) > 0 for _, trace in selected)
        adequate = sum(trace.get("retrieval_outcome") == "sufficient" for _, trace in selected)
        retrieval_times = [
            trace.get("stage_timings_ms", {}).get("retrieval")
            for _, trace in selected
            if isinstance(  # noqa: UP038
                trace.get("stage_timings_ms", {}).get("retrieval"), (int, float)
            )
        ]
        channel_stats[channel] = {
            "channel": channel,
            "label": CHANNEL_LABELS.get(channel, channel),
            "runs": len(selected),
            "cited_answer_rate": _rate(successes, len(selected)),
            "sufficient_evidence_rate": _rate(adequate, len(selected)),
            "retrieval_p95_ms": _percentile(retrieval_times, 0.95),
        }

    intent_stats: list[dict[str, Any]] = []
    for intent in sorted(KNOWN_INTENTS | {"unknown"}):
        selected = [(run, trace) for run, trace in traces if trace.get("intent") == intent]
        if not selected:
            continue
        successes = sum(int(trace.get("citation_count", 0)) > 0 for _, trace in selected)
        adequate = sum(trace.get("retrieval_outcome") == "sufficient" for _, trace in selected)
        durations = [getattr(run, "duration_ms", None) for run, _ in selected]
        durations = [
            value
            for value in durations
            if isinstance(value, (int, float))  # noqa: UP038
        ]
        intent_stats.append(
            {
                "intent": intent,
                "label": INTENT_LABELS.get(intent, intent),
                "runs": len(selected),
                "cited_answer_rate": _rate(successes, len(selected)),
                "sufficient_evidence_rate": _rate(adequate, len(selected)),
                "p95_ms": _percentile(durations, 0.95),
            }
        )
    intent_stats.sort(key=lambda item: (-item["runs"], item["intent"]))

    failures = Counter(
        str(trace.get("failure_category", "none"))
        for _, trace in traces
        if trace.get("failure_category") != "none"
    )
    # 兼容升级前没有 rag_trace 的失败记录。
    for run in terminal:
        summary = getattr(run, "result_summary", None) or {}
        if isinstance(summary.get("rag_trace"), dict) or getattr(run, "status", "") != "failed":
            continue
        failures[failure_category(getattr(run, "error_code", None))] += 1

    strategy_counts = Counter(
        strategy
        for _, trace in traces
        for strategy in set(trace.get("chunking_strategies", ["unknown"]))
    )
    processor_counts = Counter(
        processor
        for _, trace in traces
        for processor in set(trace.get("retrieval_processors", []))
        if processor in KNOWN_PROCESSORS | {"other"}
    )
    rewrite_reason_counts = Counter(
        reason
        for _, trace in traces
        for reason in set(trace.get("query_rewrite_reasons", []))
        if reason in KNOWN_REWRITE_REASONS | {"other"}
    )
    reranker_fallback_counts = Counter(
        reason
        for _, trace in traces
        for reason in set(trace.get("reranker_fallback_reasons", []))
        if reason in KNOWN_RERANKER_FALLBACK_REASONS | {"other"}
    )
    stage_latency = [
        {
            "stage": stage,
            "samples": len(stage_values.get(stage, [])),
            "p50_ms": _percentile(stage_values.get(stage, []), 0.5),
            "p95_ms": _percentile(stage_values.get(stage, []), 0.95),
        }
        for stage in RAG_METRIC_STAGES
    ]
    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "window_hours": window_hours,
        "generated_at": generated_at,
        "limit_reached": limit_reached,
        "totals": {
            "runs": len(records),
            "terminal_runs": len(terminal),
            "completed_runs": completed,
            "failed_runs": failed,
            "cited_answers": cited,
            "grounded_answers": grounded,
            "rag_issue_runs": rag_issue_runs,
            "telemetry_runs": len(traces),
            "telemetry_coverage": _rate(len(traces), len(terminal)),
            "completion_rate": _rate(completed, len(terminal)),
            "failure_rate": _rate(failed, len(terminal)),
            "cited_answer_rate": _rate(cited, len(traces)),
            "rag_issue_rate": _rate(rag_issue_runs, len(traces)),
        },
        "funnel": [
            {
                "key": "observed",
                "label": "已采集运行",
                "count": len(traces),
                "rate": 1.0 if traces else 0.0,
            },
            {
                "key": "retrieved",
                "label": "召回证据",
                "count": retrieved,
                "rate": _rate(retrieved, len(traces)),
            },
            {
                "key": "sufficient",
                "label": "证据充足",
                "count": sufficient,
                "rate": _rate(sufficient, len(traces)),
            },
            {
                "key": "cited",
                "label": "充分证据引用",
                "count": grounded,
                "rate": _rate(grounded, len(traces)),
            },
        ],
        "latency": {
            "overall": {
                "samples": len(overall_durations),
                "p50_ms": _percentile(overall_durations, 0.5),
                "p95_ms": _percentile(overall_durations, 0.95),
            },
            "stages": stage_latency,
        },
        "retrieval_channels": sorted(
            channel_stats.values(), key=lambda item: (-item["runs"], item["channel"])
        ),
        "intents": intent_stats,
        "failures": [
            {
                "category": category,
                "label": FAILURE_LABELS.get(category, category),
                "count": count,
                "rate": _rate(count, len(terminal)),
            }
            for category, count in failures.most_common()
        ],
        "chunking_strategies": [
            {"strategy": strategy, "runs": count}
            for strategy, count in strategy_counts.most_common()
        ],
        "retrieval_processors": [
            {
                "processor": processor,
                "label": PROCESSOR_LABELS.get(processor, processor),
                "runs": count,
            }
            for processor, count in sorted(
                processor_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "query_rewrite_reasons": [
            {
                "reason": reason,
                "label": REWRITE_REASON_LABELS.get(reason, reason),
                "runs": count,
            }
            for reason, count in sorted(
                rewrite_reason_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "reranker_fallback_reasons": [
            {
                "reason": reason,
                "label": RERANKER_FALLBACK_LABELS.get(reason, reason),
                "runs": count,
            }
            for reason, count in sorted(
                reranker_fallback_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }
