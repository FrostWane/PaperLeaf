from pathlib import Path

from paperleaf_api.evaluation_harness import evaluate_deterministic, read_cases


def test_context_harness_evaluator_runs_production_rules() -> None:
    cases_path = (
        Path(__file__).parents[1] / "evaluation" / "context-harness-v1" / "cases.jsonl"
    )

    report = evaluate_deterministic(read_cases(cases_path))

    assert report["mode"] == "deterministic"
    assert report["evidence_level"] == "deterministic_no_external_model"
    assert report["case_count"] == 100
    assert report["metrics"]["clarification"]["total"] > 0
    assert report["metrics"]["skill"]["total"] > 0
    assert report["metrics"]["tool"]["total"] == 10
    assert report["guards"]["failed_tools_activate_mode"] is False
    assert report["guards"]["final_input_exceeded"] == 0
