"""LangGraph 状态类型。"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from ..rag.citations import CitationClaim, Evidence


class AgentState(TypedDict, total=False):
    run_id: str
    session_id: str
    user_id: str
    query: str
    original_query: str
    messages: list[dict[str, Any]]
    message_ids: list[str]
    intent: str
    stage_timings_ms: dict[str, int]
    scope: Literal["paper", "selection", "collection", "library"]
    selected_paper_ids: list[str]
    scope_paper_titles: list[str]
    scope_paper_texts: list[str]
    excluded_recommendation_entities: list[str]
    provider_policy: dict[str, Any]
    retrieval_config: dict[str, Any]
    provider_fallback_reason: str | None
    web_enabled: bool
    client_context: dict[str, Any]
    resolved_query: str
    resolved_references: dict[str, Any]
    reference_confidence: float
    context_snapshot: dict[str, Any]
    context_budget: dict[str, int]
    memory_ids: list[str]
    selected_skill: str
    skill_version: int
    skill_instructions: str
    skill_route_source: str
    skill_route_confidence: float
    tool_calls: list[dict[str, Any]]
    tool_context_entries: list[dict[str, Any]]
    tool_mode_active: bool
    pre_retrieved_evidence: list[Evidence]
    selection_evidence: list[Evidence]
    selection_scope_locked: bool
    selection_physical_page: int | None
    selection_paper_id: str | None
    pre_arxiv_candidates: list[dict[str, Any]]
    clarification_question: str | None
    clarification_requested: bool
    retrieved_evidence: list[Evidence]
    arxiv_candidates: list[dict[str, Any]]
    evidence_grade: Literal["sufficient", "insufficient"]
    evidence_quality: dict[str, Any]
    answerability_status: Literal["answerable", "unanswerable", "not_checked"]
    answerability_confidence: float | None
    answerability_reason: str | None
    tool_steps: int
    pending_action: dict[str, Any] | None
    citations: list[CitationClaim]
    citation_validation_passed: bool
    answer_repair_attempted: bool
    answer_repair_succeeded: bool
    support_repair_attempted: bool
    support_repair_succeeded: bool
    context_usage: dict[str, Any]
    external_metadata_answer: bool
    displayed_recommendations: list[dict[str, Any]]
    displayed_recommendation_entities: list[str]
    answer: str
    error: str | None
    status: Literal["pending", "running", "interrupted", "completed", "failed", "cancelled"]
