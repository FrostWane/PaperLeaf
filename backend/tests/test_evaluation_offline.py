from paperleaf_api.evaluation import EvaluationCase
from paperleaf_api.evaluation_offline import (
    OfflineRetrievalIndex,
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


def test_window_bm25_promotes_short_fact_inside_long_chunk() -> None:
    filler = "background " * 180
    chunks = [
        _chunk("p1-1", "p1", 1, f"{filler} compiler version GCC eleven {filler}"),
        _chunk("p1-2", "p1", 2, "compiler systems background"),
    ]
    index = OfflineRetrievalIndex(chunks, dimensions=256)

    result = index.window_bm25("Which compiler version uses GCC eleven?", ["p1"], limit=2)

    assert result.hits[0].chunk.physical_page == 1


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
