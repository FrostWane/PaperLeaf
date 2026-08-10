from types import SimpleNamespace

from paperleaf_api.agent.recommendation_quality import (
    entity_keys,
    filter_and_deduplicate_candidates,
    is_research_paper,
    passes_relevance_gate,
    rank_academic_candidates,
)


def test_filters_non_papers_attachments_and_retracted_works() -> None:
    candidates = [
        {"title": "A valid paper", "work_type": "article", "doi": "10.1/valid"},
        {"title": "A benchmark dataset", "work_type": "dataset"},
        {"title": "Figure 2", "work_type": "article"},
        {"title": "Retracted paper", "work_type": "article", "is_retracted": True},
        {"title": "Supplement.png", "work_type": "article"},
    ]

    kept, stats = filter_and_deduplicate_candidates(candidates)

    assert [item["title"] for item in kept] == ["A valid paper"]
    assert stats == {"input": 5, "type_filtered": 4, "duplicate_filtered": 0}


def test_entity_dedup_uses_doi_arxiv_external_id_and_title() -> None:
    excluded = {
        "doi:10.1000/existing",
        "title:alreadyinthelibrary",
        "arxiv:2401.12345",
    }
    candidates = [
        {"title": "Different title", "doi": "https://doi.org/10.1000/EXISTING"},
        {"title": "Already in the library"},
        {"title": "Versioned arXiv", "arxiv_id": "2401.12345v2"},
        {"title": "New work", "external_id": "W1", "source": "OpenAlex"},
        {"title": "New work duplicate", "external_id": "W1", "source": "OpenAlex"},
    ]

    kept, stats = filter_and_deduplicate_candidates(candidates, excluded_keys=excluded)

    assert [item["title"] for item in kept] == ["New work"]
    assert stats["duplicate_filtered"] == 4
    assert "external:openalex:w1" in entity_keys(kept[0])


def test_whole_library_short_title_excludes_provider_subtitle_variant() -> None:
    kept, stats = filter_and_deduplicate_candidates(
        [
            {
                "title": "AttentionDTA: prediction of drug target binding affinity",
                "external_id": "W-provider-only",
            }
        ],
        excluded_keys={"title:attentiondta"},
    )

    assert kept == []
    assert stats["duplicate_filtered"] == 1


def test_title_and_abstract_semantic_rerank_is_deterministic() -> None:
    candidates = [
        {"title": "Image diffusion model", "abstract": "text to image generation"},
        {
            "title": "Protein ligand affinity prediction",
            "abstract": "drug target binding with molecular sequences",
        },
    ]
    scope = ["DeepDTA\ndrug target binding affinity from protein and drug sequences"]
    # 两个候选 + 一个作用域向量；第二个候选与作用域同向。
    embeddings = [[0.0, 1.0], [1.0, 0.0], [1.0, 0.0]]

    first = rank_academic_candidates(candidates, scope, embeddings=embeddings)
    second = rank_academic_candidates(candidates, scope, embeddings=embeddings)

    assert [item["title"] for item in first] == [
        "Protein ligand affinity prediction",
        "Image diffusion model",
    ]
    assert first == second
    assert first[0]["rerank_mode"] == "semantic_lexical"


def test_missing_type_is_kept_for_backwards_compatible_providers() -> None:
    assert is_research_paper({"title": "Normal scholarly title"}) == (True, None)


def test_semantic_score_cannot_bypass_domain_anchor_gate() -> None:
    assert passes_relevance_gate(
        {
            "rerank_mode": "semantic_lexical",
            "lexical_score": 0.039841,
            "semantic_score": 0.518745,
        }
    ) is False
    assert passes_relevance_gate(
        {
            "rerank_mode": "semantic_lexical",
            "lexical_score": 0.083149,
            "semantic_score": 0.621572,
        }
    ) is True


def test_collection_consensus_rejects_single_outlier_topic() -> None:
    candidates = [
        {
            "title": "Speech autoregressive generation",
            "abstract": "semantic tokens for speech generation",
        },
        {
            "title": "Graph drug target affinity",
            "abstract": "protein ligand binding prediction",
        },
    ]
    scope = [
        "DeepDTA\ndrug target affinity with protein sequences",
        "AttentionDTA\ndrug target affinity with attention",
        "GraphDTA\ndrug target affinity with molecular graphs",
        "DrugGen\ndrug discovery and molecule generation",
        "AR-RAG\nautoregressive image generation",
    ]
    # 候选向量都被故意设为高相似，验证单篇离群主题不能仅靠 Embedding 放行。
    embeddings = [[1.0, 0.0], [1.0, 0.0], *[[1.0, 0.0] for _ in scope]]

    ranked = rank_academic_candidates(candidates, scope, embeddings=embeddings)
    by_title = {item["title"]: item for item in ranked}

    assert passes_relevance_gate(by_title["Speech autoregressive generation"]) is False
    assert passes_relevance_gate(by_title["Graph drug target affinity"]) is True


def test_imported_provider_id_matches_future_candidate_across_sessions() -> None:
    stored = SimpleNamespace(
        title="Imported work",
        doi=None,
        arxiv_id=None,
        academic_external_ids={"semantic_scholar": "CorpusId:123"},
    )
    candidate = {
        "title": "A provider title variant",
        "external_id": "corpusid:123",
        "source": "Semantic Scholar",
    }

    assert entity_keys(stored) & entity_keys(candidate)
