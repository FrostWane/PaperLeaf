"""隔离 full-stack smoke 的数据库归属与引用审计。"""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import func, select

from .db import get_session_factory
from .models import AgentRun, Paper, PaperChunk, PaperPage


async def audit(paper_id: str, user_id: str, run_id: str) -> dict[str, object]:
    async with get_session_factory()() as session:
        paper = await session.get(Paper, paper_id)
        run = await session.get(AgentRun, run_id)
        page_count = int(
            await session.scalar(
                select(func.count()).select_from(PaperPage).where(PaperPage.paper_id == paper_id)
            )
            or 0
        )
        chunk_count = int(
            await session.scalar(
                select(func.count()).select_from(PaperChunk).where(PaperChunk.paper_id == paper_id)
            )
            or 0
        )
        citations = list((run.result_summary or {}).get("citations", [])) if run else []
        valid_citations = 0
        pages: set[int] = set()
        for citation in citations:
            chunk_id = str(citation.get("chunk_id", ""))
            physical_page = int(citation.get("physical_page", 0) or 0)
            chunk = await session.get(PaperChunk, chunk_id)
            page_exists = await session.scalar(
                select(PaperPage.id).where(
                    PaperPage.paper_id == paper_id,
                    PaperPage.physical_page == physical_page,
                )
            )
            if (
                chunk
                and chunk.paper_id == paper_id
                and chunk.physical_page == physical_page
                and citation.get("paper_id") == paper_id
                and page_exists
            ):
                valid_citations += 1
                pages.add(physical_page)
        ownership_ok = bool(
            paper
            and paper.owner_id == user_id
            and run
            and run.user_id == user_id
            and run.status == "completed"
        )
        return {
            "ownership_ok": ownership_ok,
            "paper_exists": paper is not None,
            "page_count": page_count,
            "chunk_count": chunk_count,
            "citation_count": len(citations),
            "valid_citation_count": valid_citations,
            "citation_pages": sorted(pages),
            "run_status": run.status if run else "missing",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = asyncio.run(audit(args.paper_id, args.user_id, args.run_id))
    print(json.dumps(result, ensure_ascii=False))
    passed = bool(
        result["ownership_ok"]
        and result["page_count"]
        and result["chunk_count"]
        and result["citation_count"]
        and result["citation_count"] == result["valid_citation_count"]
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
