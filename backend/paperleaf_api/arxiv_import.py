"""受控 arXiv PDF 导入服务，供 API 与 Agent 人工确认复用。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import uuid
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .arxiv_service import fetch_arxiv_pdf, get_arxiv_paper
from .models import PaperStatus
from .repository import PaperRecord, Repository
from .storage import ObjectStorage, validate_pdf


async def _assert_public_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("开放 PDF 地址必须使用 HTTPS")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise ValueError("开放 PDF 地址无法解析") from error
    if not addresses:
        raise ValueError("开放 PDF 地址无法解析")
    for address in addresses:
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise ValueError("开放 PDF 地址指向了不允许的网络")


async def _fetch_open_access_pdf(url: str, max_bytes: int) -> bytes:
    """只下载 OpenAlex 复核后的公开 HTTPS PDF，并逐跳阻止 SSRF。"""

    current = url
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        for _ in range(4):
            await _assert_public_https_url(current)
            async with client.stream(
                "GET", current, headers={"User-Agent": "PaperLeaf/0.1"}
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("开放 PDF 重定向缺少目标地址")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                if "pdf" not in content_type and content_type:
                    raise ValueError("开放地址返回的不是 PDF")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("开放 PDF 超过允许大小")
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ValueError("开放 PDF 重定向次数过多")


async def import_open_access_paper(
    candidate: dict[str, Any],
    user_id: str,
    *,
    config: Any,
    repository: Repository,
    storage: ObjectStorage,
) -> Any:
    """导入经 MCP 重新核对的 DOI 开放 PDF，不信任模型给出的下载地址。"""

    doi = str(candidate.get("doi", "")).strip().removeprefix("https://doi.org/")
    pdf_url = str(candidate.get("open_access_pdf_url", "")).strip()
    if not doi or not pdf_url:
        raise ValueError("DOI 或开放 PDF 地址缺失")
    content = await _fetch_open_access_pdf(pdf_url, config.max_pdf_bytes)
    filename = f"{doi.replace('/', '_')}.pdf"
    validate_pdf(content, filename, config.max_pdf_bytes)
    sha256 = hashlib.sha256(content).hexdigest()
    paper_id = str(uuid.uuid4())
    storage_key = f"{user_id}/{paper_id}/{sha256}.pdf"
    await storage.put(storage_key, content, "application/pdf")
    year_value = candidate.get("year")
    authors = candidate.get("authors")
    record = PaperRecord(
        id=paper_id,
        owner_id=user_id,
        title=str(candidate.get("title") or doi),
        authors=[str(value) for value in authors[:50]]
        if isinstance(authors, list)
        else [],
        year=int(year_value) if str(year_value).isdigit() else None,
        abstract=str(candidate.get("abstract") or "") or None,
        doi=doi,
        publication=str(candidate.get("publication") or "") or None,
        arxiv_id=None,
        filename=filename,
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


async def import_public_paper(
    candidate: dict[str, Any],
    user_id: str,
    *,
    config: Any,
    repository: Repository,
    storage: ObjectStorage,
) -> Any:
    if candidate.get("arxiv_id"):
        return await import_arxiv_paper(
            str(candidate["arxiv_id"]),
            user_id,
            config=config,
            repository=repository,
            storage=storage,
        )
    return await import_open_access_paper(
        candidate,
        user_id,
        config=config,
        repository=repository,
        storage=storage,
    )


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
