import asyncio
from dataclasses import replace
from types import SimpleNamespace

from paperleaf_api.agent.context_budget import (
    allocate_context_budget,
    compact_conversation,
    compress_tool_results,
    estimate_tokens,
)
from paperleaf_api.agent.memory import (
    extract_memory_candidates,
    memory_hash,
    select_relevant_memories,
)
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
    assert "compacted" not in compacted[-1]


def test_memory_extraction_only_accepts_user_statements_and_selection_has_fallback() -> None:
    explicit = extract_memory_candidates("user", "请记住：以后回答都使用中文")
    research = extract_memory_candidates("user", "我的研究方向是药物靶点亲和力预测")

    assert explicit[0].type == "pinned_context"
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
    )
    await repository.create_memory_item(first)
    duplicate = await repository.create_memory_item(replace(first, id="m2", enabled=False))
    assert duplicate.id == "m1"
    assert await repository.list_memories("u2") == []

    disabled = await repository.update_owned_memory("m1", "u1", enabled=False)
    assert disabled is not None and disabled.enabled is False
    assert await repository.list_memories("u1", enabled_only=True) == []
    assert await repository.update_owned_memory("m1", "u2", enabled=True) is None
    assert await repository.delete_owned_memory("m1", "u2") is False
    assert await repository.delete_owned_memory("m1", "u1") is True
    assert await repository.list_memories("u1") == []
