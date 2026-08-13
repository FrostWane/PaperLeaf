from paperleaf_api.evaluation import EvaluationCase
from paperleaf_api.evaluation_offline import (
    OfflineRetrievalIndex,
    QueryRanking,
    ScoredChunk,
    calibrate_abstention,
)
from paperleaf_api.rag.chunking import PageChunk


def _chunk(chunk_id: str, paper_id: str, page: int, text: str) -> PageChunk:
    return PageChunk(
        id=chunk_id,
        paper_id=paper_id,
        physical_page=page,
        chunk_index=0,
        text=text,
        token_count=len(text.split()),
    )


def test_offline_channels_retrieve_relevant_page_without_model_key() -> None:
    chunks = [
        _chunk("p1-1", "p1", 1, "Residual learning uses identity shortcut connections."),
        _chunk("p1-2", "p1", 2, "The training dataset contains many images."),
    ]
    index = OfflineRetrievalIndex(chunks, dimensions=256)

    vector = index.hashing_vector("identity shortcut", ["p1"], limit=2)
    keyword = index.bm25("identity shortcut", ["p1"], limit=2)

    assert vector.hits[0].chunk.physical_page == 1
    assert keyword.hits[0].chunk.physical_page == 1


def test_page_dedup_and_scope_diversity_keep_both_papers() -> None:
    p1_page1 = _chunk("p1-1", "p1", 1, "alpha")
    p1_page2 = _chunk("p1-2", "p1", 2, "alpha")
    p2_page1 = _chunk("p2-1", "p2", 1, "alpha")
    hits = [
        ScoredChunk(p1_page1, 3.0),
        ScoredChunk(p1_page2, 2.0),
        ScoredChunk(p2_page1, 1.0),
    ]

    diversified = OfflineRetrievalIndex._diversify_scope(hits, paper_ids=["p1", "p2"], limit=3)

    assert [hit.chunk.paper_id for hit in diversified[:2]] == ["p1", "p2"]


def test_per_paper_fusion_builds_independent_rankings_before_merge(monkeypatch) -> None:
    p1 = _chunk("p1-1", "p1", 1, "alpha")
    p2 = _chunk("p2-1", "p2", 2, "beta")
    index = OfflineRetrievalIndex([p1, p2], dimensions=256)
    scopes: list[tuple[str, ...]] = []

    def fused(_query, paper_ids, **_kwargs):
        scopes.append(tuple(paper_ids))
        chunk = p1 if paper_ids == ["p1"] else p2
        return QueryRanking([ScoredChunk(chunk, 1.0)], 1.0)

    monkeypatch.setattr(index, "fused", fused)
    result = index.per_paper_fused("compare", ["p1", "p2"], limit=2)

    assert scopes == [("p1",), ("p2",)]
    assert [hit.chunk.paper_id for hit in result.hits] == ["p1", "p2"]


def test_window_bm25_promotes_short_fact_inside_long_chunk() -> None:
    filler = "background " * 180
    chunks = [
        _chunk("p1-1", "p1", 1, f"{filler} compiler version GCC eleven {filler}"),
        _chunk("p1-2", "p1", 2, "compiler systems background"),
    ]
    index = OfflineRetrievalIndex(chunks, dimensions=256)

    result = index.window_bm25("Which compiler version uses GCC eleven?", ["p1"], limit=2)

    assert result.hits[0].chunk.physical_page == 1


def test_multigranular_rrf_fuses_channels_by_physical_page() -> None:
    chunks = [
        _chunk("p1-1-a", "p1", 1, "identity shortcut residual learning"),
        PageChunk(
            id="p1-1-b",
            paper_id="p1",
            physical_page=1,
            chunk_index=1,
            text="residual networks use identity mappings",
            token_count=5,
        ),
        _chunk("p1-2", "p1", 2, "unrelated training images"),
    ]
    index = OfflineRetrievalIndex(chunks, dimensions=256)

    result = index.multigranular_fused("identity residual", ["p1"], limit=2)

    assert result.hits[0].chunk.physical_page == 1
    assert len({hit.chunk.physical_page for hit in result.hits}) == len(result.hits)


def test_adaptive_fusion_only_boosts_vector_when_lexical_coverage_is_low(
    monkeypatch,
) -> None:
    chunk = _chunk("p1-1", "p1", 1, "identity shortcut residual learning")
    index = OfflineRetrievalIndex([chunk], dimensions=256)
    calls: list[str] = []

    monkeypatch.setattr(
        index,
        "bm25",
        lambda *_args, **_kwargs: QueryRanking([ScoredChunk(chunk, 1.0)], 0.25),
    )
    monkeypatch.setattr(
        index,
        "multigranular_fused",
        lambda *_args, **_kwargs: calls.append("vector3") or QueryRanking([], 0.0),
    )
    monkeypatch.setattr(
        index,
        "fused",
        lambda *_args, **_kwargs: calls.append("baseline") or QueryRanking([], 0.0),
    )

    index.adaptive_fused("identity shortcut", ["p1"], limit=5)
    assert calls == ["vector3"]

    calls.clear()
    monkeypatch.setattr(
        index,
        "bm25",
        lambda *_args, **_kwargs: QueryRanking([ScoredChunk(chunk, 1.0)], 0.26),
    )
    index.adaptive_fused("identity shortcut", ["p1"], limit=5)
    assert calls == ["baseline"]


def test_abstention_calibration_prioritizes_zero_dev_false_answers() -> None:
    cases = [
        EvaluationCase(
            id="answerable-high",
            query="q",
            paper_ids=["p1"],
            answerable=True,
            category="fact",
            split="dev",
        ),
        EvaluationCase(
            id="answerable-low",
            query="q",
            paper_ids=["p1"],
            answerable=True,
            category="fact",
            split="dev",
        ),
        EvaluationCase(
            id="unanswerable",
            query="q",
            paper_ids=["p1"],
            answerable=False,
            category="unanswerable",
            split="dev",
        ),
    ]
    result = calibrate_abstention(
        cases,
        {"answerable-high": 0.9, "answerable-low": 0.4, "unanswerable": 0.6},
    )

    assert result["dev_unanswerable_correct"] == 1
    assert result["dev_answerable_correct"] == 1
    assert float(result["threshold"]) > 0.6
