from collections import Counter

from paperleaf_api.evaluation_harness_live import build_scenarios


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
    assert sum(len(group) == 2 for group in groups) == 10
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
