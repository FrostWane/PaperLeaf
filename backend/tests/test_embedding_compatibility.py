import asyncio
import sys
from types import SimpleNamespace

from paperleaf_api import worker
from paperleaf_api.agent.tools import SQLLibrarySearch


class _EmbeddingRouter:
    provider = SimpleNamespace(
        embedding_model="qwen3-embedding:0.6b",
        api_key="ollama",
        base_url="http://ollama.example/v1",
    )

    def has_provider(self, purpose: str) -> bool:
        return purpose == "embedding"

    async def execute(self, purpose: str, operation):
        assert purpose == "embedding"
        return await operation(self.provider)


def test_worker_preserves_raw_chunk_strings_for_compatible_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeEmbeddings:
        def __init__(self, **kwargs) -> None:
            captured["kwargs"] = kwargs

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            captured["texts"] = texts
            return [[0.1, 0.2]]

    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(OpenAIEmbeddings=FakeEmbeddings),
    )

    vectors = asyncio.run(worker.embed_texts(["原始页级 Chunk"], router=_EmbeddingRouter()))

    assert vectors == [[0.1, 0.2]]
    assert captured["texts"] == ["原始页级 Chunk"]
    assert captured["kwargs"]["check_embedding_ctx_length"] is False


def test_worker_splits_large_document_embedding_into_bounded_batches(monkeypatch) -> None:
    batches: list[list[str]] = []

    class FakeEmbeddings:
        def __init__(self, **kwargs) -> None:
            assert kwargs["check_embedding_ctx_length"] is False

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            batches.append(texts)
            return [[float(index)] for index, _ in enumerate(texts)]

    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(OpenAIEmbeddings=FakeEmbeddings),
    )
    monkeypatch.setattr(
        worker,
        "settings",
        SimpleNamespace(embedding_dimensions=1024, embedding_batch_size=2),
    )

    vectors = asyncio.run(
        worker.embed_texts(["c1", "c2", "c3", "c4", "c5"], router=_EmbeddingRouter())
    )

    assert batches == [["c1", "c2"], ["c3", "c4"], ["c5"]]
    assert vectors is not None and len(vectors) == 5


def test_query_embedding_preserves_string_input_for_compatible_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeEmbeddings:
        def __init__(self, **kwargs) -> None:
            captured["kwargs"] = kwargs

        async def aembed_query(self, query: str) -> list[float]:
            captured["query"] = query
            return [0.3, 0.4]

    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(OpenAIEmbeddings=FakeEmbeddings),
    )
    search = SQLLibrarySearch(
        config=SimpleNamespace(embedding_dimensions=1024),
        model_router=_EmbeddingRouter(),
    )

    vector = asyncio.run(search._embed_query("中文语义问题"))

    assert vector == [0.3, 0.4]
    assert captured["query"] == "中文语义问题"
    assert captured["kwargs"]["dimensions"] == 1024
    assert captured["kwargs"]["check_embedding_ctx_length"] is False
