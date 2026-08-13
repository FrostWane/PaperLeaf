import asyncio

import httpx

from paperleaf_api.evaluation_corpus_prepare import CorpusPreparer


class _RetryClient:
    def __init__(self) -> None:
        self.calls = 0

    async def post(self, path, *, headers, json):
        del headers, json
        self.calls += 1
        if self.calls == 1:
            raise httpx.ReadTimeout("timeout", request=httpx.Request("POST", path))
        return httpx.Response(201, request=httpx.Request("POST", path))


def test_corpus_prepare_retries_request_timeout(monkeypatch) -> None:
    preparer = CorpusPreparer("http://api:8000", 60)
    client = _RetryClient()
    preparer.client = client  # type: ignore[assignment]

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    response = asyncio.run(
        preparer.post_with_retry("/import", payload={"arxiv_id": "1234.5678v1"})
    )
    assert response.status_code == 201
    assert client.calls == 2
