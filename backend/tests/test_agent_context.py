from paperleaf_api.agent.context import (
    TaskFrameDecision,
    merge_task_frame,
    resolve_context,
)


def test_resolves_original_text_from_current_paper_and_recent_topic() -> None:
    result = resolve_context(
        "原文是怎么处理的？",
        {"paper_id": "paper-1", "paper_title": "DeepDTA", "physical_page": 4},
        [
            {"role": "user", "content": "DeepDTA 如何编码蛋白质序列？"},
            {"role": "assistant", "content": "使用整数编码。"},
            {"role": "user", "content": "原文是怎么处理的？"},
        ],
        session_type="paper",
    )

    assert result.needs_clarification is False
    assert result.confidence >= 0.8
    assert result.references["paper_title"] == "DeepDTA"
    assert result.references["physical_page"] == 4
    assert "蛋白质序列" in result.resolved_query


def test_selected_text_has_highest_context_confidence() -> None:
    result = resolve_context(
        "这句话是什么意思？",
        {
            "paper_id": "paper-1",
            "paper_title": "DeepDTA",
            "physical_page": 4,
            "selected_text": "Protein sequences are encoded as integer vectors.",
        },
        [],
        session_type="paper",
    )

    assert result.confidence == 0.97
    assert result.references["selected_text"].startswith("Protein sequences")


def test_selection_is_used_even_without_fixed_reference_marker() -> None:
    result = resolve_context(
        "这些讲了什么？",
        {
            "paper_id": "paper-1",
            "paper_title": "DeepDTA",
            "physical_page": 2,
            "selected_text": "The model uses two convolutional neural networks.",
        },
        [],
        session_type="paper",
    )

    assert result.needs_clarification is False
    assert result.references["selected_text"].startswith("The model uses")
    assert "本轮首要材料（已验证选文）" in result.resolved_query


def test_followup_entity_uses_structured_conversation_state() -> None:
    result = resolve_context(
        "那药物呢？",
        {"paper_id": "paper-1", "paper_title": "DeepDTA"},
        [
            {
                "role": "context",
                "content": (
                    '{"entity_state":{"discussion_entity":"蛋白质序列",'
                    '"physical_page":4}}'
                ),
            }
        ],
        session_type="paper",
    )

    assert result.needs_clarification is False
    assert result.references["discussion_entity"] == "药物"
    assert "本轮追问对象：药物" in result.resolved_query


def test_ambiguous_reference_requests_clarification_without_guessing() -> None:
    result = resolve_context("它怎么样？", {}, [], session_type="library")

    assert result.needs_clarification is True
    assert result.resolved_query == "它怎么样？"
    assert result.confidence < 0.55


def test_explicit_query_does_not_require_context_resolution() -> None:
    result = resolve_context("解释药物靶点亲和力预测", {}, [], session_type="library")

    assert result.needs_clarification is False
    assert result.resolved_query == result.original_query
    assert result.sources == ("explicit_query",)


def test_collection_pronoun_uses_server_verified_collection_scope() -> None:
    result = resolve_context(
        "这些论文有哪些共同假设与不同实验设计？",
        {"collection_id": "collection-1", "collection_title": "Harness 验收"},
        [],
        session_type="collection",
    )

    assert result.needs_clarification is False
    assert result.references["collection_id"] == "collection-1"
    assert "当前集合" in result.resolved_query


def test_recent_year_followup_inherits_external_discovery_constraints() -> None:
    previous = (
        "请根据当前集合的研究主题，联网推荐 5 篇尚未在文献库中的相关论文，"
        "列出题目、年份、出版物、DOI，并说明推荐理由。"
    )
    result = resolve_context(
        "有没有更近的论文，如2026年的",
        {"collection_id": "collection-1", "collection_title": "DTA"},
        [
            {"role": "user", "content": previous},
            {"role": "assistant", "content": "已返回 5 篇 OpenAlex 候选。"},
        ],
        session_type="collection",
    )

    assert result.needs_clarification is False
    task = result.references["active_task"]
    assert task["name"] == "find_related_papers"
    assert task["web_required"] is True
    assert task["requested_count"] == 5
    assert task["exclude_library"] is True
    assert task["source_policy"] == "academic_external"
    assert task["year_from"] == task["year_to"] == 2026
    assert task["inherited"] is True
    assert task["context_source"] == "deterministic_fallback"
    assert "继续联网推荐 5 篇" in result.resolved_query
    assert "目标发表年份：2026" in result.resolved_query


def test_unrelated_year_question_does_not_inherit_discovery_task() -> None:
    result = resolve_context(
        "2026 年这个数字代表什么？",
        {"paper_id": "paper-1", "paper_title": "DeepDTA"},
        [{"role": "user", "content": "解释这篇论文的实验表格"}],
        session_type="paper",
    )

    assert "active_task" not in result.references


def test_explicit_task_switch_does_not_reuse_stored_discovery_task() -> None:
    result = resolve_context(
        "解释这篇 2026 年论文的方法",
        {"paper_id": "paper-2", "paper_title": "New DTA"},
        [
            {
                "role": "context",
                "content": (
                    '{"entity_state":{"active_task":{"name":"find_related_papers",'
                    '"requested_count":5,"web_required":true}}}'
                ),
            },
            {"role": "user", "content": "再推荐 5 篇近期论文"},
        ],
        session_type="paper",
    )

    assert "active_task" not in result.references


def test_discovery_context_recovers_after_a_previous_failed_followup() -> None:
    result = resolve_context(
        "那就继续找 2026 年的",
        {"collection_id": "collection-1"},
        [
            {"role": "user", "content": "联网推荐 5 篇尚未入库的相关论文"},
            {"role": "assistant", "content": "已返回 OpenAlex 结果"},
            {"role": "user", "content": "有没有更近的论文，如2026年的"},
            {"role": "assistant", "content": "旧版错误地使用了本地参考文献"},
        ],
        session_type="collection",
    )

    assert result.references["active_task"]["name"] == "find_related_papers"
    assert result.references["active_task"]["requested_count"] == 5
    assert result.references["active_task"]["year_from"] == 2026


def test_model_task_frame_updates_only_source_and_preserves_other_slots() -> None:
    existing = {
        "name": "find_related_papers",
        "requested_count": 5,
        "year_from": 2026,
        "year_to": 2026,
        "exclude_library": True,
        "shown_entities": ["doi:10.1/already-shown"],
        "requested_sources": ["mcp__academic__search_openalex"],
    }
    decision = TaskFrameDecision(
        operation="update",
        task_name="find_related_papers",
        updated_fields=("requested_sources", "denied_sources"),
        values={
            "requested_sources": ["mcp__academic__search_semantic_scholar"],
            "denied_sources": ["mcp__academic__search_openalex", "search_arxiv"],
        },
        confidence=0.96,
    )

    merged = merge_task_frame(existing, decision)

    assert merged is not None
    assert merged["requested_count"] == 5
    assert merged["year_from"] == 2026
    assert merged["exclude_library"] is True
    assert merged["shown_entities"] == ["doi:10.1/already-shown"]
    assert merged["requested_sources"] == [
        "mcp__academic__search_semantic_scholar"
    ]


def test_model_task_frame_understands_count_only_followup_without_phrase_whitelist() -> None:
    decision = TaskFrameDecision(
        operation="update",
        task_name="find_related_papers",
        updated_fields=("requested_count",),
        values={"requested_count": 3},
        confidence=0.94,
    )
    result = resolve_context(
        "改成三篇",
        {"collection_id": "collection-1"},
        [
            {
                "role": "context",
                "content": (
                    '{"entity_state":{"active_task":{"name":"find_related_papers",'
                    '"requested_count":5,"year_from":2026,"year_to":2026,'
                    '"exclude_library":true}}}'
                ),
            }
        ],
        session_type="collection",
        task_frame_decision=decision,
    )

    task = result.references["active_task"]
    assert task["requested_count"] == 3
    assert task["year_from"] == 2026
    assert task["exclude_library"] is True
    assert result.snapshot({})["task_frame"] == {
        "source": "model_function_call",
        "confidence": 0.94,
    }
