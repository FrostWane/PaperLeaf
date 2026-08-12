from __future__ import annotations

import asyncio

import pytest

from paperleaf_api.rag.citations import Evidence
from paperleaf_api.rag.evidence_support_batching import (
    build_evidence_support_batches,
    grade_evidence_support_batches,
)
from paperleaf_api.rag.retrieval_quality import AnswerSupport


def _evidence(chunk_id: str) -> Evidence:
    return Evidence(
        chunk_id=chunk_id,
        paper_id="paper-1",
        paper_title="论文",
        physical_page=1,
        text=f"证据 {chunk_id}",
    )


def _answer(count: int) -> str:
    return "\n".join(
        f"- 第 {index} 条主张 [chunk:E{index}]。" for index in range(1, count + 1)
    )


def test_builds_four_claim_batches_with_only_cited_evidence() -> None:
    evidence = [_evidence(f"E{index}") for index in range(1, 10)]
    evidence.append(_evidence("E1"))
    evidence.append(_evidence("unused"))

    batches = build_evidence_support_batches(_answer(9), evidence)

    assert [batch.global_claim_indices for batch in batches] == [
        (1, 2, 3, 4),
        (5, 6, 7, 8),
        (9,),
    ]
    assert [[item.chunk_id for item in batch.evidence] for batch in batches] == [
        ["E1", "E2", "E3", "E4"],
        ["E5", "E6", "E7", "E8"],
        ["E9"],
    ]
    assert "unused" not in "".join(batch.answer for batch in batches)


def test_maps_local_supported_indices_back_to_global_claims() -> None:
    calls = 0

    async def grader(_: str, answer: str, evidence: list[Evidence]) -> AnswerSupport:
        nonlocal calls
        calls += 1
        assert len(evidence) == 4
        if calls == 1:
            assert "[chunk:E5]" not in answer
        else:
            assert "[chunk:E1]" not in answer
        if calls == 1:
            return AnswerSupport(
                False,
                0.91,
                "answer_not_supported",
                supported_claim_indices=(1, 3),
            )
        return AnswerSupport(True, 0.95, "answer_supported")

    result = asyncio.run(
        grade_evidence_support_batches(
            "问题",
            _answer(8),
            [_evidence(f"E{index}") for index in range(1, 9)],
            grader,
            max_concurrency=1,
        )
    )

    assert result.support.supported is False
    assert result.support.reason_code == "answer_not_supported"
    assert result.support.supported_claim_indices == (1, 3, 5, 6, 7, 8)
    assert result.support.support_coverage == pytest.approx(0.75)
    assert result.succeeded_batch_count == 2
    assert result.failed_batches == ()


def test_partial_failure_retains_verified_claims() -> None:
    async def grader(_: str, answer: str, __: list[Evidence]) -> AnswerSupport:
        if "[chunk:E1]" in answer:
            return AnswerSupport(True, 0.96, "answer_supported")
        if "[chunk:E5]" in answer:
            raise TimeoutError("模拟单批超时")
        return AnswerSupport(
            False,
            0.81,
            "answer_not_supported",
            supported_claim_indices=(2,),
        )

    result = asyncio.run(
        grade_evidence_support_batches(
            "问题",
            _answer(10),
            [_evidence(f"E{index}") for index in range(1, 11)],
            grader,
        )
    )

    assert result.support.reason_code == "partial_grader_unavailable"
    assert result.support.supported_claim_indices == (1, 2, 3, 4, 10)
    assert result.support.support_coverage == pytest.approx(0.5)
    assert result.succeeded_batch_count == 2
    assert result.failed_batches[0].ordinal == 2
    assert result.failed_batches[0].error_type == "TimeoutError"


def test_all_unavailable_is_not_misreported_as_unsupported_content() -> None:
    async def grader(_: str, __: str, ___: list[Evidence]) -> AnswerSupport:
        return AnswerSupport(False, 0.0, "grader_unavailable")

    result = asyncio.run(
        grade_evidence_support_batches(
            "问题",
            _answer(5),
            [_evidence(f"E{index}") for index in range(1, 6)],
            grader,
        )
    )

    assert result.support.reason_code == "grader_unavailable"
    assert result.support.claim_count == 5
    assert result.succeeded_batch_count == 0
    assert [item.ordinal for item in result.failed_batches] == [1, 2]


def test_empty_answer_and_invalid_limits_are_deterministic() -> None:
    async def grader(_: str, __: str, ___: list[Evidence]) -> AnswerSupport:
        raise AssertionError("空答案不应调用 grader")

    result = asyncio.run(grade_evidence_support_batches("问题", "", [], grader))
    assert result.support.reason_code == "no_answer_claims"
    assert result.batch_count == 0

    with pytest.raises(ValueError, match="batch_size"):
        build_evidence_support_batches(_answer(1), [], batch_size=0)
    with pytest.raises(ValueError, match="max_concurrency"):
        asyncio.run(
            grade_evidence_support_batches(
                "问题", _answer(1), [_evidence("E1")], grader, max_concurrency=0
            )
        )
