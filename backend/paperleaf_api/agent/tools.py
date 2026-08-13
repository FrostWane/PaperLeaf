"""Agent 可调用工具的窄接口。

这些接口接收业务身份和显式参数，不向模型暴露数据库、文件系统或任意 URL。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from ..config import settings
from ..db import get_session_factory
from ..embedding_contract import configured_embedding_contract
from ..model_runtime import ModelRouter, ModelRuntimeError, build_model_router
from ..models import Paper, PaperChunk, PaperPage
from ..rag.citations import Evidence
from ..rag.retrieval_enhancements import (
    MultiGranularLexicalScorer,
    assess_rewrite_need,
    balance_evidence_by_paper,
    rerank_evidence_by_sentence_windows,
    technical_tokens,
)
from ..rag.retrieval_quality import deduplicate_evidence_by_page
from ..rag.rrf import RankedHit, reciprocal_rank_fusion
from ..selection_context import match_selection_to_page

_LATIN_QUERY_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+(?:[-_.][a-zA-Z0-9]+)*")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_SCOPED_OVERVIEW_CJK_HINTS = (
    "讲了什么",
    "讲什么",
    "主要内容",
    "总结",
    "概括",
    "概览",
    "介绍一下",
    "研究内容",
)
_SCOPED_OVERVIEW_EN_RE = re.compile(
    r"\b(?:summari[sz]e|overview|what\s+is\s+(?:this|the)\s+(?:paper|article)\s+about)\b",
    re.IGNORECASE,
)
_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "does",
    "for",
    "how",
    "in",
    "is",
    "of",
    "on",
    "paper",
    "study",
    "the",
    "this",
    "to",
    "what",
    "which",
    "with",
}
_COMPARISON_STOPWORDS = {
    "compare",
    "comparison",
    "contrast",
    "difference",
    "differences",
    "versus",
    "vs",
}
_DIAGNOSTIC_UNSET = object()


def _keyword_search_query(query: str, *, limit: int = 12) -> str:
    """把自然问句变成 PostgreSQL websearch 的 OR 查询，避免整句 AND 导致零召回。"""

    terms: list[str] = []
    for raw in _LATIN_QUERY_TOKEN_RE.findall(query.casefold()):
        if raw in _QUERY_STOPWORDS or len(raw) < 2 or raw in terms:
            continue
        terms.append(raw)
        if len(terms) == limit:
            break
    return " OR ".join(terms)


def _deterministic_supplemental_query(query: str) -> str | None:
    """去掉英文问句与比较框架词，保留实体、数字和主题词。"""

    terms: list[str] = []
    for raw in _LATIN_QUERY_TOKEN_RE.findall(query):
        normalized = raw.casefold()
        if (
            normalized in _QUERY_STOPWORDS
            or normalized in _COMPARISON_STOPWORDS
            or len(normalized) < 2
            or normalized in {item.casefold() for item in terms}
        ):
            continue
        terms.append(raw)
    rendered = " ".join(terms[:16]).strip()
    if not rendered or rendered.casefold() == " ".join(query.split()).casefold():
        return None
    return rendered[:500]


def _is_scoped_overview_query(query: str) -> bool:
    """识别“概括当前论文”意图；只有显式论文范围存在时才会启用快路。"""

    normalized = "".join(query.casefold().split())
    if any(hint in normalized for hint in _SCOPED_OVERVIEW_CJK_HINTS):
        return True
    return bool(_SCOPED_OVERVIEW_EN_RE.search(query))


class LibrarySearchInput(BaseModel):
    user_id: str
    query: str = Field(min_length=1, max_length=4000)
    # 客户端显式选择仍由 API 限制为 50；集合范围由服务端递归解析，可能超过 50。
    paper_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=8, ge=1, le=20)
    # 多论文比较由服务端显式开启。普通全库搜索不会对大量论文逐一查询。
    ensure_paper_coverage: bool = False
    per_paper_query_mode: Literal["same_query", "paper_specific"] = "same_query"


class ArxivSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)


@dataclass(frozen=True)
class ToolResult:
    data: Any
    audit_summary: str


class SearchLibraryTool(Protocol):
    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]: ...


class SearchArxivTool(Protocol):
    async def __call__(self, request: ArxivSearchInput) -> ToolResult: ...


class _FastEmbedRerankScorer:
    """可选本地 Cross-Encoder；未安装评测依赖时由调用方安全降级。"""

    def __init__(self, model_name: str) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self.model = TextCrossEncoder(model_name=model_name)

    def score(self, query: str, documents: Sequence[str]) -> Sequence[float]:
        return list(self.model.rerank(query, list(documents)))


class EmptyLibrarySearch:
    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        return []


class DemoLibrarySearch:
    """演示检索器，只返回内置的明确证据。"""

    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        if not request.query.strip():
            return []
        return [
            Evidence(
                chunk_id="demo-paper:p3:c0",
                paper_id="demo-paper",
                paper_title="Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
                physical_page=3,
                text="RAG combines parametric and non-parametric memory for generation.",
                retrieval_score=1.0,
                retrieval_channels=("demo",),
                channel_scores=(("demo", 1.0),),
            )
        ]


class ArxivSearch:
    async def __call__(self, request: ArxivSearchInput) -> ToolResult:
        from ..arxiv_service import search_arxiv

        papers = await search_arxiv(request.query, request.limit)
        return ToolResult(
            data=[paper.__dict__ for paper in papers],
            audit_summary=f"arXiv 返回 {len(papers)} 个候选",
        )


class SQLLibrarySearch:
    """按用户隔离的 PostgreSQL 全文 + 可选向量检索。"""

    def __init__(
        self,
        config: Any = settings,
        model_router: ModelRouter[Any] | None = None,
        reranker: Any | None = None,
    ) -> None:
        self.config = config
        self.model_router = model_router or build_model_router(config)
        self.reranker = reranker
        # SQLLibrarySearch 由 API/Worker 复用。检索诊断必须按 async Task 隔离，
        # 否则两个并发请求会互相覆盖改写原因和降级状态。
        self._vector_fallback_reason: ContextVar[object] = ContextVar(
            f"rag_vector_fallback_{id(self)}", default=_DIAGNOSTIC_UNSET
        )
        self._rewrite_reasons: ContextVar[object] = ContextVar(
            f"rag_rewrite_reasons_{id(self)}", default=_DIAGNOSTIC_UNSET
        )
        self._reranker_fallback_reason: ContextVar[object] = ContextVar(
            f"rag_reranker_fallback_{id(self)}", default=_DIAGNOSTIC_UNSET
        )
        self._candidate_snapshot: ContextVar[object] = ContextVar(
            f"rag_candidate_snapshot_{id(self)}", default=_DIAGNOSTIC_UNSET
        )
        self._last_vector_fallback_snapshot: str | None = None
        self._last_rewrite_reasons_snapshot: tuple[str, ...] = ()
        self._last_reranker_fallback_snapshot: str | None = None
        self._last_candidate_snapshot: tuple[Evidence, ...] = ()

    def _diagnostic_context_var(self, attribute: str) -> ContextVar[object]:
        value = getattr(self, attribute, None)
        if isinstance(value, ContextVar):
            return value
        value = ContextVar(f"{attribute}_{id(self)}", default=_DIAGNOSTIC_UNSET)
        setattr(self, attribute, value)
        return value

    @property
    def last_vector_fallback_reason(self) -> str | None:
        value = self._diagnostic_context_var("_vector_fallback_reason").get()
        if value is _DIAGNOSTIC_UNSET:
            return getattr(self, "_last_vector_fallback_snapshot", None)
        return value if isinstance(value, str) else None

    @last_vector_fallback_reason.setter
    def last_vector_fallback_reason(self, value: str | None) -> None:
        self._diagnostic_context_var("_vector_fallback_reason").set(value)
        self._last_vector_fallback_snapshot = value

    @property
    def last_rewrite_reasons(self) -> tuple[str, ...]:
        value = self._diagnostic_context_var("_rewrite_reasons").get()
        if value is _DIAGNOSTIC_UNSET:
            return getattr(self, "_last_rewrite_reasons_snapshot", ())
        return value if isinstance(value, tuple) else ()

    @last_rewrite_reasons.setter
    def last_rewrite_reasons(self, value: tuple[str, ...]) -> None:
        self._diagnostic_context_var("_rewrite_reasons").set(value)
        self._last_rewrite_reasons_snapshot = value

    @property
    def last_reranker_fallback_reason(self) -> str | None:
        value = self._diagnostic_context_var("_reranker_fallback_reason").get()
        if value is _DIAGNOSTIC_UNSET:
            return getattr(self, "_last_reranker_fallback_snapshot", None)
        return value if isinstance(value, str) else None

    @last_reranker_fallback_reason.setter
    def last_reranker_fallback_reason(self, value: str | None) -> None:
        self._diagnostic_context_var("_reranker_fallback_reason").set(value)
        self._last_reranker_fallback_snapshot = value

    @property
    def last_candidate_snapshot(self) -> tuple[Evidence, ...]:
        """返回同一次检索在页级去重后的候选池，供只读评测记录 Top-40。"""

        value = self._diagnostic_context_var("_candidate_snapshot").get()
        if value is _DIAGNOSTIC_UNSET:
            return getattr(self, "_last_candidate_snapshot", ())
        return value if isinstance(value, tuple) else ()

    @last_candidate_snapshot.setter
    def last_candidate_snapshot(self, value: tuple[Evidence, ...]) -> None:
        self._diagnostic_context_var("_candidate_snapshot").set(value)
        self._last_candidate_snapshot = value

    async def _embed_query(self, query: str) -> list[float] | None:
        self.last_vector_fallback_reason = None
        if not self.model_router.has_provider("embedding"):
            self.last_vector_fallback_reason = "embedding_provider_unavailable"
            return None
        contract = configured_embedding_contract(self.config, self.model_router)
        if contract is None:
            self.last_vector_fallback_reason = "embedding_contract_mismatch"
            return None
        from langchain_openai import OpenAIEmbeddings

        async def invoke(provider: Any) -> list[float]:
            if provider.embedding_model != contract.model:
                raise RuntimeError("EMBEDDING_CONTRACT_MISMATCH")
            kwargs: dict[str, Any] = {
                "model": provider.embedding_model,
                "api_key": provider.api_key,
                "base_url": provider.base_url,
                "max_retries": 0,
                # 查询已经受 API 长度限制，不需要 LangChain 把字符串重编码为
                # Token 数组；部分兼容服务只稳定接受字符串输入。
                "check_embedding_ctx_length": False,
            }
            if self.config.embedding_dimensions:
                kwargs["dimensions"] = self.config.embedding_dimensions
            return await OpenAIEmbeddings(**kwargs).aembed_query(query)

        try:
            vector = await self.model_router.execute(
                "embedding", invoke, required_model=contract.model
            )
        except ModelRuntimeError:
            # 向量服务故障时保留关键词检索，不让整个文库问答不可用。
            self.last_vector_fallback_reason = "embedding_provider_unavailable"
            return None
        if len(vector) != contract.dimensions:
            self.last_vector_fallback_reason = "query_dimension_mismatch"
            return None
        return vector

    async def _embed_rewritten_queries(
        self,
        queries: Sequence[str],
        *,
        contract: Any,
    ) -> dict[str, list[float]]:
        """批量嵌入逐论文补充查询，避免每篇论文单独发起模型请求。"""

        normalized = list(dict.fromkeys(query.strip() for query in queries if query.strip()))
        if not normalized:
            return {}
        from langchain_openai import OpenAIEmbeddings

        async def invoke(provider: Any) -> list[list[float]]:
            if provider.embedding_model != contract.model:
                raise RuntimeError("EMBEDDING_CONTRACT_MISMATCH")
            kwargs: dict[str, Any] = {
                "model": provider.embedding_model,
                "api_key": provider.api_key,
                "base_url": provider.base_url,
                "max_retries": 0,
                "check_embedding_ctx_length": False,
            }
            if self.config.embedding_dimensions:
                kwargs["dimensions"] = self.config.embedding_dimensions
            return await OpenAIEmbeddings(**kwargs).aembed_documents(normalized)

        try:
            vectors = await self.model_router.execute(
                "embedding",
                invoke,
                required_model=contract.model,
            )
        except (ModelRuntimeError, RuntimeError):
            self.last_vector_fallback_reason = "embedding_provider_unavailable"
            return {}
        if len(vectors) != len(normalized) or any(
            len(vector) != contract.dimensions for vector in vectors
        ):
            self.last_vector_fallback_reason = "query_dimension_mismatch"
            return {}
        return dict(zip(normalized, vectors))

    async def page_selection_evidence(
        self,
        *,
        user_id: str,
        paper_id: str,
        physical_page: int,
        selected_text: str,
        limit: int = 3,
    ) -> list[Evidence]:
        """用服务端真实 Chunk 构造选文及同页相邻证据，不依赖检索词命中。"""

        async with get_session_factory()() as session:
            rows = list(
                (
                    await session.execute(
                        select(PaperChunk, Paper)
                        .join(Paper, Paper.id == PaperChunk.paper_id)
                        .where(
                            Paper.owner_id == user_id,
                            Paper.id == paper_id,
                            PaperChunk.physical_page == physical_page,
                        )
                        .order_by(PaperChunk.chunk_index)
                    )
                ).all()
            )
        if not rows:
            return []
        matches = [
            index
            for index, (chunk, _paper) in enumerate(rows)
            if match_selection_to_page(selected_text, chunk.text).accepted
        ]
        anchor = matches[0] if matches else 0
        indexes = [anchor]
        distance = 1
        while len(indexes) < min(limit, len(rows)):
            for candidate in (anchor - distance, anchor + distance):
                if 0 <= candidate < len(rows) and candidate not in indexes:
                    indexes.append(candidate)
                    if len(indexes) >= min(limit, len(rows)):
                        break
            distance += 1
        result: list[Evidence] = []
        for index in indexes:
            chunk, paper = rows[index]
            result.append(
                Evidence(
                    chunk_id=chunk.id,
                    paper_id=paper.id,
                    paper_title=paper.title,
                    physical_page=chunk.physical_page,
                    text=chunk.text,
                    retrieval_score=1.0 if index == anchor else 0.8,
                    retrieval_channels=(
                        "verified_selection" if index == anchor else "selection_neighbor",
                    ),
                    channel_scores=(("selection", 1.0 if index == anchor else 0.8),),
                    retrieval_query=selected_text,
                    chunking_strategy=getattr(paper, "chunking_strategy", "unknown"),
                )
            )
        return result

    async def _rewrite_queries(
        self,
        query: str,
        *,
        reasons: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """生成最多两条补充查询，并强制保留数字、缩写和英文实体。"""

        max_queries = int(getattr(self.config, "rag_query_rewrite_max_queries", 2) or 0)
        if max_queries <= 0 or not self.model_router.has_provider("query_rewrite"):
            return ()
        from langchain_openai import ChatOpenAI

        required = technical_tokens(query)

        async def invoke(provider: Any) -> Any:
            model = ChatOpenAI(
                model=provider.chat_model,
                api_key=provider.api_key,
                base_url=provider.base_url,
                temperature=0,
                max_retries=0,
                max_tokens=160,
            )
            return await model.ainvoke(
                [
                    (
                        "system",
                        "你是学术 PDF 检索查询改写器。只输出 JSON："
                        '{"queries":["query one","query two"]}。生成至多两条用于检索英文'
                        "论文原文的短查询：一条保留技术实体，一条聚焦问题意图。不得回答问题，"
                        "不得添加用户未提及的数字、模型名或结论。",
                    ),
                    (
                        "human",
                        f"原问题：{query}\n弱结果原因：{','.join(reasons) or 'unspecified'}\n"
                        f"必须保留的实体：{', '.join(required) or '无'}",
                    ),
                ]
            )

        try:
            response = await self.model_router.execute(
                "query_rewrite",
                invoke,
                # 查询改写只是召回增强，不能让用户为了一个辅助步骤等待二十多秒。
                timeout_seconds=min(self.model_router.timeout_seconds, 6.0),
            )
        except ModelRuntimeError:
            return ()
        content = str(response.content).strip()
        raw_queries: list[str] = []
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and isinstance(parsed.get("queries"), list):
                raw_queries = [str(item) for item in parsed["queries"]]
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_queries = [item for item in content.splitlines() if item.strip()]
        queries: list[str] = []
        required_suffix = " ".join(required)
        for raw in raw_queries:
            cleaned = " ".join(_LATIN_QUERY_TOKEN_RE.findall(raw))
            if required_suffix:
                cleaned = f"{cleaned} {required_suffix}".strip()
            normalized = " ".join(dict.fromkeys(cleaned.split()))
            if (
                normalized
                and normalized.casefold() != query.casefold()
                and normalized not in queries
            ):
                queries.append(normalized[:500])
            if len(queries) == max_queries:
                break
        return tuple(queries)

    async def _rewrite_query(self, query: str) -> str | None:
        """兼容旧调用方；新链路使用 ``_rewrite_queries``。"""

        values = await self._rewrite_queries(query)
        return values[0] if values else None

    async def _paper_titles(
        self,
        session: Any,
        request: LibrarySearchInput,
    ) -> dict[str, str]:
        if not request.paper_ids:
            return {}
        rows = list(
            (
                await session.execute(
                    select(Paper.id, Paper.title).where(
                        Paper.owner_id == request.user_id,
                        Paper.id.in_(request.paper_ids),
                    )
                )
            ).all()
        )
        return {str(paper_id): str(title) for paper_id, title in rows}

    @staticmethod
    def _deterministic_paper_queries(
        query: str,
        paper_titles: dict[str, str],
    ) -> dict[str, str]:
        focused = _deterministic_supplemental_query(query) or query
        return {
            paper_id: f"{title[:240]} {focused}"[:500] for paper_id, title in paper_titles.items()
        }

    async def _rewrite_paper_queries(
        self,
        query: str,
        paper_titles: dict[str, str],
        *,
        reasons: Sequence[str] = (),
    ) -> dict[str, str]:
        """一次模型调用生成受论文标题约束的专属查询，最多覆盖十篇论文。"""

        if not paper_titles or not self.model_router.has_provider("query_rewrite"):
            return {}
        from langchain_openai import ChatOpenAI

        ordered = sorted(paper_titles.items())[:10]
        aliases = {f"P{index}": paper_id for index, (paper_id, _title) in enumerate(ordered, 1)}
        descriptor = "\n".join(
            f"{alias}: {paper_titles[paper_id][:300]}" for alias, paper_id in aliases.items()
        )
        required = technical_tokens(query)

        async def invoke(provider: Any) -> Any:
            model = ChatOpenAI(
                model=provider.chat_model,
                api_key=provider.api_key,
                base_url=provider.base_url,
                temperature=0,
                max_retries=0,
                max_tokens=360,
            ).bind(response_format={"type": "json_object"})
            return await model.ainvoke(
                [
                    (
                        "system",
                        "你是多论文检索计划器。只输出 JSON："
                        '{"queries":{"P1":"short English query"}}。为每篇论文生成一条'
                        "与用户比较维度和论文主题相符的英文短查询。不得回答问题、猜测结论或"
                        "改变 P 编号；必须保留用户问题中的数字、缩写和技术实体。",
                    ),
                    (
                        "human",
                        f"问题：{query}\n弱结果原因：{','.join(reasons)}\n"
                        f"必须保留：{', '.join(required) or '无'}\n论文：\n{descriptor}",
                    ),
                ]
            )

        try:
            response = await self.model_router.execute(
                "query_rewrite",
                invoke,
                timeout_seconds=min(self.model_router.timeout_seconds, 8.0),
            )
            parsed = json.loads(str(response.content).strip())
        except (ModelRuntimeError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        raw = parsed.get("queries") if isinstance(parsed, dict) else None
        if not isinstance(raw, dict):
            return {}
        required_suffix = " ".join(required)
        result: dict[str, str] = {}
        for alias, paper_id in aliases.items():
            value = raw.get(alias)
            if not isinstance(value, str):
                continue
            cleaned = " ".join(_LATIN_QUERY_TOKEN_RE.findall(value))
            if required_suffix:
                cleaned = f"{cleaned} {required_suffix}".strip()
            normalized = " ".join(dict.fromkeys(cleaned.split()))
            if normalized:
                result[paper_id] = normalized[:500]
        return result

    async def _keyword_rows(
        self,
        session: Any,
        request: LibrarySearchInput,
        query: str,
        *,
        paper_id: str | None = None,
        row_limit: int | None = None,
    ) -> list[Any]:
        search_query = _keyword_search_query(query)
        # PostgreSQL 的 simple 配置不会为连续中文可靠分词，但中文术语仍可通过
        # substring/pg_trgm 命中。不能因为没有拉丁词就直接返回空列表，否则在未配置
        # Embedding/查询改写模型时，中文全文检索会完全失效。
        if not search_query and not _CJK_RE.search(query):
            return []
        trigram_score = func.similarity(PaperChunk.text, query)
        if search_query:
            tsquery = func.websearch_to_tsquery("simple", search_query)
            lexical_match = func.to_tsvector("simple", PaperChunk.text).op("@@")(tsquery)
            rank = func.greatest(
                func.ts_rank_cd(func.to_tsvector("simple", PaperChunk.text), tsquery),
                trigram_score,
            )
            match_condition = or_(
                lexical_match,
                PaperChunk.text.contains(query, autoescape=True),
                trigram_score > 0.08,
            )
        else:
            rank = trigram_score
            match_condition = or_(
                PaperChunk.text.contains(query, autoescape=True),
                trigram_score > 0.08,
            )
        conditions = [
            Paper.owner_id == request.user_id,
            match_condition,
        ]
        if paper_id:
            conditions.append(Paper.id == paper_id)
        elif request.paper_ids:
            conditions.append(Paper.id.in_(request.paper_ids))
        return list(
            (
                await session.execute(
                    select(PaperChunk, Paper, rank.label("score"))
                    .join(Paper, Paper.id == PaperChunk.paper_id)
                    .where(*conditions)
                    .order_by(rank.desc())
                    .limit(row_limit or max(request.limit * 5, 40))
                )
            ).all()
        )

    async def _scoped_overview_evidence(self, request: LibrarySearchInput) -> list[Evidence]:
        """从用户明确选中的单篇论文跨页取样，避免概览问题依赖查询翻译。"""

        paper_id = request.paper_ids[0]
        async with get_session_factory()() as session:
            rows = list(
                (
                    await session.execute(
                        select(PaperChunk, Paper)
                        .join(Paper, Paper.id == PaperChunk.paper_id)
                        .where(
                            Paper.owner_id == request.user_id,
                            Paper.id == paper_id,
                            PaperChunk.chunk_index == 0,
                        )
                        .order_by(PaperChunk.physical_page)
                        .limit(max(request.limit * 20, 80))
                    )
                ).all()
            )
        if not rows:
            return []
        if len(rows) <= request.limit:
            selected_rows = rows
        elif request.limit == 1:
            selected_rows = [rows[0]]
        else:
            indexes = [
                round(index * (len(rows) - 1) / (request.limit - 1))
                for index in range(request.limit)
            ]
            selected_rows = [rows[index] for index in dict.fromkeys(indexes)]
        return [
            Evidence(
                chunk_id=chunk.id,
                paper_id=paper.id,
                paper_title=paper.title,
                physical_page=chunk.physical_page,
                text=chunk.text,
                retrieval_score=1.0,
                retrieval_channels=("scoped_overview",),
                channel_scores=(("scoped_overview", 1.0),),
                retrieval_query=request.query,
                chunking_strategy=getattr(paper, "chunking_strategy", "unknown"),
            )
            for chunk, paper in selected_rows
        ]

    async def _has_stored_dimension_mismatch(
        self,
        session: Any,
        request: LibrarySearchInput,
        dimensions: int,
    ) -> bool:
        conditions = [
            Paper.owner_id == request.user_id,
            Paper.embedding_status == "ready",
            Paper.embedding_dimensions == dimensions,
            PaperChunk.embedding.is_not(None),
            func.vector_dims(PaperChunk.embedding) != dimensions,
        ]
        if request.paper_ids:
            conditions.append(Paper.id.in_(request.paper_ids))
        count = await session.scalar(
            select(func.count(PaperChunk.id))
            .join(Paper, Paper.id == PaperChunk.paper_id)
            .where(*conditions)
        )
        return int(count or 0) > 0

    async def _has_scope_contract_mismatch(
        self,
        session: Any,
        request: LibrarySearchInput,
        contract: Any,
    ) -> bool:
        """检查请求范围内是否存在不能参与当前向量空间的论文。

        向量查询可以继续使用范围内其余 ready 论文，但必须把部分关键词降级写入
        RAG Trace；否则管理员会把“静默排除 stale 论文”误认为完整的混合召回。
        """

        conditions = [
            Paper.owner_id == request.user_id,
            or_(
                Paper.embedding_status != "ready",
                Paper.embedding_fingerprint != contract.fingerprint,
                Paper.embedding_dimensions != contract.dimensions,
            ),
        ]
        if request.paper_ids:
            conditions.append(Paper.id.in_(request.paper_ids))
        count = await session.scalar(select(func.count(Paper.id)).where(*conditions))
        return int(count or 0) > 0

    async def _vector_rows(
        self,
        session: Any,
        request: LibrarySearchInput,
        query_vector: list[float],
        contract: Any,
        *,
        paper_id: str | None = None,
        row_limit: int,
    ) -> list[Any]:
        distance = PaperChunk.embedding.cosine_distance(query_vector)
        conditions = [
            Paper.owner_id == request.user_id,
            Paper.embedding_status == "ready",
            Paper.embedding_fingerprint == contract.fingerprint,
            Paper.embedding_dimensions == contract.dimensions,
            PaperChunk.embedding.is_not(None),
        ]
        if paper_id:
            conditions.append(Paper.id == paper_id)
        elif request.paper_ids:
            conditions.append(Paper.id.in_(request.paper_ids))
        return list(
            (
                await session.execute(
                    select(PaperChunk, Paper, distance.label("distance"))
                    .join(Paper, Paper.id == PaperChunk.paper_id)
                    .where(*conditions)
                    .order_by(distance)
                    .limit(row_limit)
                )
            ).all()
        )

    def _fuse_channels(
        self,
        channels: Sequence[tuple[str, list[Any], str]],
        *,
        candidate_limit: int,
    ) -> list[Evidence]:
        rankings: list[list[RankedHit]] = []
        scores_by_type: dict[str, dict[str, float]] = {}
        query_by_hit: dict[str, str] = {}
        payload_by_hit: dict[str, Evidence] = {}
        for channel_name, rows, matched_query in channels:
            ranking: list[RankedHit] = []
            for row in rows:
                chunk, paper = row[0], row[1]
                score = 1.0 - float(row[2]) if channel_name == "vector" else float(row[2])
                payload = Evidence(
                    chunk_id=chunk.id,
                    paper_id=paper.id,
                    paper_title=paper.title,
                    physical_page=chunk.physical_page,
                    text=chunk.text,
                    retrieval_query=matched_query,
                    chunking_strategy=getattr(paper, "chunking_strategy", "unknown"),
                    vector_fallback_reason=self.last_vector_fallback_reason,
                )
                ranking.append(RankedHit(chunk.id, score, payload))
                payload_by_hit.setdefault(chunk.id, payload)
                scores = scores_by_type.setdefault(channel_name, {})
                scores[chunk.id] = max(scores.get(chunk.id, float("-inf")), score)
                if channel_name == "keyword_rewrite" and matched_query:
                    query_by_hit[chunk.id] = matched_query
                elif channel_name != "vector" and matched_query:
                    query_by_hit.setdefault(chunk.id, matched_query)
            if ranking:
                rankings.append(ranking)
        if not rankings:
            return []
        fused: list[Evidence] = []
        for hit in reciprocal_rank_fusion(rankings, limit=candidate_limit):
            payload = payload_by_hit.get(hit.id)
            if payload is None:
                continue
            hit_channels = tuple(
                name for name, scores in scores_by_type.items() if hit.id in scores
            )
            channel_scores = tuple(
                (name, scores[hit.id])
                for name, scores in scores_by_type.items()
                if hit.id in scores
            )
            fused.append(
                Evidence(
                    chunk_id=payload.chunk_id,
                    paper_id=payload.paper_id,
                    paper_title=payload.paper_title,
                    physical_page=payload.physical_page,
                    text=payload.text,
                    retrieval_score=hit.score,
                    retrieval_channels=hit_channels,
                    channel_scores=channel_scores,
                    retrieval_query=query_by_hit.get(hit.id, payload.retrieval_query),
                    chunking_strategy=payload.chunking_strategy,
                    vector_fallback_reason=payload.vector_fallback_reason,
                )
            )
        return deduplicate_evidence_by_page(fused, limit=candidate_limit)

    async def _maybe_rerank(
        self,
        query: str,
        candidates: list[Evidence],
        *,
        limit: int,
    ) -> list[Evidence]:
        self.last_reranker_fallback_reason = None
        if not bool(getattr(self.config, "rag_reranker_enabled", False)):
            return candidates[:limit]
        candidate_limit = int(getattr(self.config, "rag_reranker_candidate_limit", 40) or 40)
        strategy = str(
            getattr(self.config, "rag_reranker_strategy", "multigranular_v1")
            or "multigranular_v1"
        )
        try:
            if self.reranker is None and strategy == "multigranular_v1":
                self.reranker = MultiGranularLexicalScorer()
            elif self.reranker is None and strategy == "legacy_cross_encoder":
                self.reranker = await asyncio.wait_for(
                    asyncio.to_thread(
                        _FastEmbedRerankScorer,
                        str(getattr(self.config, "rag_reranker_model", "")),
                    ),
                    timeout=float(getattr(self.config, "rag_reranker_timeout_seconds", 3.0) or 3.0),
                )
            elif self.reranker is None:
                raise ValueError("unknown_reranker_strategy")
            scoped = candidates[:candidate_limit]
            page_texts: dict[tuple[str, int], str] = {}
            if scoped:
                conditions = [
                    (PaperPage.paper_id == item.paper_id)
                    & (PaperPage.physical_page == item.physical_page)
                    for item in scoped
                ]
                async with get_session_factory()() as session:
                    rows = await session.execute(
                        select(PaperPage.paper_id, PaperPage.physical_page, PaperPage.text).where(
                            or_(*conditions)
                        )
                    )
                    page_texts = {
                        (str(paper_id), int(physical_page)): text or ""
                        for paper_id, physical_page, text in rows.all()
                    }
            documents = [
                page_texts.get((item.paper_id, item.physical_page), "") or item.text
                for item in scoped
            ]
            return await asyncio.wait_for(
                asyncio.to_thread(
                    rerank_evidence_by_sentence_windows,
                    query,
                    scoped,
                    self.reranker,
                    limit=limit,
                    document_texts=documents,
                    channel_name=(
                        "multigranular_reranker"
                        if strategy == "multigranular_v1"
                        else "legacy_sentence_reranker"
                    ),
                ),
                timeout=float(getattr(self.config, "rag_reranker_timeout_seconds", 3.0) or 3.0),
            )
        except Exception:
            # 重排只是可选排序增强；依赖缺失、模型下载失败或超时都保留 RRF。
            self.last_reranker_fallback_reason = "reranker_unavailable"
            return candidates[:limit]

    def _annotate_retrieval(
        self,
        evidence: Sequence[Evidence],
        *,
        per_paper: bool,
    ) -> list[Evidence]:
        channels = {channel for item in evidence for channel in item.retrieval_channels}
        processors = [
            *(["per_paper_balance"] if per_paper else []),
            *(["weak_query_rewrite"] if "keyword_rewrite" in channels else []),
            *(
                ["multigranular_page_rerank"]
                if "multigranular_reranker" in channels
                else []
            ),
            *(
                ["legacy_sentence_window_rerank"]
                if "legacy_sentence_reranker" in channels
                else []
            ),
        ]
        return [
            replace(
                item,
                retrieval_processors=tuple(processors),
                query_rewrite_reasons=self.last_rewrite_reasons,
                reranker_fallback_reason=self.last_reranker_fallback_reason,
            )
            for item in evidence
        ]

    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        self.last_vector_fallback_reason = None
        self.last_rewrite_reasons = ()
        self.last_reranker_fallback_reason = None
        self.last_candidate_snapshot = ()
        if len(request.paper_ids) == 1 and _is_scoped_overview_query(request.query):
            overview = await self._scoped_overview_evidence(request)
            self.last_candidate_snapshot = tuple(overview)
            return overview

        candidate_limit = max(
            request.limit * 5,
            int(getattr(self.config, "rag_candidate_pool_size", 40) or 40),
        )
        per_paper = (
            bool(getattr(self.config, "rag_per_paper_retrieval_enabled", True))
            and request.ensure_paper_coverage
            and 1 < len(request.paper_ids) <= 10
        )
        per_paper_row_limit = max(
            int(getattr(self.config, "rag_per_paper_candidate_limit", 5) or 5) * 3,
            10,
        )
        independent_same_query = per_paper and request.per_paper_query_mode == "same_query"
        row_limit = per_paper_row_limit if independent_same_query else candidate_limit
        async with get_session_factory()() as session:
            channels: list[tuple[str, list[Any], str]] = []
            # 逐论文专属查询先保留现有全局候选作为回退；只有消融模式
            # per_paper_same 才会把原问题也对每篇论文独立执行。
            scopes: list[str | None] = list(request.paper_ids) if independent_same_query else [None]
            for paper_id in scopes:
                rows = await self._keyword_rows(
                    session,
                    request,
                    request.query,
                    paper_id=paper_id,
                    row_limit=row_limit,
                )
                if rows:
                    channels.append(("keyword", rows, request.query))

            query_vector = await self._embed_query(request.query)
            contract = None
            if query_vector:
                contract = configured_embedding_contract(self.config, self.model_router)
                if contract is None:
                    self.last_vector_fallback_reason = "embedding_contract_mismatch"
                else:
                    if await self._has_scope_contract_mismatch(session, request, contract):
                        self.last_vector_fallback_reason = "embedding_contract_mismatch"
                    try:
                        stored_mismatch = await self._has_stored_dimension_mismatch(
                            session, request, contract.dimensions
                        )
                    except Exception:
                        stored_mismatch = False
                        self.last_vector_fallback_reason = "vector_query_failed"
                    if stored_mismatch:
                        self.last_vector_fallback_reason = "stored_dimension_mismatch"
                    if stored_mismatch or self.last_vector_fallback_reason == "vector_query_failed":
                        query_vector = None
                if contract is not None and query_vector:
                    for paper_id in scopes:
                        try:
                            rows = await self._vector_rows(
                                session,
                                request,
                                query_vector,
                                contract,
                                paper_id=paper_id,
                                row_limit=row_limit,
                            )
                        except Exception:
                            self.last_vector_fallback_reason = "vector_query_failed"
                            rows = []
                        if rows:
                            channels.append(("vector", rows, request.query))

            initial = self._fuse_channels(channels, candidate_limit=candidate_limit)
            decision = assess_rewrite_need(request.query, initial)
            self.last_rewrite_reasons = decision.reasons
            if (
                bool(getattr(self.config, "rag_weak_query_rewrite_enabled", True))
                and decision.required
            ):
                paper_queries: dict[str, str] = {}
                paper_query_sets: dict[str, list[str]] = {}
                if per_paper and request.per_paper_query_mode == "paper_specific":
                    paper_titles = await self._paper_titles(session, request)
                    deterministic_queries = self._deterministic_paper_queries(
                        request.query, paper_titles
                    )
                    paper_query_sets = {
                        paper_id: [rewritten]
                        for paper_id, rewritten in deterministic_queries.items()
                    }
                    if set(decision.reasons) & {
                        "cross_language",
                        "low_lexical_coverage",
                        "no_candidates",
                    }:
                        paper_queries = await self._rewrite_paper_queries(
                            request.query,
                            paper_titles,
                            reasons=decision.reasons,
                        )
                        for paper_id, rewritten in paper_queries.items():
                            values = paper_query_sets.setdefault(paper_id, [])
                            if rewritten not in values:
                                values.append(rewritten)
                if paper_query_sets:
                    all_paper_queries = [
                        rewritten for values in paper_query_sets.values() for rewritten in values
                    ]
                    rewritten_vectors = (
                        await self._embed_rewritten_queries(
                            all_paper_queries,
                            contract=contract,
                        )
                        if contract is not None and query_vector
                        else {}
                    )
                    for paper_id, rewritten_queries in paper_query_sets.items():
                        for rewritten_query in rewritten_queries[:2]:
                            rows = await self._keyword_rows(
                                session,
                                request,
                                rewritten_query,
                                paper_id=paper_id,
                                row_limit=per_paper_row_limit,
                            )
                            if rows:
                                channels.append(("keyword_rewrite", rows, rewritten_query))
                            rewritten_vector = rewritten_vectors.get(rewritten_query)
                            if rewritten_vector and contract is not None:
                                try:
                                    rows = await self._vector_rows(
                                        session,
                                        request,
                                        rewritten_vector,
                                        contract,
                                        paper_id=paper_id,
                                        row_limit=per_paper_row_limit,
                                    )
                                except Exception:
                                    self.last_vector_fallback_reason = "vector_query_failed"
                                    rows = []
                                if rows:
                                    channels.append(("vector", rows, rewritten_query))
                else:
                    rewrites: list[str] = []
                    deterministic_query = _deterministic_supplemental_query(request.query)
                    if deterministic_query:
                        rewrites.append(deterministic_query)
                    if set(decision.reasons) & {
                        "cross_language",
                        "low_lexical_coverage",
                        "no_candidates",
                    }:
                        model_rewrites = await self._rewrite_queries(
                            request.query,
                            reasons=decision.reasons,
                        )
                        rewrites.extend(value for value in model_rewrites if value not in rewrites)
                    max_rewrites = int(
                        getattr(self.config, "rag_query_rewrite_max_queries", 2) or 0
                    )
                    for rewritten_query in rewrites[:max_rewrites]:
                        for paper_id in scopes:
                            rows = await self._keyword_rows(
                                session,
                                request,
                                rewritten_query,
                                paper_id=paper_id,
                                row_limit=row_limit,
                            )
                            if rows:
                                channels.append(("keyword_rewrite", rows, rewritten_query))

            candidates = self._fuse_channels(channels, candidate_limit=candidate_limit)

        rerank_limit = candidate_limit if per_paper else request.limit
        candidates = await self._maybe_rerank(
            request.query,
            candidates,
            limit=rerank_limit,
        )
        if per_paper:
            ranked = self._annotate_retrieval(
                balance_evidence_by_paper(
                    candidates,
                    paper_ids=request.paper_ids,
                    limit=candidate_limit,
                    per_paper_limit=int(
                        getattr(self.config, "rag_per_paper_candidate_limit", 5) or 5
                    ),
                ),
                per_paper=True,
            )
        else:
            ranked = self._annotate_retrieval(
                deduplicate_evidence_by_page(candidates, limit=candidate_limit),
                per_paper=False,
            )
        self.last_candidate_snapshot = tuple(ranked)
        return ranked[: request.limit]
