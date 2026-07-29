from pathlib import Path

from paperleaf_api.evaluation_build import build_cases


def test_frozen_annotation_source_builds_declared_case_mix() -> None:
    dataset = Path(__file__).parents[1] / "evaluation" / "datasets" / "paperleaf-rag-v1"
    cases = build_cases(dataset / "annotations.json")

    assert len(cases) == 120
    assert sum(case.answerable for case in cases) == 100
    assert sum(case.category == "cross_paper" for case in cases) == 10
    assert sum(case.category == "adversarial_query" for case in cases) == 10
    assert sum(case.split == "dev" for case in cases) == 30
