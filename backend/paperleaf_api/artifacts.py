"""证据化总结与结构图。"""

from __future__ import annotations

from sqlalchemy import select

from .config import settings
from .db import get_session_factory
from .models import Paper, PaperChunk
from .rag.citations import Evidence


async def load_paper_evidence(user_id: str, paper_id: str, limit: int = 16) -> list[Evidence]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(PaperChunk, Paper)
                .join(Paper, Paper.id == PaperChunk.paper_id)
                .where(Paper.id == paper_id, Paper.owner_id == user_id)
                .order_by(PaperChunk.physical_page, PaperChunk.chunk_index)
                .limit(limit)
            )
        ).all()
    return [
        Evidence(chunk.id, paper.id, paper.title, chunk.physical_page, chunk.text)
        for chunk, paper in rows
    ]


def extractive_summary(evidence: list[Evidence], max_chars: int = 1800) -> str:
    text = "\n\n".join(item.text for item in evidence[:6])
    return text[:max_chars].rstrip()


async def summarize_evidence(evidence: list[Evidence]) -> tuple[str, str]:
    if not settings.openai_api_key:
        return extractive_summary(evidence), "extractive"
    from langchain_openai import ChatOpenAI

    context = "\n\n".join(
        f"[物理页 {item.physical_page}｜{item.chunk_id}]\n{item.text}" for item in evidence[:12]
    )
    model = ChatOpenAI(
        model=settings.chat_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=0,
    )
    response = await model.ainvoke(
        "请仅根据下列证据，用中文总结论文的研究问题、方法、主要结果与局限。"
        "每段保留物理页码标记；证据缺失的部分明确写未找到。\n\n" + context
    )
    return str(response.content), "model"


def structure_graph(evidence: list[Evidence]) -> tuple[list[dict], list[dict], str]:
    nodes = []
    for index, item in enumerate(evidence[:16], start=1):
        label = " ".join(item.text.split())[:60].replace('"', "'")
        nodes.append(
            {
                "id": f"n{index}",
                "label": label or f"第 {item.physical_page} 页",
                "physical_page": item.physical_page,
                "chunk_id": item.chunk_id,
            }
        )
    edges = [
        {"source": nodes[index - 1]["id"], "target": nodes[index]["id"]}
        for index in range(1, len(nodes))
    ][:24]
    lines = ["flowchart TD"]
    lines.extend(f'    {node["id"]}["{node["label"]}"]' for node in nodes)
    lines.extend(f'    {edge["source"]} --> {edge["target"]}' for edge in edges)
    return nodes, edges, "\n".join(lines)
