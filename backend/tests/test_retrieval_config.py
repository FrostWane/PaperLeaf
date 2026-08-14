import asyncio

import pytest

from paperleaf_api.agent.tools import LibrarySearchInput, SQLLibrarySearch
from paperleaf_api.config import settings
from paperleaf_api.rag.retrieval_config import (
    freeze_retrieval_config,
    resolve_git_sha,
    retrieval_config_overlay,
)


def test_frozen_retrieval_config_has_verified_git_sha_and_stable_fingerprint() -> None:
    first = freeze_retrieval_config(settings)
    second = freeze_retrieval_config(settings)

    assert first == second
    assert first["schema_version"] == 1
    assert first["git_sha_verified"] is True
    assert len(first["git_sha"]) == 40
    assert len(first["fingerprint"]) == 64
    assert first["embedding"]["index_revision"] == settings.embedding_index_revision
    assert resolve_git_sha()[0] == first["git_sha"]


def test_retrieval_config_rejects_tampering() -> None:
    snapshot = freeze_retrieval_config(settings)
    snapshot["candidate_pool_size"] = 999

    with pytest.raises(ValueError, match="指纹不一致"):
        retrieval_config_overlay(settings, snapshot)


def test_sql_library_search_reads_run_frozen_config(monkeypatch) -> None:
    snapshot = freeze_retrieval_config(settings)
    snapshot["candidate_pool_size"] = 17
    snapshot.pop("fingerprint")
    # 修改配置后重新冻结指纹，模拟 Run 创建时与 Worker 当前环境不同。
    import hashlib
    import json

    snapshot["fingerprint"] = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    search = SQLLibrarySearch()
    observed: list[int] = []

    async def fake_search(_request):
        observed.append(int(search.config.rag_candidate_pool_size))
        return []

    monkeypatch.setattr(search, "_search", fake_search)
    asyncio.run(
        search(
            LibrarySearchInput(
                user_id="user",
                query="query",
                retrieval_config=snapshot,
            )
        )
    )

    assert observed == [17]
    assert search.config.rag_candidate_pool_size == settings.rag_candidate_pool_size
