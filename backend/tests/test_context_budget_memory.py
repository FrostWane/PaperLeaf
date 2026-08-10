import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

from paperleaf_api.agent.context_budget import (
    allocate_context_budget,
    compact_conversation,
    compress_tool_results,
    enforce_context_envelope,
    estimate_tokens,
)
from paperleaf_api.agent.memory import (
    extract_memory_candidates,
    memory_hash,
    select_relevant_memories,
)
from paperleaf_api.rag.citations import Evidence
from paperleaf_api.repository import MemoryItemRecord, MemoryRepository


def test_context_budget_reserves_output_and_compacts_long_conversation() -> None:
    budget = allocate_context_budget(4096)
    messages = []
    for index in range(50):
        messages.extend(
            [
                {
                    "id": f"u{index}",
                    "role": "user",
                    "content": (
                        "必须使用中文回答，并比较方法。" if index == 2 else "继续解释这个方法。"
                    )
                    * 20,
                },
                {
                    "id": f"a{index}",
                    "role": "assistant",
                    "content": "这是带引用的旧回答。[chunk:paper:p4:c1]" * 20,
                },
            ]
        )
    result = compact_conversation(
        messages,
        existing_summary={},
        keep_recent_turns=6,
        compact_at_tokens=budget.compact_at,
    )

    assert result.compacted is True
    assert len(result.recent_messages) == 12
    assert result.after_tokens < result.before_tokens * 0.5
    assert any("必须使用中文" in item for item in result.summary["user_constraints"])
    assert "[chunk:" not in str(result.summary)
    assert result.compacted_through_message_id == "a43"
    assert budget.input_limit + budget.output + budget.safety == budget.model_window
    assert estimate_tokens("中英文 mixed context") > 0


def test_tool_result_compaction_never_breaks_call_result_pair() -> None:
    entries = [
        {"kind": "call", "tool_call_id": f"t{index}", "name": "search"}
        if offset == 0
        else {
            "kind": "result",
            "tool_call_id": f"t{index}",
            "content": "证据" * 2000,
            "artifact_id": f"artifact-{index}",
        }
        for index in range(5)
        for offset in range(2)
    ]
    compacted = compress_tool_results(entries, keep_complete=3, preview_tokens=50)

    assert len(compacted) == 10
    for index in range(0, len(compacted), 2):
        assert compacted[index]["kind"] == "call"
        assert compacted[index + 1]["kind"] == "result"
        assert compacted[index]["tool_call_id"] == compacted[index + 1]["tool_call_id"]
    assert compacted[1]["compacted"] is True
    assert compacted[-1]["compacted"] is True
    assert estimate_tokens(compacted[-1]["content"]) <= 2400


def test_final_context_envelope_enforces_hard_limit_and_preserves_selection() -> None:
    evidence = [
        Evidence("selected", "p1", "论文", 2, "已验证选文" * 120),
        Evidence("low-score", "p1", "论文", 8, "低分证据" * 1200),
    ]
    entries = [
        {"kind": "call", "tool_call_id": f"t{index}", "content": "查询"}
        if offset == 0
        else {
            "kind": "result",
            "tool_call_id": f"t{index}",
            "content": "工具结果" * 1200,
        }
        for index in range(5)
        for offset in range(2)
    ]
    envelope = enforce_context_envelope(
        query="这些讲了什么？\n本轮首要材料（已验证选文）：已验证选文",
        messages=[
            {"role": "user", "content": "旧消息" * 800},
            {"role": "assistant", "content": "旧回答" * 800},
            {"role": "skill", "content": "只回答选文"},
        ],
        evidence=evidence,
        tool_entries=entries,
        hard_limit=2600,
        protected_evidence_ids={"selected"},
        system_reserve=200,
    )

    assert envelope.exceeded is False
    assert envelope.usage["final_input_tokens"] <= 2600
    assert [item.chunk_id for item in envelope.evidence] == ["selected"]
    assert envelope.usage["dropped_evidence"] == 1
    assert len(envelope.tool_entries) % 2 == 0
    for index in range(0, len(envelope.tool_entries), 2):
        assert (
            envelope.tool_entries[index]["tool_call_id"]
            == envelope.tool_entries[index + 1]["tool_call_id"]
        )


def test_context_envelope_refuses_request_when_protected_input_alone_is_too_large() -> None:
    envelope = enforce_context_envelope(
        query="选文" * 2000,
        messages=[],
        evidence=[],
        tool_entries=[],
        hard_limit=500,
        system_reserve=200,
    )

    assert envelope.exceeded is True
    assert envelope.usage["final_input_tokens"] > envelope.usage["hard_limit"]


def test_extreme_structured_tool_result_stays_valid_and_drops_as_whole_pair() -> None:
    entries = [
        {
            "kind": "call",
            "tool_call_id": "huge-1",
            "tool": "mcp__academic__search_openalex",
            "content": json.dumps({"query": "主题" * 5000}, ensure_ascii=False),
        },
        {
            "kind": "result",
            "tool_call_id": "huge-1",
            "tool": "mcp__academic__search_openalex",
            "content": json.dumps(
                {
                    "status": "succeeded",
                    "items": [
                        {"title": f"论文 {index}", "abstract": "摘要" * 2000}
                        for index in range(100)
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]

    compacted = compress_tool_results(entries, keep_complete=3, preview_tokens=100)
    assert all(json.loads(item["content"]) for item in compacted)

    envelope = enforce_context_envelope(
        query="推荐论文",
        messages=[],
        evidence=[],
        tool_entries=entries,
        hard_limit=110,
        system_reserve=100,
    )
    assert envelope.exceeded is False
    assert envelope.tool_entries == []
    assert envelope.usage["dropped_tool_pairs"] == 1
    assert envelope.usage["final_input_tokens"] <= 110


def test_memory_extraction_only_accepts_user_statements_and_selection_has_fallback() -> None:
    explicit = extract_memory_candidates("user", "请记住：以后回答都使用中文")
    research = extract_memory_candidates("user", "我的研究方向是药物靶点亲和力预测")

    assert explicit[0].type == "preference"
    assert explicit[0].confidence == 1.0
    assert research[0].type == "research_interest"
    assert extract_memory_candidates("assistant", "记住论文结论是有效的") == []
    assert extract_memory_candidates("tool", "记住工具结果") == []
    memories = [
        SimpleNamespace(
            value="偏好中文回答", confidence=1.0, enabled=True, pinned=True, embedding=None
        ),
        SimpleNamespace(
            value="研究蛋白质结构", confidence=0.9, enabled=False, pinned=False, embedding=None
        ),
    ]
    assert select_relevant_memories("解释方法", memories, limit=5) == [memories[0]]


def test_unrelated_high_confidence_memory_cannot_enter_context() -> None:
    relevant = SimpleNamespace(
        type="research_interest",
        value="药物靶点亲和力预测",
        confidence=0.7,
        enabled=True,
        pinned=False,
        embedding=[1.0, 0.0],
        embedding_fingerprint="fp-current",
    )
    unrelated = SimpleNamespace(
        type="research_interest",
        value="量子密码协议",
        confidence=1.0,
        enabled=True,
        pinned=False,
        embedding=[0.0, 1.0],
        embedding_fingerprint="fp-current",
    )
    stale = SimpleNamespace(
        type="research_interest",
        value="完全无关内容",
        confidence=1.0,
        enabled=True,
        pinned=False,
        embedding=[1.0, 0.0],
        embedding_fingerprint="fp-old",
    )

    selected = select_relevant_memories(
        "推荐药物靶点相关论文",
        [unrelated, stale, relevant],
        query_embedding=[1.0, 0.0],
        embedding_fingerprint="fp-current",
    )

    assert relevant in selected
    assert unrelated not in selected
    assert stale not in selected


def test_memory_repository_versions_isolates_users_and_honors_disable_delete() -> None:
    asyncio.run(_memory_repository_scenario())


async def _memory_repository_scenario() -> None:
    repository = MemoryRepository("secret")
    first = MemoryItemRecord(
        id="m1",
        user_id="u1",
        type="preference",
        value="使用中文回答",
        normalized_hash=memory_hash("preference", "使用中文回答"),
        confidence=1.0,
        source_kind="manual",
        embedding=[0.1, 0.2],
        embedding_fingerprint="old-fingerprint",
    )
    await repository.create_memory_item(first)
    duplicate = await repository.create_memory_item(replace(first, id="m2", enabled=False))
    assert duplicate.id == "m1"
    assert await repository.list_memories("u2") == []

    disabled = await repository.update_owned_memory("m1", "u1", enabled=False)
    assert disabled is not None and disabled.enabled is False
    assert await repository.list_memories("u1", enabled_only=True) == []
    assert await repository.update_owned_memory("m1", "u2", enabled=True) is None
    changed = await repository.update_owned_memory(
        "m1",
        "u1",
        value="改为简洁回答",
        normalized_hash=memory_hash("preference", "改为简洁回答"),
    )
    assert changed is not None
    assert changed.embedding is None
    assert changed.embedding_fingerprint is None
    assert await repository.delete_owned_memory("m1", "u2") is False
    assert await repository.delete_owned_memory("m1", "u1") is True
    assert await repository.list_memories("u1") == []
