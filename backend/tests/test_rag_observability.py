from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from paperleaf_api.rag.citations import CitationClaim, Evidence
from paperleaf_api.rag_observability import (
    aggregate_rag_runs,
    build_rag_trace,
    classify_intent,
)


def test_intent_classification_uses_stable_content_free_enums() -> None:
    assert (
        classify_intent(
            "这篇论文讲了什么", scope="paper", selected_paper_count=1, web_enabled=False
        )
        == "paper_overview"
    )
    assert (
        classify_intent(
            "比较两种方法的实验结果",
            scope="collection",
            selected_paper_count=3,
            web_enabled=False,
        )
        == "comparison"
    )
    assert (
        classify_intent("模型有哪些局限", scope="paper", selected_paper_count=1, web_enabled=False)
        == "limitation"
    )


def test_trace_records_channels_latency_and_strategy_without_content() -> None:
    evidence = Evidence(
        "paper:p2:c0",
        "paper",
        "不会写入指标的标题",
        2,
        "不会写入指标的证据正文",
        retrieval_channels=("keyword", "vector"),
        chunking_strategy="structure_aware_v2",
    )
    trace = build_rag_trace(
        intent="method",
        scope="paper",
        result={
            "retrieved_evidence": [evidence],
            "citations": [CitationClaim(evidence.chunk_id, evidence.paper_id, 2)],
            "evidence_quality": {"grade": "sufficient", "reason_code": "hybrid_support"},
            "stage_timings_ms": {"retrieval": 125, "generation": 860},
        },
        outcome="cited_answer",
    )

    assert trace["retrieval_channels"] == ["keyword", "vector"]
    assert trace["chunking_strategies"] == ["structure_aware_v2"]
    assert trace["stage_timings_ms"] == {"retrieval": 125, "generation": 860}
    assert trace["citation_count"] == 1
    serialized = str(trace)
    assert "不会写入指标的标题" not in serialized
    assert "不会写入指标的证据正文" not in serialized
    assert "paper:p2:c0" not in serialized


def test_admin_aggregation_exposes_funnel_failures_channels_and_intents() -> None:
    now = datetime.now(timezone.utc)
    successful_trace = {
        "version": 1,
        "intent": "method",
        "scope": "paper",
        "outcome": "cited_answer",
        "retrieval_outcome": "sufficient",
        "retrieval_channels": ["keyword", "vector"],
        "evidence_count": 4,
        "evidence_grade": "sufficient",
        "evidence_reason_code": "hybrid_support",
        "citation_count": 2,
        "stage_timings_ms": {"retrieval": 100, "generation": 900},
        "chunking_strategies": ["structure_aware_v2"],
        "failure_category": "none",
    }
    failed_trace = {
        "version": 1,
        "intent": "comparison",
        "scope": "collection",
        "outcome": "unverified_answer",
        "retrieval_outcome": "insufficient",
        "retrieval_channels": ["keyword_rewrite"],
        "evidence_count": 2,
        "evidence_grade": "insufficient",
        "evidence_reason_code": "answer_not_supported",
        "citation_count": 0,
        "stage_timings_ms": {"retrieval": 300, "generation": 1500},
        "chunking_strategies": ["fixed_window_v1"],
        "failure_category": "unverified_answer",
    }
    runs = [
        SimpleNamespace(
            status="completed",
            duration_ms=1200,
            error_code=None,
            created_at=now,
            result_summary={"rag_trace": successful_trace},
        ),
        SimpleNamespace(
            status="failed",
            duration_ms=2200,
            error_code="UNVERIFIED_ANSWER",
            created_at=now - timedelta(minutes=1),
            result_summary={"rag_trace": failed_trace},
        ),
    ]

    report = aggregate_rag_runs(runs, window_hours=24)

    assert report["totals"]["runs"] == 2
    assert report["totals"]["failure_rate"] == 0.5
    assert report["totals"]["cited_answer_rate"] == 0.5
    assert report["totals"]["grounded_answers"] == 1
    assert report["totals"]["rag_issue_rate"] == 0.5
    assert [step["count"] for step in report["funnel"]] == [2, 2, 1, 1]
    assert report["latency"]["overall"] == {"samples": 2, "p50_ms": 1200, "p95_ms": 2200}
    assert {item["channel"] for item in report["retrieval_channels"]} == {
        "keyword",
        "vector",
        "keyword_rewrite",
    }
    assert report["failures"][0]["category"] == "unverified_answer"
    assert {item["intent"] for item in report["intents"]} == {"method", "comparison"}
