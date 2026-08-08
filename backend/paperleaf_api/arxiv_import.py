"""受控 arXiv PDF 导入服务，供 API 与 Agent 人工确认复用。"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from .arxiv_service import fetch_arxiv_pdf, get_arxiv_paper
from .models import PaperStatus
from .repository import PaperRecord, Repository
from .storage import ObjectStorage, validate_pdf


async def import_arxiv_paper(
    arxiv_id: str,
    user_id: str,
    *,
    config: Any,
    repository: Repository,
    storage: ObjectStorage,
) -> Any:
    """只按校验后的 arXiv ID 从官方白名单下载，不信任模型提供的 URL。"""

    content_result, metadata_result = await asyncio.gather(
        fetch_arxiv_pdf(arxiv_id, config.max_pdf_bytes),
        get_arxiv_paper(arxiv_id),
        return_exceptions=True,
    )
    if isinstance(content_result, Exception):
        raise content_result
    content = content_result
    metadata = metadata_result if not isinstance(metadata_result, Exception) else None
    validate_pdf(content, f"{arxiv_id}.pdf", config.max_pdf_bytes)
    sha256 = hashlib.sha256(content).hexdigest()
    paper_id = str(uuid.uuid4())
    storage_key = f"{user_id}/{paper_id}/{sha256}.pdf"
    await storage.put(storage_key, content, "application/pdf")
    record = PaperRecord(
        id=paper_id,
        owner_id=user_id,
        title=(getattr(metadata, "title", None) or f"arXiv {arxiv_id}"),
        authors=list(getattr(metadata, "authors", None) or []),
        year=(
            int(metadata.published[:4])
            if getattr(metadata, "published", "")[:4].isdigit()
            else None
        ),
        abstract=getattr(metadata, "abstract", None),
        doi=None,
        publication=getattr(metadata, "journal_ref", None),
        arxiv_id=arxiv_id,
        filename=f"{arxiv_id}.pdf",
        storage_key=storage_key,
        mime_type="application/pdf",
        size_bytes=len(content),
        sha256=sha256,
        page_count=None,
        status=PaperStatus.queued,
    )
    try:
        return await repository.create_paper(record)
    except Exception:
        await storage.delete(storage_key)
        raise
