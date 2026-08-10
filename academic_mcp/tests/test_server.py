from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from academic_search_mcp import server


def test_http_client_request_logs_are_not_emitted_at_info() -> None:
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_openalex_search_converts_http_error_without_leaking_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "must-not-appear"
    request = httpx.Request(
        "GET", f"https://api.openalex.org/works?api_key={secret}"
    )

    async def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise httpx.ReadTimeout(f"request failed: {request.url}", request=request)

    monkeypatch.setenv("OPENALEX_API_KEY", secret)
    monkeypatch.setattr(server, "_get_json", fail)

    result = asyncio.run(server.search_openalex("DeepDTA", 3))

    assert result == {
        "source": "OpenAlex",
        "available": False,
        "error_code": "OPENALEX_TIMEOUT",
        "query": "DeepDTA",
        "results": [],
    }
    assert secret not in str(result)


def test_openalex_metadata_converts_auth_error_without_leaking_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "must-not-appear"
    request = httpx.Request(
        "GET", f"https://api.openalex.org/works/W123?api_key={secret}"
    )
    response = httpx.Response(401, request=request)

    async def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise httpx.HTTPStatusError(
            f"unauthorized: {request.url}", request=request, response=response
        )

    monkeypatch.setenv("OPENALEX_API_KEY", secret)
    monkeypatch.setattr(server, "_get_json", fail)

    result = asyncio.run(server.get_academic_metadata("W123", source="openalex"))

    assert result == {
        "source": "OpenAlex",
        "available": False,
        "error_code": "OPENALEX_AUTH_REQUIRED",
        "result": None,
    }
    assert secret not in str(result)


def test_openalex_search_passes_server_side_year_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def succeed(_url: str, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"results": []}

    monkeypatch.setenv("OPENALEX_API_KEY", "configured")
    monkeypatch.setattr(server, "_get_json", succeed)

    result = asyncio.run(server.search_openalex("drug target affinity", 5, 2026, 2026))

    assert result["available"] is True
    assert result["year_from"] == result["year_to"] == 2026
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["filter"] == (
        "from_publication_date:2026-01-01,to_publication_date:2026-12-31"
    )
