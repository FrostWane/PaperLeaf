from types import SimpleNamespace

from paperleaf_api.agent.answerability import AnswerabilityDecision
from paperleaf_api.evaluation_answerability import (
    score_answerability,
    select_development_threshold,
)


def _case(answerable: bool):
    return SimpleNamespace(answerable=answerable)


def test_answerability_metrics_keep_false_answer_denominator_explicit() -> None:
    metrics = score_answerability(
        [_case(True), _case(True), _case(False), _case(False)],
        [
            AnswerabilityDecision(answerable=True, confidence=0.9, reason_code="direct"),
            AnswerabilityDecision(answerable=True, confidence=0.6, reason_code="direct"),
            AnswerabilityDecision(answerable=True, confidence=0.8, reason_code="adjacent"),
            AnswerabilityDecision(answerable=False, confidence=0.9, reason_code="missing"),
        ],
        threshold=0.7,
    )

    assert metrics["true_positive"] == 1
    assert metrics["false_negative"] == 1
    assert metrics["false_positive"] == 1
    assert metrics["true_negative"] == 1
    assert metrics["unanswerable_false_answer_rate"] == 0.5


def test_threshold_selection_prioritizes_zero_false_answers() -> None:
    selected = select_development_threshold(
        [
            {
                "threshold": 0.6,
                "not_checked": 0,
                "false_positive": 1,
                "answerable_recall": 1.0,
            },
            {
                "threshold": 0.7,
                "not_checked": 0,
                "false_positive": 0,
                "answerable_recall": 0.8,
            },
            {
                "threshold": 0.8,
                "not_checked": 0,
                "false_positive": 0,
                "answerable_recall": 0.7,
            },
        ]
    )

    assert selected["threshold"] == 0.7
    assert selected["selection_reason"] == "zero_false_answer_then_max_answerable_recall"
