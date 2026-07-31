"""LangGraph 状态类型。"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from ..rag.citations import CitationClaim, Evidence


class AgentState(TypedDict, total=False):
    run_id: str
    session_id: str
    user_id: str
    query: str
    messages: list[dict[str, Any]]
    message_ids: list[str]
    intent: str
    scope: Literal["paper", "selection", "library"]
    selected_paper_ids: list[str]
    web_enabled: bool
    retrieved_evidence: list[Evidence]
    arxiv_candidates: list[dict[str, Any]]
    evidence_grade: Literal["sufficient", "insufficient"]
    evidence_quality: dict[str, Any]
    tool_steps: int
    pending_action: dict[str, Any] | None
    citations: list[CitationClaim]
    answer: str
    error: str | None
    status: Literal["pending", "running", "interrupted", "completed", "failed", "cancelled"]
