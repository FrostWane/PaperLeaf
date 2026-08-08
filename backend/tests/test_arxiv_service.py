"""arXiv Atom 元数据与精确 ID 查询的离线测试。"""

import asyncio

import httpx
import pytest

from paperleaf_api.arxiv_service import (
    _parse_arxiv_feed,
    get_arxiv_paper,
    search_related_arxiv,
)


def _feed(arxiv_id: str = "2401.01234v2") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>https://arxiv.org/abs/{arxiv_id}</id>
    <title> A reliable paper </title>
    <summary> Abstract with normalized spacing. </summary>
    <published>2024-01-03T00:00:00Z</published>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <arxiv:journal_ref>Journal of Reliable Systems 12 (2025) 1-9</arxiv:journal_ref>
  </entry>
</feed>""".encode()


def test_arxiv_feed_parses_journal_reference() -> None:
    papers = _parse_arxiv_feed(_feed())

    assert len(papers) == 1
    assert papers[0].arxiv_id == "2401.01234v2"
    assert papers[0].authors == ["Ada Lovelace", "Alan Turing"]
    assert papers[0].journal_ref == "Journal of Reliable Systems 12 (2025) 1-9"


def test_get_arxiv_paper_uses_id_list_and_exactly_matches_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "export.arxiv.org"
        assert request.url.params["id_list"] == "2401.01234v2"
        assert "search_query" not in request.url.params
        return httpx.Response(200, content=_feed("2401.01234v2"))

    paper = asyncio.run(
        get_arxiv_paper("2401.01234v2", transport=httpx.MockTransport(handler))
    )

    assert paper is not None
    assert paper.arxiv_id == "2401.01234v2"
    assert len(requests) == 1


def test_get_arxiv_paper_does_not_accept_mismatched_feed_entry() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=_feed("2401.99999"))
    )

    assert asyncio.run(get_arxiv_paper("2401.01234", transport=transport)) is None


def test_get_arxiv_paper_accepts_versioned_result_for_same_base_id() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=_feed("2401.01234v3"))
    )

    paper = asyncio.run(get_arxiv_paper("2401.01234", transport=transport))
    assert paper is not None
    assert paper.arxiv_id == "2401.01234v3"


def test_get_arxiv_paper_rejects_invalid_id_before_request() -> None:
    with pytest.raises(ValueError, match="arXiv ID 格式错误"):
        asyncio.run(get_arxiv_paper("2401.01234 OR all:*"))


def test_related_search_uses_server_generated_phrases_and_batch_offset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["search_query"] == (
            'all:"drug target" OR all:"binding affinity"'
        )
        assert request.url.params["start"] == "20"
        assert request.url.params["max_results"] == "6"
        return httpx.Response(200, content=_feed())

    papers = asyncio.run(
        search_related_arxiv(
            ["drug target", "binding affinity"],
            6,
            start=20,
            transport=httpx.MockTransport(handler),
        )
    )

    assert [paper.arxiv_id for paper in papers] == ["2401.01234v2"]
