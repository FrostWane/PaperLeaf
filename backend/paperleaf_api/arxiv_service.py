"""受白名单约束的 arXiv 检索与 PDF 下载。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx

_ARXIV_ID = re.compile(r"^[0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?$")
_ALLOWED_HOSTS = {"arxiv.org", "export.arxiv.org"}
_ATOM = {"a": "http://www.w3.org/2005/Atom"}


@dataclass(frozen=True)
class ArxivPaper:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    published: str
    pdf_url: str


def _normalize(value: str | None) -> str:
    return " ".join((value or "").split())


def _assert_allowed(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise ValueError("arXiv 返回了不允许的下载地址")


async def search_arxiv(query: str, limit: int = 10) -> list[ArxivPaper]:
    parameters = urlencode(
        {"search_query": f"all:{query}", "start": 0, "max_results": min(max(limit, 1), 20)}
    )
    url = f"https://export.arxiv.org/api/query?{parameters}"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "PaperLeaf/0.1"})
        response.raise_for_status()
    root = ET.fromstring(response.content)
    results: list[ArxivPaper] = []
    for entry in root.findall("a:entry", _ATOM):
        entry_id = _normalize(entry.findtext("a:id", namespaces=_ATOM)).rsplit("/", 1)[-1]
        base_id = entry_id
        if not _ARXIV_ID.fullmatch(base_id):
            continue
        pdf_url = f"https://arxiv.org/pdf/{base_id}.pdf"
        results.append(
            ArxivPaper(
                arxiv_id=base_id,
                title=_normalize(entry.findtext("a:title", namespaces=_ATOM)),
                authors=[
                    _normalize(node.findtext("a:name", namespaces=_ATOM))
                    for node in entry.findall("a:author", _ATOM)
                ],
                abstract=_normalize(entry.findtext("a:summary", namespaces=_ATOM)),
                published=_normalize(entry.findtext("a:published", namespaces=_ATOM)),
                pdf_url=pdf_url,
            )
        )
    return results


async def fetch_arxiv_pdf(arxiv_id: str, max_bytes: int) -> bytes:
    if not _ARXIV_ID.fullmatch(arxiv_id):
        raise ValueError("arXiv ID 格式错误")
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    _assert_allowed(url)
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "PaperLeaf/0.1"}) as response:
            response.raise_for_status()
            _assert_allowed(str(response.url))
            content = bytearray()
            async for block in response.aiter_bytes():
                content.extend(block)
                if len(content) > max_bytes:
                    raise ValueError("arXiv PDF 超过大小限制")
    return bytes(content)

