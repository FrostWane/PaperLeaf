"""Agent 可调用工具的窄接口。

这些接口接收业务身份和显式参数，不向模型暴露数据库、文件系统或任意 URL。
"""

from __future__ import annotations

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


class LibrarySearchInput(BaseModel):
    user_id: str
    query: str = Field(min_length=1, max_length=4000)
    paper_ids: list[str] = Field(default_factory=list, max_length=50)
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

    async def __call__(self, request: LibrarySearchInput) -> list[Evidence]:
        async with get_session_factory()() as session:
            tsquery = func.plainto_tsquery("simple", request.query)
            lexical_match = func.to_tsvector("simple", PaperChunk.text).op("@@")(tsquery)
            trigram_score = func.similarity(PaperChunk.text, request.query)
            rank = func.greatest(
                func.ts_rank_cd(func.to_tsvector("simple", PaperChunk.text), tsquery),
                trigram_score,
            )
            conditions = [
                Paper.owner_id == request.user_id,
                or_(
                    lexical_match,
                    PaperChunk.text.contains(request.query, autoescape=True),
                    trigram_score > 0.08,
                ),
            ]
            if request.paper_ids:
                conditions.append(Paper.id.in_(request.paper_ids))
            keyword_rows = (
                await session.execute(
                    select(PaperChunk, Paper, rank.label("score"))
                    .join(Paper, Paper.id == PaperChunk.paper_id)
                    .where(*conditions)
                    .order_by(rank.desc())
                    .limit(max(request.limit * 5, 40))
                )
            ).all()

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

        def evidence_from(row: Any) -> Evidence:
            chunk, paper = row[0], row[1]
            return Evidence(
                chunk_id=chunk.id,
                paper_id=paper.id,
                paper_title=paper.title,
                physical_page=chunk.physical_page,
                text=chunk.text,
            )

        keyword_hits = [
            RankedHit(row[0].id, float(row[2]), evidence_from(row)) for row in keyword_rows
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
                for name, scores in (("keyword", keyword_scores), ("vector", vector_scores))
                if hit.id in scores
            )
            channel_scores = tuple(
                (name, scores[hit.id])
                for name, scores in (("keyword", keyword_scores), ("vector", vector_scores))
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
                )
            )
        return deduplicate_evidence_by_page(fused_evidence, limit=request.limit)
