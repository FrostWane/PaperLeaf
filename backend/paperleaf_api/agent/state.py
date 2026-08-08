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
    clarification_question: str | None
    clarification_requested: bool
    retrieved_evidence: list[Evidence]
    arxiv_candidates: list[dict[str, Any]]
    evidence_grade: Literal["sufficient", "insufficient"]
    evidence_quality: dict[str, Any]
    tool_steps: int
    pending_action: dict[str, Any] | None
    citations: list[CitationClaim]
    citation_validation_passed: bool
    answer: str
    error: str | None
    status: Literal["pending", "running", "interrupted", "completed", "failed", "cancelled"]
