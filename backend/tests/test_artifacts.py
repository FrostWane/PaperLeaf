import asyncio
import re
from types import SimpleNamespace

from paperleaf_api.artifacts import cited_chunk_ids, extractive_summary, summarize_evidence
from paperleaf_api.model_runtime import ModelRouter, ModelRuntimeError
from paperleaf_api.rag.citations import Evidence


class FakeSummaryRouter:
    def __init__(self, content: str) -> None:
        self.content = content

    def has_provider(self, purpose: str) -> bool:
        return purpose == "summary"

    async def execute(self, purpose: str, operation):
        assert purpose == "summary"
        return SimpleNamespace(content=self.content)


class TimeoutThenSummaryRouter(FakeSummaryRouter):
    def __init__(self, content: str, *, always_timeout: bool = False) -> None:
        super().__init__(content)
        self.calls = 0
        self.always_timeout = always_timeout

    async def execute(self, purpose: str, operation):
        self.calls += 1
        if self.calls == 1 or self.always_timeout:
            raise ModelRuntimeError("MODEL_TIMEOUT", [])
        return await super().execute(purpose, operation)


class SequenceSummaryRouter(FakeSummaryRouter):
    def __init__(self, contents: list[str]) -> None:
        super().__init__("")
        self.contents = contents
        self.calls = 0

    async def execute(self, purpose: str, operation):
        assert purpose == "summary"
        content = self.contents[self.calls]
        self.calls += 1
        return SimpleNamespace(content=content)


def _evidence() -> list[Evidence]:
    return [
        Evidence("c1", "p1", "测试论文", 2, "论文提出一种可复核的检索方法。"),
        Evidence("c2", "p1", "测试论文", 5, "实验结果表明引用能够定位到原文。"),
    ]


def test_summary_without_model_is_visible_extractive_and_cited() -> None:
    evidence = _evidence()

    content, mode = asyncio.run(
        summarize_evidence(evidence, model_router=ModelRouter([]))
    )

    assert mode == "extractive"
    assert content.startswith("提取式概览（非模型生成）")
    assert "论文提出一种可复核的检索方法。" in content
    assert "实验结果表明引用能够定位到原文。" in content
    assert re.findall(r"\[chunk:([^\]]+)\]", content) == ["c1", "c2"]


def test_summary_keeps_model_output_when_every_fact_line_has_valid_citation() -> None:
    evidence = _evidence()
    model_content = (
        "## 方法\n"
        "- 论文提出一种可复核的检索方法。 [chunk:c1]\n"
        "## 结果\n"
        "实验结果表明引用能够定位到原文。 [chunk:c2]"
    )

    content, mode = asyncio.run(
        summarize_evidence(evidence, model_router=FakeSummaryRouter(model_content))
    )

    assert mode == "model"
    assert content == model_content


def test_summary_retries_timeout_with_compact_context() -> None:
    model_content = "## 方法\n论文提出一种可复核的检索方法。 [chunk:c1]"
    router = TimeoutThenSummaryRouter(model_content)

    content, mode = asyncio.run(summarize_evidence(_evidence(), model_router=router))

    assert router.calls == 2
    assert mode == "model"
    assert content == model_content


def test_summary_keeps_cited_extractive_fallback_after_two_timeouts() -> None:
    router = TimeoutThenSummaryRouter("", always_timeout=True)

    content, mode = asyncio.run(summarize_evidence(_evidence(), model_router=router))

    assert router.calls == 2
    assert mode == "extractive"
    assert cited_chunk_ids(content, _evidence()) == ["c1", "c2"]


def test_summary_retries_invalid_citation_format_once() -> None:
    invalid_content = "下面是论文概览：\n## 方法\n论文提出一种检索方法。 [chunk:c1]"
    valid_content = "## 方法\n论文提出一种可复核的检索方法。 [chunk:c1]"
    router = SequenceSummaryRouter([invalid_content, valid_content])

    content, mode = asyncio.run(summarize_evidence(_evidence(), model_router=router))

    assert router.calls == 2
    assert mode == "model"
    assert content == valid_content


def test_cited_chunk_ids_returns_valid_ids_in_first_appearance_order() -> None:
    content = (
        "结果一。 [chunk:c2]\n"
        "结果二。 [chunk:forged] [chunk:c1]\n"
        "结果三。 [chunk:c2]"
    )

    assert cited_chunk_ids(content, _evidence()) == ["c2", "c1"]


def test_summary_falls_back_when_model_fact_line_has_no_citation() -> None:
    evidence = _evidence()
    model_content = "## 方法\n论文提出一种可复核的检索方法。"

    content, mode = asyncio.run(
        summarize_evidence(evidence, model_router=FakeSummaryRouter(model_content))
    )

    assert mode == "extractive"
    assert content == extractive_summary(evidence)


def test_summary_falls_back_when_model_uses_unknown_chunk_id() -> None:
    evidence = _evidence()
    model_content = "论文提出一种可复核的检索方法。 [chunk:forged]"

    content, mode = asyncio.run(
        summarize_evidence(evidence, model_router=FakeSummaryRouter(model_content))
    )

    assert mode == "extractive"
    assert content == extractive_summary(evidence)


def test_extractive_summary_keeps_complete_citation_when_truncated() -> None:
    evidence = [
        Evidence("chunk-long", "p1", "测试论文", 3, "很长的原文内容" * 500),
    ]

    content, mode = asyncio.run(
        summarize_evidence(evidence, model_router=ModelRouter([]))
    )

    assert mode == "extractive"
    assert len(content) <= 1800
    assert content.endswith("[chunk:chunk-long]")
    assert "…" in content


def test_extractive_summary_spreads_evidence_across_the_paper() -> None:
    evidence = [
        Evidence(f"c{page}", "p1", "测试论文", page, f"第 {page} 页内容" * 120)
        for page in range(1, 9)
    ]

    content = extractive_summary(evidence)
    cited = cited_chunk_ids(content, evidence)

    assert len(cited) == 6
    assert cited[0] == "c1"
    assert cited[-1] == "c8"
    assert len(content) <= 1800
