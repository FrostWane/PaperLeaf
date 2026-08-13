import asyncio

import pytest

from paperleaf_api.agent.research_specialists import (
    EvidenceSpecialist,
    SpecialistBudgetError,
    SpecialistOutputError,
    build_specialist_prompt,
)
from paperleaf_api.agent.research_synthesis import ResearchTask
from paperleaf_api.rag.citations import Evidence


def _task(*, token_budget: int = 2048) -> ResearchTask:
    return ResearchTask(
        subtask_id="research-test:s1",
        objective="比较论文的核心方法与主要结果",
        paper_ids=("p1",),
        dimensions=("核心方法", "主要结果"),
        token_budget=token_budget,
    )


def _evidence(
    chunk_id: str = "private-chunk-id",
    *,
    paper_id: str = "p1",
    page: int = 2,
    text: str = "论文使用稀疏测量校准冷启动药物靶点亲和力预测。",
) -> Evidence:
    return Evidence(
        chunk_id=chunk_id,
        paper_id=paper_id,
        paper_title=f"论文 {paper_id}",
        physical_page=page,
        text=text,
        retrieval_score=0.9,
        retrieval_channels=("test",),
    )


def test_specialist_uses_fresh_context_and_maps_aliases_to_real_chunks() -> None:
    async def scenario() -> None:
        captured: dict = {}

        async def model(messages: tuple[dict[str, str], ...], *, max_output_tokens: int):
            captured["messages"] = messages
            captured["max_output_tokens"] = max_output_tokens
            return {
                "claims": [
                    {
                        "dimension": "核心方法",
                        "claim_key": "稀疏测量校准",
                        "claim": "  论文使用稀疏测量校准。 ",
                        "evidence_aliases": ["E1"],
                        "stance": "support",
                        "confidence": 0.92,
                    }
                ]
            }

        specialist = EvidenceSpecialist(model, timeout_seconds=1)
        analysis = await specialist.analyze(_task(), [_evidence()])

        assert [item["role"] for item in captured["messages"]] == ["system", "user"]
        combined = "\n".join(item["content"] for item in captured["messages"])
        assert "E1" in combined
        assert "private-chunk-id" not in combined
        assert "主会话" not in captured["messages"][1]["content"]
        assert captured["max_output_tokens"] == analysis.usage.output_reserve
        assert analysis.claims[0].chunk_ids == ("private-chunk-id",)
        assert analysis.claims[0].paper_ids == ("p1",)
        assert analysis.claims[0].claim_key == "稀疏测量校准"
        assert analysis.claims[0].claim == "论文使用稀疏测量校准。"
        assert analysis.finding.chunk_ids == ("private-chunk-id",)
        assert analysis.finding.stance == "support"
        assert analysis.finding.confidence == pytest.approx(0.92)
        assert analysis.usage.input_tokens + analysis.usage.output_reserve <= 2048
        assert analysis.usage.output_tokens > 0

    asyncio.run(scenario())


def test_specialist_rejects_unknown_alias_dimension_and_unscoped_evidence() -> None:
    async def scenario() -> None:
        responses = iter(
            [
                {
                    "claims": [
                        {
                            "dimension": "核心方法",
                            "claim": "主张",
                            "evidence_aliases": ["E999"],
                            "stance": "support",
                            "confidence": 0.8,
                        }
                    ]
                },
                {
                    "claims": [
                        {
                            "dimension": "研究问题",
                            "claim": "主张",
                            "evidence_aliases": ["E1"],
                            "stance": "support",
                            "confidence": 0.8,
                        }
                    ]
                },
            ]
        )

        async def model(_messages: tuple[dict[str, str], ...], *, max_output_tokens: int):
            assert max_output_tokens > 0
            return next(responses)

        specialist = EvidenceSpecialist(model, timeout_seconds=1)
        with pytest.raises(SpecialistOutputError, match="SPECIALIST_UNKNOWN_EVIDENCE_ALIAS"):
            await specialist.analyze(_task(), [_evidence()])
        with pytest.raises(SpecialistOutputError, match="SPECIALIST_UNKNOWN_DIMENSION"):
            await specialist.analyze(_task(), [_evidence()])
        with pytest.raises(SpecialistOutputError, match="SPECIALIST_NO_SCOPED_EVIDENCE"):
            await specialist.analyze(
                _task(),
                [_evidence("foreign", paper_id="another-user-paper")],
            )

    asyncio.run(scenario())


def test_specialist_timeout_lease_and_budget_are_hard_failures() -> None:
    async def scenario() -> None:
        calls = 0

        async def slow_model(_messages: tuple[dict[str, str], ...], *, max_output_tokens: int):
            nonlocal calls
            calls += 1
            assert max_output_tokens > 0
            await asyncio.sleep(0.05)
            return {"claims": []}

        specialist = EvidenceSpecialist(slow_model, timeout_seconds=0.01)
        with pytest.raises(TimeoutError):
            await specialist.analyze(_task(), [_evidence()])
        with pytest.raises(asyncio.CancelledError):
            await specialist.analyze(_task(), [_evidence()], lease_guard=lambda: False)
        assert calls == 1

        with pytest.raises(SpecialistBudgetError):
            build_specialist_prompt(_task(token_budget=256), [_evidence(text="很长" * 400)])

    asyncio.run(scenario())


def test_prompt_selection_is_deterministic_and_drops_foreign_scope() -> None:
    evidence = [
        _evidence("c2", page=2, text="第二条证据"),
        _evidence("foreign", paper_id="p2", page=1, text="越权证据"),
        _evidence("c1", page=1, text="第一条证据"),
        _evidence("c1", page=1, text="重复证据"),
    ]
    first = build_specialist_prompt(_task(), evidence)
    second = build_specialist_prompt(_task(), list(reversed(evidence)))

    assert first.messages == second.messages
    assert list(first.evidence_by_alias) == ["E1", "E2"]
    assert [item.chunk_id for item in first.evidence_by_alias.values()] == ["c1", "c2"]
    assert "越权证据" not in first.messages[1]["content"]
