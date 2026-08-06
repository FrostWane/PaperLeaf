"""仅按公开 DOI 查询 Crossref 出版物元数据。

客户端固定访问 Crossref 官方 HTTPS API，不跟随重定向，也不会发送论文标题、作者、
摘要或用户信息。失败采用短期负缓存并返回 ``None``，避免阻断 PDF 解析任务。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import httpx

from .pdf_metadata import normalize_doi

_API_ORIGIN = "https://api.crossref.org"
_ALLOWED_HOST = "api.crossref.org"


@dataclass(frozen=True)
class _CacheEntry:
    value: str | None
    expires_at: float


class CrossrefPublicationCache:
    """可注入时钟的进程内 TTL 缓存，``None`` 表示已缓存的失败结果。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, doi: str) -> tuple[bool, str | None]:
        entry = self._entries.get(doi)
        if not entry:
            return False, None
        if entry.expires_at <= self._clock():
            self._entries.pop(doi, None)
            return False, None
        return True, entry.value

    def set(self, doi: str, value: str | None, ttl_seconds: float) -> None:
        self._entries[doi] = _CacheEntry(
            value=value,
            expires_at=self._clock() + max(ttl_seconds, 0),
        )


def _assert_official_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != _ALLOWED_HOST or parsed.port is not None:
        raise ValueError("Crossref 地址不在官方白名单内")


def _normalize_publication(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    publication = " ".join(value.split()).strip(" ,;:|.-–—")
    if not publication or len(publication) < 4 or len(publication) > 300:
        return None
    if publication.casefold() in {"arxiv", "arxiv.org", "unknown", "n/a"}:
        return None
    return publication


def _publication_from_payload(payload: object) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), dict):
        return None
    message = payload["message"]
    for key in ("container-title", "short-container-title"):
        values = message.get(key)
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            for value in values:
                if publication := _normalize_publication(value):
                    return publication
    event = message.get("event")
    if isinstance(event, dict):
        return _normalize_publication(event.get("name"))
    return None


class CrossrefClient:
    """受白名单和 TTL 缓存约束的 Crossref 查询器。"""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: CrossrefPublicationCache | None = None,
        timeout_seconds: float = 5,
        positive_ttl_seconds: float = 24 * 60 * 60,
        negative_ttl_seconds: float = 10 * 60,
    ) -> None:
        self._transport = transport
        self._cache = cache or CrossrefPublicationCache()
        self._timeout_seconds = timeout_seconds
        self._positive_ttl_seconds = positive_ttl_seconds
        self._negative_ttl_seconds = negative_ttl_seconds

    async def lookup_publication(self, doi_value: object) -> str | None:
        """按 DOI 查询出版物；参数无效或任何外部错误均安全降级为 ``None``。"""

        doi = normalize_doi(doi_value)
        if not doi:
            return None
        cached, value = self._cache.get(doi)
        if cached:
            return value

        url = f"{_API_ORIGIN}/works/{quote(doi, safe='')}"
        try:
            _assert_official_url(url)
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
                headers={"User-Agent": "PaperLeaf/1.0 (open-source literature manager)"},
            ) as client:
                response = await client.get(url)
            _assert_official_url(str(response.request.url))
            if response.status_code != httpx.codes.OK:
                publication = None
            else:
                publication = _publication_from_payload(response.json())
        except (httpx.HTTPError, ValueError, TypeError):
            publication = None

        self._cache.set(
            doi,
            publication,
            self._positive_ttl_seconds if publication else self._negative_ttl_seconds,
        )
        return publication


crossref_client = CrossrefClient()
