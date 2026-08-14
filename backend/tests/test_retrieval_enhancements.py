from paperleaf_api.rag.citations import Evidence
from paperleaf_api.rag.retrieval_enhancements import (
    MultiGranularLexicalScorer,
    assess_rewrite_need,
    balance_evidence_by_paper,
    contextual_embedding_text,
    merge_paper_subquery_evidence,
    rerank_evidence_by_sentence_windows,
    sentence_windows,
    technical_tokens,
)


def _evidence(paper: str, page: int, score: float, text: str = "attention method") -> Evidence:
    return Evidence(f"{paper}:p{page}:c0", paper, paper, page, text, retrieval_score=score)


def test_weak_rewrite_triggers_for_cross_language_and_comparison() -> None:
    decision = assess_rewrite_need(
        "比较这些方法的局限",
        [_evidence("p1", 1, 0.5, "retrieval augmented generation limitations")],
    )
    assert decision.required is True
    assert "cross_language" in decision.reasons
    assert "broad_or_comparison_intent" in decision.reasons


def test_empty_comparison_keeps_both_failure_and_intent_reasons() -> None:
    decision = assess_rewrite_need("比较三篇论文的方法", [])
    assert decision.reasons == ("no_candidates", "broad_or_comparison_intent")


def test_strong_specific_match_does_not_force_rewrite() -> None:
    decision = assess_rewrite_need(
        "What is the residual learning framework?",
        [
            _evidence("p1", 1, 0.7, "The residual learning framework eases optimization."),
            _evidence("p1", 2, 0.5, "Experiments on ImageNet."),
        ],
    )
    assert decision.required is False


def test_technical_tokens_preserve_numbers_and_acronyms() -> None:
    assert technical_tokens("比较 BERT、GPT-3 与 1.28 million 样本") == (
        "BERT",
        "GPT-3",
        "1.28",
        "million",
    )


def test_paper_balancing_prevents_one_paper_from_filling_top_k() -> None:
    result = balance_evidence_by_paper(
        [
            _evidence("p1", 1, 0.9),
            _evidence("p1", 2, 0.8),
            _evidence("p2", 3, 0.7),
            _evidence("p3", 4, 0.6),
        ],
        paper_ids=["p1", "p2", "p3"],
        limit=3,
        per_paper_limit=2,
    )
    assert [item.paper_id for item in result] == ["p1", "p2", "p3"]


def test_paper_subquery_merge_uses_one_one_one_plus_two_policy() -> None:
    result = merge_paper_subquery_evidence(
        [
            _evidence("p1", 1, 0.99),
            _evidence("p1", 2, 0.98),
            _evidence("p1", 3, 0.97),
            _evidence("p2", 1, 0.60),
            _evidence("p2", 2, 0.50),
            _evidence("p3", 1, 0.40),
        ],
        paper_ids=["p1", "p2", "p3"],
        limit=5,
    )

    assert [item.paper_id for item in result[:3]] == ["p1", "p2", "p3"]
    assert [(item.paper_id, item.physical_page) for item in result[3:]] == [
        ("p1", 2),
        ("p1", 3),
    ]
    assert len({(item.paper_id, item.physical_page) for item in result}) == 5


def test_sentence_windows_keep_complete_sentences_and_are_deterministic() -> None:
    text = " ".join(f"Sentence {index} contains several useful tokens." for index in range(80))
    first = sentence_windows(text, target_tokens=40, min_tokens=20, max_tokens=50)
    second = sentence_windows(text, target_tokens=40, min_tokens=20, max_tokens=50)
    assert first == second
    assert len(first) > 1
    assert all(window.text.endswith(".") for window in first)
    assert all(window.token_count <= 50 for window in first)


def test_sentence_windows_bound_a_single_oversized_ocr_line() -> None:
    text = " ".join(f"token{index}" for index in range(180))
    windows = sentence_windows(text, target_tokens=40, min_tokens=20, max_tokens=50)
    assert len(windows) > 1
    assert all(window.token_count <= 50 for window in windows)


class _Scorer:
    def score(self, query, documents):
        return [1.0 if "needle" in document else 0.0 for document in documents]


def test_sentence_reranker_uses_best_window_and_keeps_original_evidence() -> None:
    result = rerank_evidence_by_sentence_windows(
        "needle",
        [
            _evidence("p1", 1, 0.9, "irrelevant sentence."),
            _evidence("p2", 2, 0.1, "first sentence. needle appears here."),
        ],
        _Scorer(),
        limit=2,
        rrf_weight=0.2,
    )
    assert result[0].paper_id == "p2"
    assert result[0].text == "first sentence. needle appears here."
    assert "sentence_reranker" in result[0].retrieval_channels


def test_multigranular_reranker_scores_full_page_but_keeps_original_chunk() -> None:
    candidates = [
        _evidence("p1", 1, 0.9, "short chunk without the requested entity"),
        _evidence("p2", 2, 0.1, "original citation chunk"),
    ]
    result = rerank_evidence_by_sentence_windows(
        "FlashAttention IO complexity",
        candidates,
        MultiGranularLexicalScorer(),
        limit=2,
        rrf_weight=0.2,
        document_texts=[
            "This page discusses an unrelated baseline.",
            "FlashAttention reduces IO complexity between HBM and SRAM.",
        ],
        channel_name="multigranular_reranker",
    )
    assert result[0].paper_id == "p2"
    assert result[0].text == "original citation chunk"
    assert "multigranular_reranker" in result[0].retrieval_channels


def test_multigranular_scorer_is_deterministic_for_chinese_and_english() -> None:
    scorer = MultiGranularLexicalScorer()
    documents = ["药物靶点结合亲和力 prediction", "image generation benchmark"]
    assert scorer.score("药物靶点 affinity", documents) == scorer.score(
        "药物靶点 affinity", documents
    )
    assert scorer.score("药物靶点 affinity", documents)[0] > scorer.score(
        "药物靶点 affinity", documents
    )[1]


def test_reranker_rejects_mismatched_page_texts() -> None:
    try:
        rerank_evidence_by_sentence_windows(
            "query",
            [_evidence("p1", 1, 0.5)],
            MultiGranularLexicalScorer(),
            limit=1,
            document_texts=[],
        )
    except ValueError as exc:
        assert "数量" in str(exc)
    else:
        raise AssertionError("页文本数量不一致时必须失败")


def test_contextual_embedding_contains_metadata_without_changing_chunk_text() -> None:
    rendered = contextual_embedding_text(
        paper_title="Attention Is All You Need",
        physical_page=3,
        chunk_text="3 Methods\nThe encoder uses self-attention.",
        section_title="3 Methods",
    )
    assert "论文标题：Attention Is All You Need" in rendered
    assert "章节：3 Methods" in rendered
    assert "物理页：3" in rendered
    assert rendered.endswith("The encoder uses self-attention.")
