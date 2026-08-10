from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from paperleaf_api import arxiv_import
from paperleaf_api.repository import MemoryRepository
from paperleaf_api.storage import LocalObjectStorage


def test_open_access_import_rejects_private_network_pdf_url() -> None:
    with pytest.raises(ValueError, match="不允许的网络"):
        asyncio.run(arxiv_import._assert_public_https_url("https://127.0.0.1/paper.pdf"))


def test_open_access_import_persists_revalidated_doi_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    valid_pdf_bytes: bytes,
) -> None:
    async def fake_fetch(url: str, max_bytes: int) -> bytes:
        assert url == "https://publisher.example/open.pdf"
        assert max_bytes == 1024 * 1024
        return valid_pdf_bytes

    monkeypatch.setattr(arxiv_import, "_fetch_open_access_pdf", fake_fetch)

    async def scenario() -> None:
        repository = MemoryRepository("secret")
        paper = await arxiv_import.import_open_access_paper(
            {
                "title": "Verified Open Paper",
                "authors": ["Author One"],
                "year": 2026,
                "publication": "Open Journal",
                "doi": "10.1000/verified.paper",
                "abstract": "Verified metadata.",
                "source": "OpenAlex",
                "external_id": "W123",
                "open_access_pdf_url": "https://publisher.example/open.pdf",
            },
            "u1",
            config=SimpleNamespace(max_pdf_bytes=1024 * 1024),
            repository=repository,
            storage=LocalObjectStorage(tmp_path),
        )

        assert paper.title == "Verified Open Paper"
        assert paper.doi == "10.1000/verified.paper"
        assert paper.arxiv_id is None
        assert paper.publication == "Open Journal"
        assert paper.academic_external_ids == {"openalex": "W123"}

    asyncio.run(scenario())
