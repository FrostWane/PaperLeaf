"""个人文献库驱动的发现推荐测试。"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from paperleaf_api.arxiv_service import ArxivPaper
from paperleaf_api.discovery import (
    build_discovery_profile,
    collect_recommendations,
    rank_recommendations,
    with_indexed_text,
)


def _paper(
    paper_id: str,
    title: str,
    abstract: str,
    *,
    days_ago: int,
    arxiv_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=paper_id,
        title=title,
        abstract=abstract,
        publication=None,
        arxiv_id=arxiv_id,
        last_opened_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def _candidate(arxiv_id: str, title: str, abstract: str) -> ArxivPaper:
    return ArxivPaper(
        arxiv_id=arxiv_id,
        title=title,
        authors=["Researcher"],
        abstract=abstract,
        published="2026-01-01T00:00:00Z",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
    )


def test_profile_rotates_library_seeds_and_advances_arxiv_page() -> None:
    papers = [
        _paper(
            "p1",
            "DeepDTA drug-target binding affinity",
            "protein ligand interaction",
            days_ago=0,
        ),
        _paper(
            "p2",
            "Autoregressive retrieval augmentation for image generation",
            "retrieval generation",
            days_ago=1,
        ),
    ]

    first = build_discovery_profile(papers, 0)
    second = build_discovery_profile(papers, 1)
    third = build_discovery_profile(papers, 2)

    assert first and second and third
    assert first.seed.id == "p1"
    assert first.search_phrases[:2] == ("drug target", "binding affinity")
    assert second.seed.id == "p2"
    assert third.seed.id == "p1"
    assert third.search_start == 20


def test_short_model_title_uses_domain_phrases_from_abstract() -> None:
    profile = build_discovery_profile(
        [
            _paper(
                "p1",
                "DeepDTA",
                "Deep drug-target binding affinity prediction with convolutional networks",
                days_ago=0,
            )
        ],
        0,
    )

    assert profile
    assert profile.search_phrases[:2] == ("drug target", "binding affinity")


def test_missing_abstract_uses_owned_indexed_text_without_mutating_record() -> None:
    source = _paper("p1", "DeepDTA", "", days_ago=0)
    enriched = with_indexed_text(
        [source],
        {"p1": "Drug-target binding affinity prediction with protein sequences."},
    )
    profile = build_discovery_profile(enriched, 0)

    assert source.abstract == ""
    assert enriched[0].abstract.startswith("Drug-target")
    assert profile
    assert profile.search_phrases[:2] == ("drug target", "binding affinity")


def test_rank_uses_semantic_similarity_and_excludes_existing_or_seen_papers() -> None:
    papers = [
        _paper(
            "p1",
            "Drug target binding affinity",
            "protein ligand prediction",
            days_ago=0,
            arxiv_id="2401.00001v2",
        ),
        _paper(
            "p2",
            "Retrieval augmented image generation",
            "autoregressive retrieval",
            days_ago=1,
        ),
    ]
    profile = build_discovery_profile(papers, 0)
    assert profile
    candidates = [
        _candidate("2401.00001v3", "Already imported", "duplicate"),
        _candidate("2402.00002", "Neural binding affinity estimation", "drug target interaction"),
        _candidate(
            "2403.00003",
            "Retrieval guided visual generation",
            "image retrieval generation",
        ),
        _candidate("2404.00004", "Previously shown", "drug target"),
    ]
    # 去重后候选为 2402、2403；随后是两篇文献库论文向量。
    embeddings = [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]]

    ranked = rank_recommendations(
        profile,
        candidates,
        excluded_arxiv_ids={"2404.00004"},
        embeddings=embeddings,
        limit=6,
    )

    assert [item.paper.arxiv_id for item in ranked] == ["2402.00002", "2403.00003"]
    assert ranked[0].matched_paper_title == "Drug target binding affinity"
    assert ranked[1].matched_paper_title == "Retrieval augmented image generation"
    assert all(item.match_type == "semantic" for item in ranked)


def test_embedding_failure_falls_back_to_deterministic_keyword_ranking() -> None:
    papers = [_paper("p1", "Graph neural network for molecules", "molecular property", days_ago=0)]
    profile = build_discovery_profile(papers, 0)
    assert profile
    candidates = [
        _candidate("2501.00001", "Graph networks for molecular property prediction", "molecules"),
        _candidate("2501.00002", "Language models for poetry", "text generation"),
    ]

    async def unavailable(_config, _router, _texts):
        return None

    first, first_strategy = asyncio.run(
        collect_recommendations(
            profile,
            candidates,
            config=SimpleNamespace(),
            model_router=SimpleNamespace(),
            excluded_arxiv_ids=set(),
            limit=6,
            embedder=unavailable,
        )
    )
    second, second_strategy = asyncio.run(
        collect_recommendations(
            profile,
            candidates,
            config=SimpleNamespace(),
            model_router=SimpleNamespace(),
            excluded_arxiv_ids=set(),
            limit=6,
            embedder=unavailable,
        )
    )

    assert first_strategy == second_strategy == "keyword"
    assert [item.paper.arxiv_id for item in first] == [item.paper.arxiv_id for item in second]
    assert first[0].paper.arxiv_id == "2501.00001"
