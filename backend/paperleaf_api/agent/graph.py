"""LangGraph 编排与无可选依赖时的确定性兼容运行器。"""

from __future__ import annotations

import inspect
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Union

from ..config import settings
from ..rag.citations import CitationClaim, Evidence, validate_citations
from .state import AgentState
from .tools import (
    ArxivSearch,
    ArxivSearchInput,
    EmptyLibrarySearch,
    LibrarySearchInput,
    SearchArxivTool,
    SearchLibraryTool,
)

AnswererResult = Union[
    Awaitable[tuple[str, list[CitationClaim]]], tuple[str, list[CitationClaim]]
]
Answerer = Callable[[str, list[Evidence]], AnswererResult]


async def _default_answerer(
    query: str, evidence: list[Evidence]
) -> tuple[str, list[CitationClaim]]:
    source = evidence[0]
    return (
        f"根据已检索文献，第 {source.physical_page} 页的证据表明：{source.text}",
        [
            CitationClaim(
                chunk_id=source.chunk_id,
                paper_id=source.paper_id,
                physical_page=source.physical_page,
                excerpt=source.text,
            )
        ],
    )


async def _configured_answerer(
    query: str, evidence: list[Evidence]
) -> tuple[str, list[CitationClaim]]:
    """OpenAI-compatible 回答器；无 Key 时使用确定性降级。"""
    if not settings.openai_api_key:
        return await _default_answerer(query, evidence)
    from langchain_openai import ChatOpenAI

    context = "\n\n".join(
        f"[chunk:{item.chunk_id}｜论文:{item.paper_title}｜物理页:{item.physical_page}]\n{item.text}"
        for item in evidence
    )
    model = ChatOpenAI(
        model=settings.chat_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=0,
        timeout=30,
        max_retries=2,
    )
    response = await model.ainvoke(
        "你是 PaperLeaf 文献问答助手。只能依据给定证据回答；每个事实后必须写"
        " `[chunk:完整块ID]`，不得编造块 ID 或页码。证据不足就明确说无法回答。\n\n"
        f"问题：{query}\n\n证据：\n{context}"
    )
    answer = str(response.content)
    evidence_by_id = {item.chunk_id: item for item in evidence}
    citation_ids = list(dict.fromkeys(re.findall(r"\[chunk:([^\]]+)\]", answer)))
    citations = [
        CitationClaim(
            chunk_id=chunk_id,
            paper_id=evidence_by_id[chunk_id].paper_id,
            physical_page=evidence_by_id[chunk_id].physical_page,
        )
        for chunk_id in citation_ids
        if chunk_id in evidence_by_id
    ]
    return answer, citations


class AgentRuntime:
    def __init__(
        self,
        retriever: SearchLibraryTool,
        answerer: Answerer,
        arxiv_search: SearchArxivTool,
        *,
        use_native_interrupt: bool,
    ) -> None:
        self.retriever = retriever
        self.answerer = answerer
        self.arxiv_search = arxiv_search
        self.use_native_interrupt = use_native_interrupt

    async def validate_request(self, state: AgentState) -> AgentState:
        query = str(state.get("query", "")).strip()
        user_id = str(state.get("user_id", "")).strip()
        if not query or not user_id:
            return {"status": "failed", "error": "缺少用户或问题"}
        return {"status": "running", "error": None, "tool_steps": state.get("tool_steps", 0)}

    async def retrieve_library(self, state: AgentState) -> AgentState:
        if state.get("status") == "failed":
            return {}
        evidence = await self.retriever(
            LibrarySearchInput(
                user_id=state["user_id"],
                query=state["query"],
                paper_ids=state.get("selected_paper_ids", []),
            )
        )
        return {"retrieved_evidence": evidence, "tool_steps": state.get("tool_steps", 0) + 1}

    async def grade_evidence(self, state: AgentState) -> AgentState:
        grade = "sufficient" if state.get("retrieved_evidence") else "insufficient"
        return {"evidence_grade": grade}

    async def generate_answer(self, state: AgentState) -> AgentState:
        result = self.answerer(state["query"], state.get("retrieved_evidence", []))
        answer, citations = await result if inspect.isawaitable(result) else result
        return {"answer": answer, "citations": citations}

    async def abstain(self, state: AgentState) -> AgentState:
        return {
            "answer": "当前文献库中没有足够证据回答这个问题。你可以调整问题或允许搜索 arXiv。",
            "citations": [],
            "status": "completed",
        }

    async def search_arxiv(self, state: AgentState) -> AgentState:
        result = await self.arxiv_search(ArxivSearchInput(query=state["query"], limit=5))
        return {
            "arxiv_candidates": result.data,
            "tool_steps": state.get("tool_steps", 0) + 1,
        }

    async def propose_import(self, state: AgentState) -> AgentState:
        candidates = state.get("arxiv_candidates", [])
        if not candidates:
            return await self.abstain(state)
        pending = {
            "action_id": str(uuid.uuid4()),
            "type": "confirm_arxiv_import",
            "candidates": candidates,
            "risk_message": "导入会下载并解析所选 arXiv PDF，需要你的明确确认。",
            "allowed_decisions": ["approve", "reject"],
        }
        if not self.use_native_interrupt:
            return {"pending_action": pending, "status": "interrupted"}
        try:
            from langgraph.types import interrupt

            decision = interrupt(pending)
        except ImportError:
            return {"pending_action": pending, "status": "interrupted"}
        if decision == "approve":
            return {
                "pending_action": None,
                "status": "completed",
                "answer": "已批准候选文献导入，请由受控导入接口创建任务。",
            }
        return {"pending_action": None, "status": "completed", "answer": "已取消导入。"}

    async def validate_answer_citations(self, state: AgentState) -> AgentState:
        valid, errors = validate_citations(
            state.get("citations", []), state.get("retrieved_evidence", [])
        )
        if not valid:
            return {
                "answer": "检索到了相关内容，但回答引用未通过服务端校验，因此本次不返回结论。",
                "citations": [],
                "error": "; ".join(errors),
                "status": "completed",
            }
        return {"status": "completed", "error": None}

    async def run(self, initial: AgentState) -> AgentState:
        """LangGraph 不可用时保持相同业务语义的运行器。"""
        state: AgentState = dict(initial)
        for node in (self.validate_request, self.retrieve_library, self.grade_evidence):
            state.update(await node(state))
        if state.get("status") == "failed":
            return state
        if state.get("evidence_grade") == "insufficient":
            if state.get("web_enabled"):
                try:
                    state.update(await self.search_arxiv(state))
                    state.update(await self.propose_import(state))
                    return state
                except Exception:
                    state.update(await self.abstain(state))
                    return state
            state.update(await self.abstain(state))
            return state
        state.update(await self.generate_answer(state))
        state.update(await self.validate_answer_citations(state))
        return state


class CompatibleGraph:
    """提供与 LangGraph 编译结果一致的 `ainvoke` 入口。"""

    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime

    async def ainvoke(self, state: AgentState, config: dict[str, Any] | None = None) -> AgentState:
        return await self.runtime.run(state)


def build_agent_graph(
    retriever: SearchLibraryTool | None = None,
    answerer: Answerer | None = None,
    *,
    checkpointer: Any | None = None,
    arxiv_search: SearchArxivTool | None = None,
    use_langgraph: bool = True,
) -> Any:
    """构建受控图。

    `search_arxiv → interrupt → resume` 是下一阶段的显式扩展点；当前图在证据不足时拒答，
    不会未经用户确认自动下载文献。
    """
    runtime = AgentRuntime(
        retriever or EmptyLibrarySearch(),
        answerer or _configured_answerer,
        arxiv_search or ArxivSearch(),
        use_native_interrupt=use_langgraph,
    )
    if not use_langgraph:
        return CompatibleGraph(runtime)
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return CompatibleGraph(runtime)

    graph = StateGraph(AgentState)
    graph.add_node("validate_request", runtime.validate_request)
    graph.add_node("retrieve_library", runtime.retrieve_library)
    graph.add_node("grade_evidence", runtime.grade_evidence)
    graph.add_node("generate_answer", runtime.generate_answer)
    graph.add_node("abstain", runtime.abstain)
    graph.add_node("search_arxiv", runtime.search_arxiv)
    graph.add_node("propose_import", runtime.propose_import)
    graph.add_node("validate_citations", runtime.validate_answer_citations)
    graph.add_edge(START, "validate_request")
    graph.add_edge("validate_request", "retrieve_library")
    graph.add_edge("retrieve_library", "grade_evidence")
    graph.add_conditional_edges(
        "grade_evidence",
        lambda state: (
            "generate"
            if state.get("evidence_grade") == "sufficient"
            else "search_arxiv"
            if state.get("web_enabled")
            else "abstain"
        ),
        {
            "generate": "generate_answer",
            "search_arxiv": "search_arxiv",
            "abstain": "abstain",
        },
    )
    graph.add_edge("search_arxiv", "propose_import")
    graph.add_edge("propose_import", END)
    graph.add_edge("generate_answer", "validate_citations")
    graph.add_edge("validate_citations", END)
    graph.add_edge("abstain", END)
    return graph.compile(checkpointer=checkpointer)
