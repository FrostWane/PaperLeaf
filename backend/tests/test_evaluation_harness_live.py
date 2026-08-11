from collections import Counter
from types import SimpleNamespace

from paperleaf_api.evaluation_harness_live import (
    LiveHarness,
    LiveRunResult,
    LiveScenario,
    _grade_recommendation_sequence,
    build_scenarios,
)


def test_live_harness_matrix_contains_exactly_one_hundred_real_run_slots() -> None:
    papers = [
        {"id": "p1", "title": "Paper One"},
        {"id": "p2", "title": "Paper Two"},
        {"id": "p3", "title": "Paper Three"},
    ]
    samples = {
        paper["id"]: [(1, "This is a sufficiently long trusted page excerpt for live testing.")]
        for paper in papers
    }

    groups = build_scenarios(papers, "collection-1", samples)
    scenarios = [scenario for group in groups for scenario in group]

    assert len(scenarios) == 100
    assert [scenario.index for scenario in scenarios] == list(range(1, 101))
    assert Counter(scenario.category for scenario in scenarios) == {
        "selection": 25,
        "multiturn": 20,
        "paper_qa": 20,
        "collection": 15,
        "function_mcp": 10,
        "memory_long_context": 5,
        "degradation": 5,
    }
    assert sum(len(group) == 2 for group in groups) == 15
    assert sum(
        len(group) == 2 and group[0].category == "function_mcp" for group in groups
    ) == 5
    assert all(
        group[0].group == group[1].group
        for group in groups
        if len(group) == 2
    )


def test_selected_live_cases_bind_text_to_the_same_paper_and_page() -> None:
    papers = [
        {"id": "p1", "title": "Paper One"},
        {"id": "p2", "title": "Paper Two"},
    ]
    samples = {
        "p1": [(2, "A selected passage that was read from physical page two.")],
        "p2": [(5, "Another selected passage that came from physical page five.")],
    }

    scenarios = [
        scenario
        for group in build_scenarios(papers, "collection-1", samples)
        for scenario in group
        if scenario.category == "selection"
    ]

    assert len(scenarios) == 25
    assert all(scenario.selected_text for scenario in scenarios)
    assert all(scenario.physical_page in {2, 5} for scenario in scenarios)
    assert all(scenario.session_type == "paper" for scenario in scenarios)


def test_method_summary_is_a_valid_single_paper_skill() -> None:
    papers = [
        {"id": "p1", "title": "Paper One"},
        {"id": "p2", "title": "Paper Two"},
    ]
    samples = {
        paper["id"]: [(1, "A sufficiently long page excerpt for live testing.")]
        for paper in papers
    }
    scenarios = [
        scenario
        for group in build_scenarios(papers, "collection-1", samples)
        for scenario in group
        if scenario.category == "paper_qa" and "概括论文采用的主要方法" in scenario.question
    ]

    assert scenarios
    assert all("summarize_paper" in item.expected_skills for item in scenarios)


def test_live_grader_separates_controlled_provider_degradation_from_app_failure() -> None:
    scenario = LiveScenario(
        index=1,
        category="function_mcp",
        title="Semantic Scholar 限流",
        session_type="collection",
        question="只使用 Semantic Scholar 推荐五篇论文",
        expected_skills=("find_related_papers",),
        require_citations=False,
        require_native_tools=True,
        expected_tools=("mcp__academic__search_semantic_scholar",),
    )
    result = LiveRunResult(
        index=1,
        category="function_mcp",
        title=scenario.title,
        question=scenario.question,
        status="completed",
        selected_skill="find_related_papers",
        native_function_calling_attempted=True,
        tool_mode_active=False,
        tool_calls=[
            {
                "tool": "mcp__academic__search_semantic_scholar",
                "status": "failed",
                "error_code": "SEMANTIC_SCHOLAR_RATE_LIMITED",
            }
        ],
        final_input_tokens=1000,
        hard_limit=2000,
        answer=(
            "### 联网推荐\n\nSemantic Scholar 本轮请求频率受限，"
            "没有返回可核验的候选论文。"
        ),
    )
    run = SimpleNamespace(
        status="completed",
        error_code=None,
        selected_skill="find_related_papers",
        scope_snapshot={"paper_ids": []},
    )

    LiveHarness._grade(object.__new__(LiveHarness), scenario, run, [], result)

    assert result.failures == []
    assert result.external_provider_degradations == [
        "SEMANTIC_SCHOLAR_RATE_LIMITED"
    ]


def test_live_sequence_detects_repeated_batch_and_lost_year_constraint() -> None:
    first = LiveRunResult(
        index=1,
        category="function_mcp",
        title="first",
        question="推荐一篇",
        answer="| 1 | **Paper A** | 2025 | Venue | DOI | OpenAlex |",
        displayed_recommendations=[
            {"title": "Paper A", "year": 2025, "relevance_score": 0.9}
        ],
        structural_pass=True,
    )
    followup = LiveRunResult(
        index=2,
        category="function_mcp",
        title="followup",
        question="换一批一篇 2026 年的",
        answer="| 1 | **Paper A** | 2025 | Venue | DOI | OpenAlex |",
        active_task={"requested_count": 1, "year_from": 2026, "year_to": 2026},
        displayed_recommendations=[
            {"title": "Paper A", "year": 2025, "relevance_score": 0.9}
        ],
        structural_pass=True,
    )

    _grade_recommendation_sequence([first, followup])

    assert first.failures == []
    assert followup.failures == [
        "recommendation_batch_repeated",
        "recommendation_year_constraint_lost",
    ]
    assert followup.structural_pass is False


def test_live_sequence_rejects_empty_short_or_library_recommendations() -> None:
    empty = LiveRunResult(
        index=1,
        category="function_mcp",
        title="empty",
        question="推荐五篇",
        active_task={"requested_count": 5, "exclude_library": True},
        answer="### 联网推荐\n\n没有候选。",
    )
    library = LiveRunResult(
        index=2,
        category="function_mcp",
        title="library",
        question="改成一篇",
        active_task={"requested_count": 1, "exclude_library": True},
        answer="| 1 | **DeepDTA** | 2018 | Venue | DOI | OpenAlex |",
        displayed_recommendations=[
            {"title": "DeepDTA", "year": 2018, "lexical_score": 1.0}
        ],
        library_titles=["DeepDTA"],
    )

    _grade_recommendation_sequence([empty, library])

    assert "recommendation_empty" in empty.failures
    assert "recommendation_count:0/5" not in empty.failures
    assert "recommendation_contains_library_paper" in library.failures
