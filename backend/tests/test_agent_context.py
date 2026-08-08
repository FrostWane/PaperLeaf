from paperleaf_api.agent.context import resolve_context


def test_resolves_original_text_from_current_paper_and_recent_topic() -> None:
    result = resolve_context(
        "原文是怎么处理的？",
        {"paper_id": "paper-1", "paper_title": "DeepDTA", "physical_page": 4},
        [
            {"role": "user", "content": "DeepDTA 如何编码蛋白质序列？"},
            {"role": "assistant", "content": "使用整数编码。"},
            {"role": "user", "content": "原文是怎么处理的？"},
        ],
        session_type="paper",
    )

    assert result.needs_clarification is False
    assert result.confidence >= 0.8
    assert result.references["paper_title"] == "DeepDTA"
    assert result.references["physical_page"] == 4
    assert "蛋白质序列" in result.resolved_query


def test_selected_text_has_highest_context_confidence() -> None:
    result = resolve_context(
        "这句话是什么意思？",
        {
            "paper_id": "paper-1",
            "paper_title": "DeepDTA",
            "physical_page": 4,
            "selected_text": "Protein sequences are encoded as integer vectors.",
        },
        [],
        session_type="paper",
    )

    assert result.confidence == 0.97
    assert result.references["selected_text"].startswith("Protein sequences")


def test_ambiguous_reference_requests_clarification_without_guessing() -> None:
    result = resolve_context("它怎么样？", {}, [], session_type="library")

    assert result.needs_clarification is True
    assert result.resolved_query == "它怎么样？"
    assert result.confidence < 0.55


def test_explicit_query_does_not_require_context_resolution() -> None:
    result = resolve_context("解释药物靶点亲和力预测", {}, [], session_type="library")

    assert result.needs_clarification is False
    assert result.resolved_query == result.original_query
    assert result.sources == ("explicit_query",)
