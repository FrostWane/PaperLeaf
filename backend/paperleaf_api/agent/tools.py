"""Agent 可调用工具的窄接口。

这些接口接收业务身份和显式参数，不向模型暴露数据库、文件系统或任意 URL。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from ..config import settings
from ..db import get_session_factory
from ..model_runtime import ModelRouter, ModelRuntimeError, build_model_router
from ..models import Paper, PaperChunk
from ..rag.citations import Evidence
from ..rag.retrieval_quality import deduplicate_evidence_by_page
from ..rag.rrf import RankedHit, reciprocal_rank_fusion

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
    ) -> None:
        self.config = config
        self.model_router = model_router or build_model_router(config)

    async def _embed_query(self, query: str) -> list[float] | None:
        if not self.model_router.has_provider("embedding"):
            return None
        from langchain_openai import OpenAIEmbeddings

        async def invoke(provider: Any) -> list[float]:
            kwargs: dict[str, Any] = {
                "model": provider.embedding_model,
                "api_key": provider.api_key,
                "base_url": provider.base_url,
                "max_retries": 0,
            }
            if self.config.embedding_dimensions:
                kwargs["dimensions"] = self.config.embedding_dimensions
            return await OpenAIEmbeddings(**kwargs).aembed_query(query)

        try:
            return await self.model_router.execute("embedding", invoke)
        except ModelRuntimeError:
            # 向量服务故障时保留关键词检索，不让整个文库问答不可用。
            return None

    async def _rewrite_query(self, query: str) -> str | None:
        """将中文/口语问题改写成少量英文论文检索词；失败时安静回退到原查询。"""

        if not _CJK_RE.search(query) or not self.model_router.has_provider("query_rewrite"):
            return None
        from langchain_openai import ChatOpenAI

        async def invoke(provider: Any) -> Any:
            model = ChatOpenAI(
                model=provider.chat_model,
                api_key=provider.api_key,
                base_url=provider.base_url,
                temperature=0,
                max_retries=0,
                max_tokens=80,
            )
            return await model.ainvoke(
                [
                    (
                        "system",
                        "你是学术 PDF 检索查询改写器。把用户问题改写为 3 到 8 个用于检索"
                        "英文论文原文的英文技术关键词，只输出空格分隔的关键词，不回答问题，"
                        "不输出解释或标点。",
                    ),
                    ("human", query),
                ]
            )

        try:
            response = await self.model_router.execute("query_rewrite", invoke)
        except ModelRuntimeError:
            return None
        rewritten = _keyword_search_query(str(response.content), limit=8)
        return rewritten.replace(" OR ", " ") if rewritten else None

    async def _keyword_rows(
        self,
        session: Any,
        request: LibrarySearchInput,
        query: str,
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
        if request.paper_ids:
            conditions.append(Paper.id.in_(request.paper_ids))
        return list(
            (
                await session.execute(
                    select(PaperChunk, Paper, rank.label("score"))
                    .join(Paper, Paper.id == PaperChunk.paper_id)
                    .where(*conditions)
                    .order_by(rank.desc())
                    .limit(max(request.limit * 5, 40))
                )
            ).all()
        )

    async def _scoped_overview_evidence(
        self, request: LibrarySearchInput
    ) -> list[Evidence]:
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
            )
            for chunk, paper in selected_rows
        ]

    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        if len(request.paper_ids) == 1 and _is_scoped_overview_query(request.query):
            return await self._scoped_overview_evidence(request)

        async with get_session_factory()() as session:
            retrieval_query = request.query
            keyword_channel = "keyword"
            keyword_rows = await self._keyword_rows(session, request, retrieval_query)
            if not keyword_rows:
                rewritten_query = await self._rewrite_query(request.query)
                if rewritten_query:
                    rewritten_rows = await self._keyword_rows(session, request, rewritten_query)
                    if rewritten_rows:
                        keyword_rows = rewritten_rows
                        retrieval_query = rewritten_query
                        keyword_channel = "keyword_rewrite"

            vector_rows: list[Any] = []
            query_vector = await self._embed_query(request.query)
            if query_vector:
                distance = PaperChunk.embedding.cosine_distance(query_vector)
                vector_conditions = [
                    Paper.owner_id == request.user_id,
                    PaperChunk.embedding.is_not(None),
                ]
                if request.paper_ids:
                    vector_conditions.append(Paper.id.in_(request.paper_ids))
                vector_rows = (
                    await session.execute(
                        select(PaperChunk, Paper, distance.label("distance"))
                        .join(Paper, Paper.id == PaperChunk.paper_id)
                        .where(*vector_conditions)
                        .order_by(distance)
                        .limit(max(request.limit * 5, 40))
                    )
                ).all()

        def evidence_from(row: Any, *, matched_query: str = "") -> Evidence:
            chunk, paper = row[0], row[1]
            return Evidence(
                chunk_id=chunk.id,
                paper_id=paper.id,
                paper_title=paper.title,
                physical_page=chunk.physical_page,
                text=chunk.text,
                retrieval_query=matched_query,
            )

        keyword_hits = [
            RankedHit(
                row[0].id,
                float(row[2]),
                evidence_from(row, matched_query=retrieval_query),
            )
            for row in keyword_rows
        ]
        vector_hits = [
            RankedHit(row[0].id, 1.0 - float(row[2]), evidence_from(row)) for row in vector_rows
        ]
        channels = [hits for hits in (vector_hits, keyword_hits) if hits]
        if not channels:
            return []
        channel_limit = max(request.limit * 5, 40)
        keyword_scores = {hit.id: hit.score for hit in keyword_hits}
        vector_scores = {hit.id: hit.score for hit in vector_hits}
        fused_evidence: list[Evidence] = []
        for hit in reciprocal_rank_fusion(channels, limit=channel_limit):
            if not isinstance(hit.payload, Evidence):
                continue
            hit_channels = tuple(
                name
                for name, scores in ((keyword_channel, keyword_scores), ("vector", vector_scores))
                if hit.id in scores
            )
            channel_scores = tuple(
                (name, scores[hit.id])
                for name, scores in ((keyword_channel, keyword_scores), ("vector", vector_scores))
                if hit.id in scores
            )
            fused_evidence.append(
                Evidence(
                    chunk_id=hit.payload.chunk_id,
                    paper_id=hit.payload.paper_id,
                    paper_title=hit.payload.paper_title,
                    physical_page=hit.payload.physical_page,
                    text=hit.payload.text,
                    retrieval_score=hit.score,
                    retrieval_channels=hit_channels,
                    channel_scores=channel_scores,
                    retrieval_query=hit.payload.retrieval_query,
                )
            )
        return deduplicate_evidence_by_page(fused_evidence, limit=request.limit)
