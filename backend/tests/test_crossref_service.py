"""Crossref DOI 查询器的离线测试。"""

import asyncio

import httpx

from paperleaf_api.crossref_service import CrossrefClient, CrossrefPublicationCache


def test_crossref_returns_container_title_and_uses_positive_cache() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "api.crossref.org"
        assert request.url.path.startswith("/works/10.1093/")
        return httpx.Response(
            200,
            json={"message": {"container-title": [" Bioinformatics "]}},
        )

    client = CrossrefClient(transport=httpx.MockTransport(handler))

    async def scenario() -> None:
        assert await client.lookup_publication("10.1093/BIOINFORMATICS/BTY593") == "Bioinformatics"
        publication = await client.lookup_publication(
            "https://doi.org/10.1093/bioinformatics/bty593"
        )
        assert publication == "Bioinformatics"

    asyncio.run(scenario())
    assert len(calls) == 1


def test_crossref_timeout_is_negative_cached_and_expires() -> None:
    now = [100.0]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    cache = CrossrefPublicationCache(clock=lambda: now[0])
    client = CrossrefClient(
        transport=httpx.MockTransport(handler),
        cache=cache,
        negative_ttl_seconds=10,
    )

    async def scenario() -> None:
        assert await client.lookup_publication("10.1000/timeout") is None
        assert await client.lookup_publication("10.1000/timeout") is None
        now[0] += 11
        assert await client.lookup_publication("10.1000/timeout") is None

    asyncio.run(scenario())
    assert calls == 2


def test_crossref_http_and_payload_errors_degrade_without_raising() -> None:
    responses = iter(
        [
            httpx.Response(503, json={"message": "unavailable"}),
            httpx.Response(200, content=b"not-json"),
            httpx.Response(200, json={"message": {"container-title": []}}),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = CrossrefClient(transport=httpx.MockTransport(handler), negative_ttl_seconds=0)

    async def scenario() -> None:
        assert await client.lookup_publication("10.1000/http-error") is None
        assert await client.lookup_publication("10.1000/json-error") is None
        assert await client.lookup_publication("10.1000/empty") is None
        assert await client.lookup_publication("not-a-doi") is None

    asyncio.run(scenario())


def test_crossref_uses_event_name_for_proceedings() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "message": {
                    "container-title": [],
                    "event": {"name": "International Conference on Learning Representations"},
                }
            },
        )
    )
    client = CrossrefClient(transport=transport)

    assert (
        asyncio.run(client.lookup_publication("10.1000/proceedings"))
        == "International Conference on Learning Representations"
    )
