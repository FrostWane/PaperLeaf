"""联网论文候选的类型过滤、实体去重与可复现相关性重排。"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Any

from ..discovery import tokenize

_ALLOWED_WORK_TYPES = frozenset(
    {
        "article",
        "preprint",
        "proceedings-article",
        "conference",
        "journalarticle",
        "review",
        "metaanalysis",
        "study",
        "clinicaltrial",
        "casereport",
        "book-chapter",
        "booksection",
        "dissertation",
        "thesis",
    }
)
_REJECTED_WORK_TYPES = frozenset(
    {
        "dataset",
        "paratext",
        "peer-review",
        "reference-entry",
        "editorial",
        "lettersandcomments",
        "news",
        "book",
        "report",
        "other",
        "component",
        "supplementary-material",
    }
)
_ATTACHMENT_TITLE_RE = re.compile(
    r"(?:^|\s)(?:figure|fig\.?|image|graphical abstract|supplementary (?:figure|image))\s*\d*\s*$"
    r"|\.(?:png|jpe?g|gif|tiff?|bmp|svg)(?:\?.*)?$",
    re.IGNORECASE,
)


def normalize_title(value: str | None) -> str:
    return "".join(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", (value or "").casefold()))


def _titles_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    return min(len(left), len(right)) >= 6 and (
        left.startswith(right) or right.startswith(left)
    )


def normalize_doi(value: str | None) -> str:
    normalized = (value or "").strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
    return normalized.rstrip(" .")


def normalize_arxiv_id(value: str | None) -> str:
    normalized = (value or "").strip().casefold()
    normalized = normalized.removeprefix("https://arxiv.org/abs/")
    normalized = normalized.removeprefix("http://arxiv.org/abs/")
    return re.sub(r"v\d+$", "", normalized)


def entity_keys(item: Any, *, source: str | None = None) -> set[str]:
    """返回一个实体的全部稳定键；任意键命中即视为同一篇论文。"""

    getter = (
        item.get
        if isinstance(item, dict)
        else lambda key, default=None: getattr(item, key, default)
    )
    keys: set[str] = set()
    doi = normalize_doi(getter("doi"))
    if doi:
        keys.add(f"doi:{doi}")
    arxiv_id = normalize_arxiv_id(getter("arxiv_id"))
    if arxiv_id:
        keys.add(f"arxiv:{arxiv_id}")
    external_id = str(getter("external_id") or "").strip().casefold()
    effective_source = re.sub(
        r"[^a-z0-9]+",
        "_",
        str(source or getter("source") or "external").strip().casefold(),
    ).strip("_")
    if external_id:
        keys.add(f"external:{effective_source}:{external_id}")
    stored_external_ids = getter("academic_external_ids")
    if isinstance(stored_external_ids, dict):
        for stored_source, stored_id in stored_external_ids.items():
            normalized_source = re.sub(
                r"[^a-z0-9]+", "_", str(stored_source).strip().casefold()
            ).strip("_")
            normalized_id = str(stored_id).strip().casefold()
            if normalized_source and normalized_id:
                keys.add(f"external:{normalized_source}:{normalized_id}")
    title = normalize_title(str(getter("title") or getter("paper_title") or ""))
    if title:
        keys.add(f"title:{title}")
    return keys


def is_research_paper(item: dict[str, Any]) -> tuple[bool, str | None]:
    """只依赖公开元数据做保守类型过滤；类型缺失时不误删正常论文。"""

    title = " ".join(str(item.get("title") or item.get("paper_title") or "").split())
    if not title:
        return False, "missing_title"
    if item.get("is_paratext") is True:
        return False, "paratext"
    if item.get("is_retracted") is True:
        return False, "retracted"
    if _ATTACHMENT_TITLE_RE.search(title):
        return False, "attachment_title"

    declared: list[str] = []
    work_type = str(item.get("work_type") or "").strip().casefold()
    if work_type:
        declared.append(work_type)
    publication_types = item.get("publication_types")
    if isinstance(publication_types, list):
        declared.extend(str(value).strip().casefold() for value in publication_types)
    declared = [value.replace("_", "-").replace(" ", "") for value in declared if value]
    if any(value in _REJECTED_WORK_TYPES for value in declared):
        return False, "non_paper_type"
    if declared and not any(value in _ALLOWED_WORK_TYPES for value in declared):
        return False, "unsupported_work_type"
    return True, None


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return dot / norm if norm else 0.0


def _lexical(left: str, right: str) -> float:
    left_tokens = set(tokenize(left))
    right_tokens = set(tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return min(
        1.0,
        len(left_tokens & right_tokens) / math.sqrt(len(left_tokens) * len(right_tokens)),
    )


def rank_academic_candidates(
    candidates: Sequence[dict[str, Any]],
    scope_texts: Sequence[str],
    *,
    embeddings: Sequence[Sequence[float]] | None = None,
) -> list[dict[str, Any]]:
    """以标题+摘要对作用域论文做混合重排；Embedding 不可用时退回词项排序。"""

    raw_scope = [value.strip()[:3500] for value in scope_texts if value.strip()]
    clean_scope = [" ".join(value.split()) for value in raw_scope]
    scope_token_sets = [set(tokenize(value)) for value in clean_scope]
    scope_document_frequency: dict[str, int] = {}
    for tokens in scope_token_sets:
        for token in tokens:
            scope_document_frequency[token] = scope_document_frequency.get(token, 0) + 1
    consensus_required = math.ceil(len(clean_scope) / 2) if len(clean_scope) >= 3 else 1
    consensus_available = any(
        frequency >= consensus_required for frequency in scope_document_frequency.values()
    )
    candidate_texts = [
        " ".join(
            value
            for value in (
                str(item.get("title") or ""),
                str(item.get("title") or ""),
                str(item.get("abstract") or "")[:3000],
            )
            if value
        )
        for item in candidates
    ]
    semantic_ready = bool(
        clean_scope and embeddings and len(embeddings) == len(candidates) + len(clean_scope)
    )
    ranked: list[dict[str, Any]] = []
    for index, (candidate, candidate_text) in enumerate(zip(candidates, candidate_texts)):
        best_score = 0.0
        best_scope = ""
        best_lexical = 0.0
        best_semantic = 0.0
        for scope_index, scope_text in enumerate(clean_scope):
            lexical = _lexical(candidate_text, scope_text)
            semantic = 0.0
            if semantic_ready and embeddings:
                semantic = max(
                    0.0,
                    _cosine(
                        embeddings[index],
                        embeddings[len(candidates) + scope_index],
                    ),
                )
            score = 0.72 * semantic + 0.28 * lexical if semantic_ready else lexical
            if score > best_score:
                best_score = score
                best_scope = raw_scope[scope_index].splitlines()[0][:220]
                best_lexical = lexical
                best_semantic = semantic
        candidate_tokens = set(tokenize(candidate_text))
        anchor_frequency = max(
            (
                scope_document_frequency[token]
                for token in candidate_tokens
                if token in scope_document_frequency
            ),
            default=0,
        )
        enriched = dict(candidate)
        enriched["relevance_score"] = round(best_score, 6)
        enriched["rerank_mode"] = "semantic_lexical" if semantic_ready else "lexical"
        enriched["lexical_score"] = round(best_lexical, 6)
        enriched["semantic_score"] = round(best_semantic, 6)
        enriched["scope_document_count"] = len(clean_scope)
        enriched["scope_anchor_document_frequency"] = anchor_frequency
        enriched["scope_consensus_required"] = consensus_required
        enriched["scope_consensus_available"] = consensus_available
        if best_scope:
            enriched["matched_scope_title"] = best_scope
        ranked.append(enriched)
    ranked.sort(
        key=lambda item: (
            -float(item.get("relevance_score") or 0.0),
            -int(item.get("citation_count") or 0),
            -int(item.get("year") or 0),
            sorted(entity_keys(item))[0] if entity_keys(item) else "",
        )
    )
    return ranked


def passes_relevance_gate(item: dict[str, Any]) -> bool:
    """为 Precision@5 采用保守门禁，避免 Embedding 假高分带入跨领域论文。

    语义相似度只负责发现同义表达，不能单独放行候选；候选仍需与集合标题或
    摘要存在少量实词锚点。纯词项模式则使用更高的重合阈值。
    """

    lexical = float(item.get("lexical_score") or 0.0)
    semantic = float(item.get("semantic_score") or 0.0)
    if item.get("rerank_mode") == "semantic_lexical":
        base_match = lexical >= 0.025 and (semantic >= 0.55 or lexical >= 0.08)
    else:
        base_match = lexical >= 0.05
    if not base_match:
        return False
    if bool(item.get("scope_consensus_available")):
        return int(item.get("scope_anchor_document_frequency") or 0) >= int(
            item.get("scope_consensus_required") or 1
        )
    return True


def filter_and_deduplicate_candidates(
    candidates: Sequence[dict[str, Any]],
    *,
    excluded_keys: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    kept: list[dict[str, Any]] = []
    seen: set[str] = set(excluded_keys)
    seen_titles = {
        value.removeprefix("title:")
        for value in excluded_keys
        if value.startswith("title:")
    }
    stats = {"input": 0, "type_filtered": 0, "duplicate_filtered": 0}
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        stats["input"] += 1
        allowed, _reason = is_research_paper(raw)
        if not allowed:
            stats["type_filtered"] += 1
            continue
        keys = entity_keys(raw)
        title = next(
            (value.removeprefix("title:") for value in keys if value.startswith("title:")),
            "",
        )
        if (
            not keys
            or keys & seen
            or (title and any(_titles_equivalent(title, value) for value in seen_titles))
        ):
            stats["duplicate_filtered"] += 1
            continue
        seen.update(keys)
        if title:
            seen_titles.add(title)
        kept.append(dict(raw))
    return kept, stats
