import asyncio
import sys
from types import SimpleNamespace

from paperleaf_api import worker
from paperleaf_api.agent.tools import SQLLibrarySearch
from paperleaf_api.embedding_contract import (
    EMBEDDING_INPUT_FORMAT,
    configured_embedding_contract,
    contract_fingerprint,
)
from paperleaf_api.model_runtime import ModelProvider, ModelRouter, ModelRuntimeError


class _EmbeddingRouter:
    provider = SimpleNamespace(
        embedding_model="qwen3-embedding:0.6b",
        api_key="ollama",
        base_url="http://ollama.example/v1",
    )

    def has_provider(self, purpose: str) -> bool:
        return purpose == "embedding"

    async def execute(self, purpose: str, operation, **_kwargs):
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
    monkeypatch.setattr(
        worker,
        "settings",
        SimpleNamespace(
            embedding_provider="auto",
            embedding_dimensions=2,
            embedding_index_revision=1,
            embedding_batch_size=8,
        ),
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
        SimpleNamespace(embedding_dimensions=1, embedding_batch_size=2),
    )

    vectors = asyncio.run(
        worker.embed_texts(["c1", "c2", "c3", "c4", "c5"], router=_EmbeddingRouter())
    )

    assert batches == [["c1", "c2"], ["c3", "c4"], ["c5"]]
    assert vectors is not None and len(vectors) == 5


def test_worker_retries_only_failed_embedding_batch_with_dedicated_timeout(monkeypatch) -> None:
    calls = 0
    timeouts: list[float | None] = []

    class FakeEmbeddings:
        def __init__(self, **_kwargs) -> None:
            pass

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2] for _ in texts]

    class TransientRouter(_EmbeddingRouter):
        async def execute(self, purpose: str, operation, **kwargs):
            nonlocal calls
            calls += 1
            timeouts.append(kwargs.get("timeout_seconds"))
            if calls == 1:
                raise ModelRuntimeError("MODEL_TIMEOUT", [])
            return await operation(self.provider)

    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(OpenAIEmbeddings=FakeEmbeddings),
    )
    monkeypatch.setattr(
        worker,
        "settings",
        SimpleNamespace(
            embedding_provider="auto",
            embedding_dimensions=2,
            embedding_index_revision=1,
            embedding_batch_size=8,
            embedding_batch_attempts=2,
            embedding_timeout_seconds=90,
        ),
    )

    vectors = asyncio.run(worker.embed_texts(["c1", "c2"], router=TransientRouter()))

    assert vectors == [[0.1, 0.2], [0.1, 0.2]]
    assert calls == 2
    assert timeouts == [90, 90]


def test_query_embedding_preserves_string_input_for_compatible_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeEmbeddings:
        def __init__(self, **kwargs) -> None:
            captured["kwargs"] = kwargs

        async def aembed_query(self, query: str) -> list[float]:
            captured["query"] = query
            return [0.3] * 1024

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

    assert vector is not None and len(vector) == 1024
    assert captured["query"] == "中文语义问题"
    assert captured["kwargs"]["dimensions"] == 1024
    assert captured["kwargs"]["check_embedding_ctx_length"] is False


def test_paper_specific_queries_share_one_embedding_batch(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeEmbeddings:
        def __init__(self, **kwargs) -> None:
            captured["kwargs"] = kwargs

        async def aembed_documents(self, queries: list[str]) -> list[list[float]]:
            captured["queries"] = queries
            return [[float(index)] * 1024 for index, _ in enumerate(queries, 1)]

    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(OpenAIEmbeddings=FakeEmbeddings),
    )
    search = SQLLibrarySearch(
        config=SimpleNamespace(
            embedding_dimensions=1024,
            embedding_index_revision=2,
        ),
        model_router=_EmbeddingRouter(),
    )
    contract = configured_embedding_contract(search.config, search.model_router)
    assert contract is not None

    vectors = asyncio.run(
        search._embed_rewritten_queries(
            ["drug target affinity", "protein ligand binding", "drug target affinity"],
            contract=contract,
        )
    )

    assert captured["queries"] == ["drug target affinity", "protein ligand binding"]
    assert set(vectors) == {"drug target affinity", "protein ligand binding"}
    assert all(len(vector) == 1024 for vector in vectors.values())


def test_query_embedding_dimension_mismatch_falls_back_without_pgvector(monkeypatch) -> None:
    class FakeEmbeddings:
        def __init__(self, **_kwargs) -> None:
            pass

        async def aembed_query(self, _query: str) -> list[float]:
            return [0.3, 0.4]

    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(OpenAIEmbeddings=FakeEmbeddings),
    )
    search = SQLLibrarySearch(
        config=SimpleNamespace(embedding_dimensions=1024, embedding_index_revision=1),
        model_router=_EmbeddingRouter(),
    )

    vector = asyncio.run(search._embed_query("中文语义问题"))

    assert vector is None
    assert search.last_vector_fallback_reason == "query_dimension_mismatch"


def test_stored_vector_dimension_mismatch_is_detected_before_vector_query() -> None:
    class FakeSession:
        async def scalar(self, _statement):
            return 1

    search = SQLLibrarySearch(
        config=SimpleNamespace(embedding_dimensions=1024, embedding_index_revision=1),
        model_router=_EmbeddingRouter(),
    )
    request = SimpleNamespace(user_id="u1", paper_ids=["paper-1"])

    mismatched = asyncio.run(search._has_stored_dimension_mismatch(FakeSession(), request, 1024))

    assert mismatched is True


def test_stale_paper_in_scope_is_reported_as_contract_fallback() -> None:
    class FakeSession:
        async def scalar(self, _statement):
            return 1

    search = SQLLibrarySearch(
        config=SimpleNamespace(embedding_dimensions=1024, embedding_index_revision=1),
        model_router=_EmbeddingRouter(),
    )
    request = SimpleNamespace(user_id="u1", paper_ids=["paper-1"])
    contract = configured_embedding_contract(search.config, search.model_router)

    mismatched = asyncio.run(search._has_scope_contract_mismatch(FakeSession(), request, contract))

    assert mismatched is True


def test_verified_selection_uses_real_page_chunk_and_neighbors(monkeypatch) -> None:
    chunks = [
        SimpleNamespace(
            id=f"paper-1:p1:c{index}",
            physical_page=1,
            chunk_index=index,
            text=text,
        )
        for index, text in enumerate(
            [
                "Background and earlier work.",
                "We propose a deep-learning based model that uses only sequence information.",
                "The model predicts drug target binding affinity.",
            ]
        )
    ]
    paper = SimpleNamespace(id="paper-1", title="DeepDTA", chunking_strategy="structure_aware_v2")

    class FakeResult:
        def all(self):
            return [(chunk, paper) for chunk in chunks]

    class FakeSession:
        async def execute(self, _statement):
            return FakeResult()

    class FakeContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        "paperleaf_api.agent.tools.get_session_factory",
        lambda: lambda: FakeContext(),
    )
    search = SQLLibrarySearch(
        config=SimpleNamespace(embedding_dimensions=1024),
        model_router=_EmbeddingRouter(),
    )

    evidence = asyncio.run(
        search.page_selection_evidence(
            user_id="u1",
            paper_id="paper-1",
            physical_page=1,
            selected_text="propose a deep-learning based model that uses only seq",
        )
    )

    assert evidence[0].chunk_id == "paper-1:p1:c1"
    assert {item.chunk_id for item in evidence} == {
        "paper-1:p1:c0",
        "paper-1:p1:c1",
        "paper-1:p1:c2",
    }
    assert all(item.physical_page == 1 for item in evidence)


def test_embedding_contract_selects_explicit_provider_and_blocks_other_vector_spaces() -> None:
    router = ModelRouter(
        [
            ModelProvider(
                "primary",
                "key",
                "https://deepseek.example",
                "chat",
                "other-embedding",
            ),
            ModelProvider(
                "fallback",
                "ollama",
                "http://ollama.example/v1",
                "",
                "qwen3-embedding:0.6b",
            ),
        ]
    )
    config = SimpleNamespace(
        embedding_provider="fallback",
        embedding_dimensions=1024,
        embedding_index_revision=1,
    )
    contract = configured_embedding_contract(config, router)
    assert contract is not None
    assert contract.provider == "fallback"
    assert contract.model == "qwen3-embedding:0.6b"
    assert contract.input_format == EMBEDDING_INPUT_FORMAT
    assert contract.fingerprint != contract_fingerprint(
        contract.model,
        contract.dimensions,
        contract.revision,
        input_format="raw_chunk_v1",
    )

    invoked: list[str] = []

    async def operation(provider):
        invoked.append(provider.name)
        return [0.1] * 1024

    vector = asyncio.run(router.execute("embedding", operation, required_model=contract.model))

    assert len(vector) == 1024
    assert invoked == ["fallback"]
