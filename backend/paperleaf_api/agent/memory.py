"""用户可控长期记忆的安全提取与选择。"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

MEMORY_TYPES = {"preference", "research_interest", "entity_alias", "workflow", "pinned_context"}
_EXPLICIT = re.compile(r"(?:请)?记住[：:\s]*(.+)", re.IGNORECASE)
_PREFERENCE = re.compile(r"(?:我(?:更)?(?:喜欢|偏好|习惯)|以后请)[：:\s]*(.+)", re.IGNORECASE)
_RESEARCH = re.compile(r"(?:我的研究方向是|我主要研究|我在研究)[：:\s]*(.+)", re.IGNORECASE)


def normalize_memory_value(value: str) -> str:
    return " ".join(value.strip().split())[:2000]


def memory_hash(memory_type: str, value: str) -> str:
    normalized = f"{memory_type}:{normalize_memory_value(value).casefold()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemoryCandidate:
    type: str
    value: str
    confidence: float
    source_kind: str
    source_excerpt: str


def extract_memory_candidates(role: str, content: str) -> list[MemoryCandidate]:
    """只读取用户原话；论文、工具和 assistant 内容不会进入该函数的有效分支。"""

    if role != "user":
        return []
    normalized = normalize_memory_value(content)
    if not normalized:
        return []
    patterns = (
        (_EXPLICIT, "pinned_context", 1.0, "explicit"),
        (_RESEARCH, "research_interest", 0.97, "stated"),
        (_PREFERENCE, "preference", 0.96, "stated"),
    )
    for pattern, memory_type, confidence, source_kind in patterns:
        match = pattern.search(normalized)
        if not match:
            continue
        value = normalize_memory_value(match.group(1)).rstrip("。.!！")
        if len(value) < 2:
            return []
        if memory_type == "pinned_context" and any(
            marker in value for marker in ("回答", "语言", "中文", "英文", "格式")
        ):
            memory_type = "preference"
        return [MemoryCandidate(memory_type, value, confidence, source_kind, normalized[:500])]
    return []


def _tokens(value: str) -> set[str]:
    latin = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9_\-]+", value)
        if token.strip()
    }
    chinese = "".join(re.findall(r"[\u3400-\u9fff]", value))
    bigrams = {
        chinese[index : index + 2]
        for index in range(max(0, len(chinese) - 1))
    }
    return latin | bigrams


def _intent_matches(memory_type: str, query: str) -> bool:
    normalized = query.casefold()
    if memory_type == "preference":
        return any(
            marker in normalized for marker in ("怎么回答", "格式", "语言", "偏好", "习惯")
        )
    if memory_type == "research_interest":
        return any(
            marker in normalized
            for marker in (
                "我的研究方向",
                "按我的兴趣",
                "结合我的研究",
                "我之前研究",
                "研究方向",
                "推荐一些相关论文",
                "推荐相关论文",
            )
        )
    if memory_type == "workflow":
        return any(marker in normalized for marker in ("流程", "工作流", "下一步", "习惯"))
    if memory_type == "entity_alias":
        return any(marker in normalized for marker in ("简称", "别名", "指的是", "术语"))
    return False


def _cosine(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return dot / norm if norm else 0.0


def select_relevant_memories(
    query: str,
    memories: Sequence[Any],
    *,
    query_embedding: Sequence[float] | None = None,
    embedding_fingerprint: str | None = None,
    limit: int = 5,
) -> list[Any]:
    query_tokens = _tokens(query)

    def relevance(item: Any) -> tuple[bool, float, float, float, str]:
        value = str(getattr(item, "value", ""))
        memory_tokens = _tokens(value)
        lexical = len(query_tokens & memory_tokens) / max(
            1, min(len(query_tokens), len(memory_tokens))
        )
        compatible = bool(
            embedding_fingerprint
            and getattr(item, "embedding_fingerprint", None) == embedding_fingerprint
        )
        semantic = (
            _cosine(query_embedding, getattr(item, "embedding", None)) if compatible else 0.0
        )
        pinned = 1.0 if getattr(item, "pinned", False) else 0.0
        confidence = float(getattr(item, "confidence", 0.0))
        intent_match = _intent_matches(str(getattr(item, "type", "")), query)
        eligible = bool(pinned or semantic >= 0.55 or lexical >= 0.2 or intent_match)
        # 置信度只在已经相关的候选之间排序，不能独立使无关记忆入选。
        score = pinned * 2 + semantic + lexical + (0.25 if intent_match else 0)
        return eligible, score, semantic, confidence, value

    eligible = [item for item in memories if getattr(item, "enabled", False)]
    eligible.sort(key=lambda item: relevance(item)[1:], reverse=True)
    selected = [item for item in eligible if relevance(item)[0]]
    return selected[: max(0, limit)]
