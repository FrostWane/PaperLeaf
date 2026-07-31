from paperleaf_api.rag.citations import Evidence
from paperleaf_api.rag.retrieval_quality import (
    AnswerSupport,
    EvidenceQualityPolicy,
    apply_answer_support,
    assess_evidence,
    deduplicate_evidence_by_page,
    lexical_coverage,
)


def _evidence(
    chunk_id: str,
    page: int,
    text: str,
    *,
    channels: tuple[str, ...] = (),
    scores: tuple[tuple[str, float], ...] = (),
    retrieval_score: float = 0.0,
) -> Evidence:
    return Evidence(
        chunk_id=chunk_id,
        paper_id="paper-1",
        paper_title="测试论文",
        physical_page=page,
        text=text,
        retrieval_score=retrieval_score,
        retrieval_channels=channels,
        channel_scores=scores,
    )


def test_page_dedup_merges_channel_signals_and_keeps_page_order() -> None:
    evidence = [
        _evidence(
            "keyword-page-2",
            2,
            "retrieval augmented generation",
            channels=("keyword",),
            scores=(("keyword", 0.2),),
            retrieval_score=0.04,
        ),
        _evidence(
            "vector-page-2",
            2,
            "RAG combines two memories",
            channels=("vector",),
            scores=(("vector", 0.72),),
            retrieval_score=0.03,
        ),
        _evidence("page-5", 5, "A separate result", retrieval_score=0.02),
    ]

    result = deduplicate_evidence_by_page(evidence, limit=2)

    assert [item.physical_page for item in result] == [2, 5]
    assert result[0].chunk_id == "keyword-page-2"
    assert result[0].retrieval_channels == ("keyword", "vector")
    assert dict(result[0].channel_scores) == {"keyword": 0.2, "vector": 0.72}


def test_quality_accepts_bilingual_terms_and_channel_agreement() -> None:
    evidence = [
        _evidence(
            "c1",
            3,
            "The retriever combines parametric and non-parametric memory.",
            channels=("keyword", "vector"),
            scores=(("keyword", 0.2), ("vector", 0.62)),
        )
    ]

    quality = assess_evidence("How does the retriever combine memory?", evidence)

    assert lexical_coverage("检索器如何融合外部证据", "检索器融合外部证据后回答") > 0.5
    assert quality.grade == "sufficient"
    assert quality.reason_code == "channel_agreement"
    assert quality.confidence > 0.5


def test_quality_allows_strong_cross_language_vector_without_term_overlap() -> None:
    evidence = [
        _evidence(
            "c1",
            3,
            "RAG combines parametric and non-parametric memory.",
            channels=("vector",),
            scores=(("vector", 0.68),),
        )
    ]

    quality = assess_evidence("这种方法如何利用外部知识？", evidence)

    assert quality.grade == "sufficient"
    assert quality.reason_code == "semantic_support"


def test_quality_rejects_nonempty_but_irrelevant_evidence() -> None:
    evidence = [_evidence("c1", 7, "The appendix lists hardware configurations.")]

    quality = assess_evidence("模型的主要结论是什么？", evidence)

    assert quality.grade == "insufficient"
    assert quality.reason_code == "weak_match"
    assert quality.confidence == 0


def test_quality_policy_can_raise_release_gate() -> None:
    evidence = [
        _evidence(
            "c1",
            3,
            "retrieval evidence",
            channels=("vector",),
            scores=(("vector", 0.5),),
        )
    ]

    quality = assess_evidence(
        "retrieval evidence",
        evidence,
        policy=EvidenceQualityPolicy(min_confidence=0.8, min_vector_score=0.7),
    )

    assert quality.grade == "insufficient"


def test_answer_support_is_separate_from_retrieval_relevance() -> None:
    evidence = [
        _evidence(
            "c1",
            3,
            "Chain-of-thought prompting improves arithmetic reasoning.",
            channels=("vector",),
            scores=(("vector", 0.7),),
        )
    ]
    retrieval_quality = assess_evidence(
        "Which compiler version executes chain-of-thought prompts?", evidence
    )

    final_quality = apply_answer_support(
        retrieval_quality,
        AnswerSupport(False, 0.96, "answer_not_supported"),
    )

    assert retrieval_quality.retrieval_grade == "sufficient"
    assert final_quality.grade == "insufficient"
    assert final_quality.answer_support_grade == "unsupported"
    assert "没有通过逐条证据核验" in final_quality.summary
