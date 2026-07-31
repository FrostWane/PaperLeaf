from paperleaf_api.evaluation_neural import CrossEncoderRerankIndex, NeuralRetrievalIndex
from paperleaf_api.evaluation_offline import OfflineRetrievalIndex
from paperleaf_api.rag.chunking import PageChunk


class FakeEmbedding:
    def passage_embed(self, texts: list[str]):
        mapping = {"relevant evidence": [1.0, 0.0], "unrelated appendix": [0.0, 1.0]}
        return iter(mapping[text] for text in texts)

    def query_embed(self, query: str):
        return iter([[1.0, 0.0]])


class FakeReranker:
    def rerank(self, query: str, documents: list[str]):
        return iter(1.0 if "relevant" in document else 0.0 for document in documents)


def _chunk(chunk_id: str, page: int, text: str) -> PageChunk:
    return PageChunk(
        id=chunk_id,
        paper_id="p1",
        physical_page=page,
        chunk_index=0,
        text=text,
        token_count=2,
    )


def test_neural_dense_and_hybrid_use_same_page_contract() -> None:
    base = OfflineRetrievalIndex(
        [
            _chunk("relevant", 2, "relevant evidence"),
            _chunk("unrelated", 7, "unrelated appendix"),
        ],
        dimensions=64,
    )
    index = NeuralRetrievalIndex(
        base,
        embedding_model=FakeEmbedding(),
        reranker=FakeReranker(),
    )

    dense = index.dense("relevant evidence", ["p1"], limit=2)
    hybrid = index.hybrid("relevant evidence", ["p1"], limit=2, rerank=True)

    assert dense.hits[0].chunk.physical_page == 2
    assert dense.quality is not None
    assert dense.quality.retrieval_grade == "sufficient"
    assert hybrid.hits[0].chunk.physical_page == 2


def test_cross_encoder_reranks_rrf_candidate_pool_without_dense_index() -> None:
    base = OfflineRetrievalIndex(
        [
            _chunk("relevant", 2, "relevant evidence"),
            _chunk("unrelated", 7, "unrelated appendix"),
        ],
        dimensions=64,
    )
    index = CrossEncoderRerankIndex(
        base,
        reranker=FakeReranker(),
        candidate_limit=2,
    )

    result = index.retrieve("evidence", ["p1"], limit=2)

    assert result.hits[0].chunk.id == "relevant"


def test_focus_window_prefers_late_query_terms_over_chunk_prefix() -> None:
    text = (
        "Background sentence. Another unrelated sentence. The compiler version is GCC 11. Results."
    )

    focused = CrossEncoderRerankIndex._focus_text("Which compiler version?", text)

    assert "compiler version" in focused
