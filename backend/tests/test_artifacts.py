import asyncio
import json
import re
from types import SimpleNamespace

from paperleaf_api.artifacts import (
    artifact_source_revision,
    cited_chunk_ids,
    extractive_summary,
    generate_structure_artifact,
    generate_summary_artifact,
    summarize_evidence,
)
from paperleaf_api.model_runtime import ModelRouter, ModelRuntimeError
from paperleaf_api.rag.citations import Evidence


class FakeSummaryRouter:
    def __init__(self, content: str) -> None:
        self.content = content
        self.timeouts: list[float | None] = []

    def has_provider(self, purpose: str) -> bool:
        return purpose == "summary"

    async def execute(self, purpose: str, operation, *, timeout_seconds=None):
        assert purpose == "summary"
        self.timeouts.append(timeout_seconds)
        return SimpleNamespace(content=self.content)


class TimeoutThenSummaryRouter(FakeSummaryRouter):
    def __init__(self, content: str, *, always_timeout: bool = False) -> None:
        super().__init__(content)
        self.calls = 0
        self.always_timeout = always_timeout

    async def execute(self, purpose: str, operation, *, timeout_seconds=None):
        self.calls += 1
        self.timeouts.append(timeout_seconds)
        if self.calls == 1 or self.always_timeout:
            raise ModelRuntimeError("MODEL_TIMEOUT", [])
        assert purpose == "summary"
        return SimpleNamespace(content=self.content)


class SequenceSummaryRouter(FakeSummaryRouter):
    def __init__(self, contents: list[str]) -> None:
        super().__init__("")
        self.contents = contents
        self.calls = 0

    async def execute(self, purpose: str, operation, *, timeout_seconds=None):
        assert purpose == "summary"
        self.timeouts.append(timeout_seconds)
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


def _summary_json() -> str:
    rows = (
        ("research_question", "研究可复核检索。", "E1", 2),
        ("core_method", "方法采用页级检索。", "E1", 2),
        ("experimental_setup", "实验检查引用定位。", "E2", 5),
        ("main_results", "引用能够定位原文。", "E2", 5),
        ("limitations_scope", "证据仅覆盖当前论文。", "E2", 5),
    )
    return json.dumps(
        {
            "sections": [
                {
                    "key": key,
                    "facts": [
                        {
                            "text": text,
                            "citations": [
                                {"evidence_id": chunk_id, "physical_page": page}
                            ],
                        }
                    ],
                }
                for key, text, chunk_id, page in rows
            ]
        },
        ensure_ascii=False,
    )


def _structure_json(
    *, cycle: bool = False, bad_type: bool = False, disconnected: bool = False
) -> str:
    types = ["研究问题", "方法", "实验", "结果", "局限"]
    if bad_type:
        types[2] = "非法类型"
    nodes = [
        {
            "id": f"n{index}",
            "type": node_type,
            "label": f"节点 {index}",
            "summary": f"节点 {index} 的可核验内容",
            "citations": [
                {
                    "evidence_id": "E1" if index < 3 else "E2",
                    "physical_page": 2 if index < 3 else 5,
                }
            ],
        }
        for index, node_type in enumerate(types, start=1)
    ]
    edges = [
        {"source": f"n{index}", "target": f"n{index + 1}"}
        for index in range(1, 5)
    ]
    if disconnected:
        edges = [
            {"source": "n1", "target": "n2"},
            {"source": "n2", "target": "n3"},
            {"source": "n4", "target": "n5"},
        ]
    if cycle:
        edges.append({"source": "n5", "target": "n1"})
    return json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False)


def test_structured_summary_has_five_sections_and_fact_citations() -> None:
    result = asyncio.run(
        generate_summary_artifact(
            _evidence(), model_router=FakeSummaryRouter(_summary_json())
        )
    )

    assert result.status == "ready"
    assert result.fallback_reason is None
    assert [item["title"] for item in result.payload["sections"]] == [
        "研究问题",
        "核心方法",
        "实验设置",
        "主要结果",
        "局限与适用范围",
    ]
    assert all(item["facts"][0]["citations"] for item in result.payload["sections"])
    assert result.payload["citations"][0] == {
        "chunk_id": "c1",
        "physical_page": 2,
        "quote": "论文提出一种可复核的检索方法。",
    }
    assert "## 研究问题" in result.markdown


def test_structured_summary_retries_format_once_but_not_invalid_citation() -> None:
    retried = SequenceSummaryRouter(["not-json", _summary_json()])
    result = asyncio.run(generate_summary_artifact(_evidence(), model_router=retried))
    assert retried.calls == 2
    assert retried.timeouts == [120, 90]
    assert result.status == "ready"

    invalid = json.loads(_summary_json())
    invalid["sections"][0]["facts"][0]["citations"][0]["evidence_id"] = "forged"
    router = SequenceSummaryRouter([json.dumps(invalid, ensure_ascii=False)])
    result = asyncio.run(generate_summary_artifact(_evidence(), model_router=router))
    assert router.calls == 1
    assert result.status == "failed"
    assert result.fallback_reason == "模型引用未通过证据校验"
    assert result.markdown == ""
    assert result.payload["citations"] == []


def test_structured_summary_requires_a_fact_in_every_section() -> None:
    invalid = json.loads(_summary_json())
    invalid["sections"][2]["facts"] = []
    raw = json.dumps(invalid, ensure_ascii=False)
    router = SequenceSummaryRouter([raw, raw])

    result = asyncio.run(generate_summary_artifact(_evidence(), model_router=router))

    assert router.calls == 2
    assert result.status == "failed"
    assert result.fallback_reason == "模型输出格式不合法"


def test_structured_summary_rejects_english_only_facts_without_exposing_excerpts() -> None:
    english = json.loads(_summary_json())
    for section in english["sections"]:
        section["facts"][0]["text"] = "This is an English-only generated fact."
    raw = json.dumps(english, ensure_ascii=False)

    result = asyncio.run(
        generate_summary_artifact(
            _evidence(), model_router=SequenceSummaryRouter([raw, raw])
        )
    )

    assert result.status == "failed"
    assert result.markdown == ""
    assert result.payload["sections"]
    assert all(not section["facts"] for section in result.payload["sections"])


def test_structure_requires_valid_semantic_nodes_and_acyclic_edges() -> None:
    result = asyncio.run(
        generate_structure_artifact(
            _evidence(), model_router=FakeSummaryRouter(_structure_json())
        )
    )
    assert result.status == "ready"
    assert len(result.payload["nodes"]) == 5
    assert result.payload["mermaid"].startswith("flowchart TD")
    assert all(item["citations"] for item in result.payload["nodes"])
    assert result.payload["nodes"][0]["citations"][0]["chunk_id"] == "c1"

    invalid_router = SequenceSummaryRouter(
        [_structure_json(bad_type=True), _structure_json(bad_type=True)]
    )
    invalid = asyncio.run(
        generate_structure_artifact(_evidence(), model_router=invalid_router)
    )
    assert invalid_router.calls == 2
    assert invalid.status == "failed"
    assert invalid.payload["nodes"] == []
    assert invalid.payload["mermaid"] == ""

    cycle_router = SequenceSummaryRouter(
        [_structure_json(cycle=True), _structure_json(cycle=True)]
    )
    cycle = asyncio.run(
        generate_structure_artifact(_evidence(), model_router=cycle_router)
    )
    assert cycle.status == "failed"
    assert cycle.fallback_reason == "模型结构图包含孤立节点或循环关系"
    assert cycle.payload["nodes"] == []

    disconnected_router = SequenceSummaryRouter(
        [_structure_json(disconnected=True), _structure_json(disconnected=True)]
    )
    disconnected = asyncio.run(
        generate_structure_artifact(_evidence(), model_router=disconnected_router)
    )
    assert disconnected_router.calls == 2
    assert disconnected.status == "failed"
    assert disconnected.fallback_reason == "模型结构图未形成从研究问题出发的完整有向链路"
    assert disconnected.payload["nodes"] == []


def test_structure_retries_unknown_evidence_alias_with_compact_context() -> None:
    invalid = json.loads(_structure_json())
    invalid["nodes"][0]["citations"][0]["evidence_id"] = "E99"
    router = SequenceSummaryRouter(
        [json.dumps(invalid, ensure_ascii=False), _structure_json()]
    )

    result = asyncio.run(
        generate_structure_artifact(_evidence(), model_router=router)
    )

    assert router.calls == 2
    assert router.timeouts == [180, 120]
    assert result.status == "ready"


def test_structure_without_model_never_builds_sequential_chunk_graph() -> None:
    result = asyncio.run(
        generate_structure_artifact(_evidence(), model_router=ModelRouter([]))
    )
    assert result.status == "failed"
    assert result.fallback_reason == "尚未配置可用的论文结构图模型"
    assert result.payload["nodes"] == []
    assert result.payload["evidence_excerpt"] == ""


def test_artifact_source_revision_changes_with_page_evidence() -> None:
    original = artifact_source_revision(_evidence())
    changed = [*_evidence()[:-1], Evidence("c2", "p1", "测试论文", 5, "新结果")]
    assert artifact_source_revision(changed) != original
