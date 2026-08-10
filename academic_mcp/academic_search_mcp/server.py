"""只读学术搜索 MCP。

模型只能提交查询词或公开文献标识，不能提交 URL。服务端固定访问 OpenAlex 与
Semantic Scholar 官方 API，并把所有外部字段当作不可信数据进行限长和规范化。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from typing import Any, Literal
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
OPENALEX_API = "https://api.openalex.org"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1"
_DOI_RE = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$", re.IGNORECASE)
_ARXIV_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.I)
_http_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()

# httpx 的 INFO 请求日志会打印完整查询字符串。OpenAlex 当前通过查询参数传递
# api_key，因此必须在进程启动时关闭该日志，避免凭据进入 Docker/集中日志。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

mcp = FastMCP(
    "PaperLeaf Academic Search",
    instructions="只返回公开学术元数据，结果不能替代论文原文证据。",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "academic-search-mcp:8080",
            "localhost:8080",
            "127.0.0.1:8080",
        ],
        allowed_origins=[
            "http://academic-search-mcp:8080",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
        ],
    ),
)
mcp.settings.streamable_http_path = "/mcp"

READ_ONLY_OPEN_WORLD = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _url(value: Any) -> str | None:
    candidate = _text(value, 1200)
    return candidate if candidate.startswith(("https://", "http://")) else None


def _abstract_from_index(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    ordered: list[tuple[int, str]] = []
    for word, positions in value.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and 0 <= position < 20_000:
                ordered.append((position, _text(word, 100)))
    ordered.sort(key=lambda item: item[0])
    return _text(" ".join(word for _, word in ordered), 4000)


async def _get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    global _http_client
    async with _client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    float(os.getenv("PAPERLEAF_ACADEMIC_HTTP_TIMEOUT_SECONDS", "12"))
                ),
                follow_redirects=False,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        client = _http_client
    response = await client.get(url, params=params, headers=headers)
    response.raise_for_status()
    declared = int(response.headers.get("content-length", "0") or 0)
    if declared > MAX_RESPONSE_BYTES or len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("ACADEMIC_RESPONSE_TOO_LARGE")
    if "json" not in response.headers.get("content-type", ""):
        raise ValueError("ACADEMIC_CONTENT_TYPE_INVALID")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("ACADEMIC_RESPONSE_INVALID")
    return payload


def _academic_error_code(error: Exception, source: str) -> str:
    prefix = source.upper().replace(" ", "_")
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 429:
            return f"{prefix}_RATE_LIMITED"
        if status in {401, 403}:
            return f"{prefix}_AUTH_REQUIRED"
        return f"{prefix}_HTTP_{status}"
    if isinstance(error, httpx.TimeoutException):
        return f"{prefix}_TIMEOUT"
    if isinstance(error, httpx.HTTPError):
        return f"{prefix}_UNAVAILABLE"
    return f"{prefix}_RESPONSE_INVALID"


def _openalex_item(item: dict[str, Any]) -> dict[str, Any]:
    external = item.get("ids") if isinstance(item.get("ids"), dict) else {}
    primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    source = primary.get("source") if isinstance(primary.get("source"), dict) else {}
    open_access = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}
    authorships = item.get("authorships") if isinstance(item.get("authorships"), list) else []
    authors = []
    for authorship in authorships[:20]:
        author = authorship.get("author") if isinstance(authorship, dict) else None
        if isinstance(author, dict) and author.get("display_name"):
            authors.append(_text(author["display_name"], 200))
    doi = _text(item.get("doi"), 300).removeprefix("https://doi.org/") or None
    arxiv_url = _text(external.get("arxiv"), 300) if isinstance(external, dict) else ""
    arxiv_id = arxiv_url.rsplit("/", 1)[-1] if arxiv_url else None
    return {
        "external_id": _text(item.get("id"), 300).rsplit("/", 1)[-1],
        "title": _text(item.get("display_name") or item.get("title"), 1000),
        "authors": authors,
        "year": (
            item.get("publication_year")
            if isinstance(item.get("publication_year"), int)
            else None
        ),
        "publication": _text(source.get("display_name"), 500) or None,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "url": _url(primary.get("landing_page_url") or item.get("id")),
        "open_access_pdf_url": _url(primary.get("pdf_url") or open_access.get("oa_url")),
        "abstract": _abstract_from_index(item.get("abstract_inverted_index")),
        "citation_count": max(0, int(item.get("cited_by_count") or 0)),
    }


def _semantic_item(item: dict[str, Any]) -> dict[str, Any]:
    external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
    open_pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
    authors = item.get("authors") if isinstance(item.get("authors"), list) else []
    return {
        "external_id": _text(item.get("paperId"), 200),
        "title": _text(item.get("title"), 1000),
        "authors": [
            _text(author.get("name"), 200)
            for author in authors[:20]
            if isinstance(author, dict) and author.get("name")
        ],
        "year": item.get("year") if isinstance(item.get("year"), int) else None,
        "publication": _text(item.get("venue"), 500) or None,
        "doi": _text(external.get("DOI"), 300) or None,
        "arxiv_id": _text(external.get("ArXiv"), 100) or None,
        "url": _url(item.get("url")),
        "open_access_pdf_url": _url(open_pdf.get("url")),
        "abstract": _text(item.get("abstract"), 4000),
        "citation_count": max(0, int(item.get("citationCount") or 0)),
    }


@mcp.tool(annotations=READ_ONLY_OPEN_WORLD, structured_output=True)
async def search_openalex(
    query: str,
    limit: int = 5,
    year_from: int | None = None,
    year_to: int | None = None,
) -> dict[str, Any]:
    """按自然语言查询 OpenAlex 公开论文元数据。"""

    normalized = _text(query, 500)
    requested = min(max(int(limit), 1), 10)
    if not normalized:
        raise ValueError("查询词不能为空")
    if year_from is not None and not 1900 <= int(year_from) <= 2100:
        raise ValueError("起始年份超出允许范围")
    if year_to is not None and not 1900 <= int(year_to) <= 2100:
        raise ValueError("结束年份超出允许范围")
    effective_from = int(year_from) if year_from is not None else None
    effective_to = int(year_to) if year_to is not None else None
    if effective_from and effective_to and effective_from > effective_to:
        raise ValueError("起始年份不能晚于结束年份")
    api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    if not api_key:
        return {
            "source": "OpenAlex",
            "available": False,
            "error_code": "OPENALEX_API_KEY_REQUIRED",
            "query": normalized,
            "results": [],
        }
    try:
        params: dict[str, Any] = {
            "search": normalized,
            "per-page": requested,
            "api_key": api_key,
            "select": (
                "id,display_name,authorships,publication_year,primary_location,ids,"
                "open_access,abstract_inverted_index,cited_by_count,doi"
            ),
        }
        filters: list[str] = []
        if effective_from:
            filters.append(f"from_publication_date:{effective_from}-01-01")
        if effective_to:
            filters.append(f"to_publication_date:{effective_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        payload = await _get_json(
            f"{OPENALEX_API}/works",
            params=params,
        )
    except (httpx.HTTPError, ValueError) as error:
        # 不把 httpx 异常继续抛给 ASGI/MCP 日志；其异常文本可能包含带 Key 的 URL。
        return {
            "source": "OpenAlex",
            "available": False,
            "error_code": _academic_error_code(error, "OpenAlex"),
            "query": normalized,
            "results": [],
        }
    rows = payload.get("results") if isinstance(payload.get("results"), list) else []
    return {
        "source": "OpenAlex",
        "available": True,
        "query": normalized,
        "year_from": effective_from,
        "year_to": effective_to,
        "results": [_openalex_item(item) for item in rows if isinstance(item, dict)][:requested],
    }


@mcp.tool(annotations=READ_ONLY_OPEN_WORLD, structured_output=True)
async def search_semantic_scholar(query: str, limit: int = 5) -> dict[str, Any]:
    """按自然语言查询 Semantic Scholar 公开论文元数据。"""

    normalized = _text(query.replace("-", " "), 500)
    requested = min(max(int(limit), 1), 10)
    if not normalized:
        raise ValueError("查询词不能为空")
    headers: dict[str, str] = {}
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    try:
        payload = await _get_json(
            f"{SEMANTIC_SCHOLAR_API}/paper/search",
            params={
                "query": normalized,
                "limit": requested,
                "fields": (
                    "title,authors,year,venue,abstract,url,externalIds,"
                    "openAccessPdf,citationCount"
                ),
            },
            headers=headers,
        )
    except (httpx.HTTPError, ValueError) as error:
        return {
            "source": "Semantic Scholar",
            "available": False,
            "error_code": _academic_error_code(error, "Semantic Scholar"),
            "query": normalized,
            "results": [],
        }
    rows = payload.get("data") if isinstance(payload.get("data"), list) else []
    return {
        "source": "Semantic Scholar",
        "available": True,
        "query": normalized,
        "results": [_semantic_item(item) for item in rows if isinstance(item, dict)][:requested],
    }


@mcp.tool(annotations=READ_ONLY_OPEN_WORLD, structured_output=True)
async def get_academic_metadata(
    identifier: str,
    source: Literal["auto", "openalex", "semantic_scholar"] = "auto",
) -> dict[str, Any]:
    """按 DOI、arXiv ID、OpenAlex ID 或 Semantic Scholar ID读取元数据。"""

    normalized = _text(identifier, 300)
    if not normalized:
        raise ValueError("文献标识不能为空")
    is_doi = bool(_DOI_RE.fullmatch(normalized.removeprefix("https://doi.org/")))
    is_arxiv = bool(_ARXIV_RE.fullmatch(normalized.removeprefix("arXiv:")))
    if source in {"auto", "semantic_scholar"}:
        prefix = (
            f"DOI:{normalized.removeprefix('https://doi.org/')}"
            if is_doi
            else f"ARXIV:{normalized.removeprefix('arXiv:')}"
            if is_arxiv
            else normalized
        )
        headers: dict[str, str] = {}
        api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
        if api_key:
            headers["x-api-key"] = api_key
        try:
            payload = await _get_json(
                f"{SEMANTIC_SCHOLAR_API}/paper/{quote(prefix, safe=':/.')}",
                params={
                    "fields": (
                        "title,authors,year,venue,abstract,url,externalIds,"
                        "openAccessPdf,citationCount"
                    )
                },
                headers=headers,
            )
            return {
                "source": "Semantic Scholar",
                "available": True,
                "result": _semantic_item(payload),
            }
        except (httpx.HTTPError, ValueError):
            if source == "semantic_scholar":
                raise
    api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    if not api_key:
        return {
            "source": "OpenAlex",
            "available": False,
            "error_code": "OPENALEX_API_KEY_REQUIRED",
            "result": None,
        }
    openalex_id = (
        f"https://doi.org/{normalized.removeprefix('https://doi.org/')}"
        if is_doi
        else normalized
    )
    try:
        payload = await _get_json(
            f"{OPENALEX_API}/works/{quote(openalex_id, safe=':/')}",
            params={"api_key": api_key},
        )
    except (httpx.HTTPError, ValueError) as error:
        return {
            "source": "OpenAlex",
            "available": False,
            "error_code": _academic_error_code(error, "OpenAlex"),
            "result": None,
        }
    return {"source": "OpenAlex", "available": True, "result": _openalex_item(payload)}


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "academic-search-mcp"})


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    global _http_client
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        async with _client_lock:
            if _http_client is not None and not _http_client.is_closed:
                await _http_client.aclose()
            _http_client = None


app = Starlette(
    routes=[Route("/health", health), Mount("/", app=mcp.streamable_http_app())],
    lifespan=lifespan,
)
