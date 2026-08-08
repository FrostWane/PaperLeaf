"""根据用户文献库生成可解释、可降级的 arXiv 推荐。"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .arxiv_service import ArxivPaper

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,}|[\u4e00-\u9fff]{2,}")
_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)
_STOPWORDS = {
    "about", "across", "address", "advances", "after", "against", "all", "also", "among",
    "an", "analysis", "and", "approach", "are", "as", "at", "based", "be", "between",
    "by", "can", "data", "deep", "for", "from", "has", "have", "in", "into", "is",
    "its", "large", "learning", "method", "methods", "model", "models", "new", "of", "on",
    "our", "paper", "prediction", "results", "study", "such", "system", "systems", "the",
    "their", "these", "this", "through", "to", "towards", "use", "used", "using", "via",
    "was", "we",
    "were", "with", "without", "一种", "方法", "模型", "研究", "论文", "基于", "用于",
}


@dataclass(frozen=True)
class DiscoveryProfile:
    papers: tuple[Any, ...]
    seed: Any
    basis_paper_count: int
    existing_arxiv_ids: frozenset[str]
    topics: tuple[str, ...]
    search_phrases: tuple[str, ...]
    search_start: int


@dataclass(frozen=True)
class RankedRecommendation:
    paper: ArxivPaper
    matched_paper_title: str
    matched_terms: tuple[str, ...]
    match_type: str
    score: float


@dataclass(frozen=True)
class DiscoveryPaperSource:
    id: str
    title: str
    abstract: str
    publication: str | None
    arxiv_id: str | None
    last_opened_at: datetime | None
    updated_at: datetime | None
    created_at: datetime | None


def normalize_arxiv_id(value: str | None) -> str:
    return _ARXIV_VERSION_RE.sub("", (value or "").strip().casefold())


def with_indexed_text(
    papers: Sequence[Any], indexed_text_by_paper: dict[str, str]
) -> list[DiscoveryPaperSource]:
    """摘要缺失时补入已鉴权读取的页级索引文本，不修改数据库记录。"""

    return [
        DiscoveryPaperSource(
            id=str(getattr(paper, "id", "")),
            title=str(getattr(paper, "title", "") or ""),
            abstract=(
                str(getattr(paper, "abstract", "") or "").strip()
                or indexed_text_by_paper.get(str(getattr(paper, "id", "")), "")
            ),
            publication=getattr(paper, "publication", None),
            arxiv_id=getattr(paper, "arxiv_id", None),
            last_opened_at=getattr(paper, "last_opened_at", None),
            updated_at=getattr(paper, "updated_at", None),
            created_at=getattr(paper, "created_at", None),
        )
        for paper in papers
    ]


def _timestamp(paper: Any) -> float:
    value = (
        getattr(paper, "last_opened_at", None)
        or getattr(paper, "updated_at", None)
        or getattr(paper, "created_at", None)
    )
    if not isinstance(value, datetime):
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _raw_tokens(text: str) -> list[str]:
    # 学术标题中的连字符通常连接领域短语而非组成一个检索 Token；拆开后
    # ``drug-target`` 可形成 arXiv 更稳定的 ``drug target`` 短语。
    return _TOKEN_RE.findall((text or "").replace("-", " "))


def tokenize(text: str) -> list[str]:
    return [
        token.casefold()
        for token in _raw_tokens(text)
        if token.casefold() not in _STOPWORDS
    ]


def _paper_text(paper: Any) -> str:
    return " ".join(
        value
        for value in (
            str(getattr(paper, "title", "") or ""),
            str(getattr(paper, "abstract", "") or "")[:3000],
            str(getattr(paper, "publication", "") or ""),
        )
        if value
    )


def _looks_like_model_name(token: str) -> bool:
    uppercase = sum(character.isupper() for character in token)
    return any(character.isdigit() for character in token) or uppercase >= 2


def _search_phrases(seed: Any, topics: Sequence[str]) -> tuple[str, ...]:
    phrases: list[str] = []
    sources = (
        str(getattr(seed, "title", "") or ""),
        str(getattr(seed, "abstract", "") or "")[:1200],
    )
    for source in sources:
        usable = [
            token.casefold()
            for token in _raw_tokens(source)
            if token.casefold() not in _STOPWORDS and not _looks_like_model_name(token)
        ][:40]
        for index in range(0, len(usable) - 1, 2):
            phrase = f"{usable[index]} {usable[index + 1]}"
            if phrase not in phrases:
                phrases.append(phrase)
            if len(phrases) == 3:
                break
        if len(phrases) == 3:
            break
    if not phrases:
        phrases.extend(term for term in topics if term.isascii())
    return tuple(phrases[:4])


def build_discovery_profile(papers: Sequence[Any], batch: int) -> DiscoveryProfile | None:
    candidates = [paper for paper in papers if str(getattr(paper, "title", "") or "").strip()]
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda paper: (-_timestamp(paper), str(getattr(paper, "id", ""))),
    )[:12]
    seed_index = batch % len(ordered)
    page = batch // len(ordered)
    weighted: Counter[str] = Counter()
    for paper in ordered:
        weighted.update({token: 3 for token in tokenize(str(getattr(paper, "title", "") or ""))})
        weighted.update(tokenize(str(getattr(paper, "abstract", "") or ""))[:120])
    topics = tuple(
        token
        for token, _count in sorted(weighted.items(), key=lambda item: (-item[1], item[0]))[:12]
    )
    seed = ordered[seed_index]
    return DiscoveryProfile(
        papers=tuple(ordered),
        seed=seed,
        basis_paper_count=len(candidates),
        existing_arxiv_ids=frozenset(
            normalize_arxiv_id(str(getattr(paper, "arxiv_id", "") or ""))
            for paper in candidates
            if getattr(paper, "arxiv_id", None)
        ),
        topics=topics,
        search_phrases=_search_phrases(seed, topics),
        search_start=page * 20,
    )


def _lexical_similarity(left: str, right: str) -> tuple[float, tuple[str, ...]]:
    left_tokens = set(tokenize(left))
    right_tokens = set(tokenize(right))
    shared = tuple(sorted(left_tokens & right_tokens))
    if not left_tokens or not right_tokens:
        return 0.0, shared
    score = len(shared) / math.sqrt(len(left_tokens) * len(right_tokens))
    return min(score, 1.0), shared


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return dot / norm if norm else 0.0


def rank_recommendations(
    profile: DiscoveryProfile,
    candidates: Sequence[ArxivPaper],
    *,
    excluded_arxiv_ids: set[str],
    embeddings: Sequence[Sequence[float]] | None = None,
    positive_feedback_texts: Sequence[str] = (),
    negative_feedback_texts: Sequence[str] = (),
    limit: int = 6,
) -> list[RankedRecommendation]:
    existing = set(profile.existing_arxiv_ids)
    excluded = {normalize_arxiv_id(value) for value in excluded_arxiv_ids}
    deduplicated: list[ArxivPaper] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = normalize_arxiv_id(candidate.arxiv_id)
        if not normalized or normalized in existing or normalized in excluded or normalized in seen:
            continue
        seen.add(normalized)
        deduplicated.append(candidate)

    candidate_count = len(deduplicated)
    semantic_ready = bool(
        embeddings
        and len(embeddings)
        == (
            candidate_count
            + len(profile.papers)
            + len(positive_feedback_texts)
            + len(negative_feedback_texts)
        )
        and candidate_count
    )
    ranked: list[RankedRecommendation] = []
    for candidate_index, candidate in enumerate(deduplicated):
        candidate_text = f"{candidate.title} {candidate.abstract}"
        best_score = -1.0
        best_title = str(getattr(profile.seed, "title", ""))
        best_terms: tuple[str, ...] = ()
        best_semantic = 0.0
        for paper_index, library_paper in enumerate(profile.papers):
            lexical, terms = _lexical_similarity(candidate_text, _paper_text(library_paper))
            semantic = 0.0
            if semantic_ready and embeddings:
                semantic = max(
                    0.0,
                    _cosine(
                        embeddings[candidate_index],
                        embeddings[candidate_count + paper_index],
                    ),
                )
            combined = 0.72 * semantic + 0.28 * lexical if semantic_ready else lexical
            if combined > best_score:
                best_score = combined
                best_title = str(getattr(library_paper, "title", ""))
                best_terms = terms
                best_semantic = semantic
        feedback_offset = candidate_count + len(profile.papers)
        positive_similarity = 0.0
        for feedback_index, feedback_text in enumerate(positive_feedback_texts):
            lexical, _ = _lexical_similarity(candidate_text, feedback_text)
            semantic = 0.0
            if semantic_ready and embeddings:
                semantic = max(
                    0.0,
                    _cosine(
                        embeddings[candidate_index],
                        embeddings[feedback_offset + feedback_index],
                    ),
                )
            positive_similarity = max(
                positive_similarity,
                0.72 * semantic + 0.28 * lexical if semantic_ready else lexical,
            )
        negative_offset = feedback_offset + len(positive_feedback_texts)
        negative_similarity = 0.0
        for feedback_index, feedback_text in enumerate(negative_feedback_texts):
            lexical, _ = _lexical_similarity(candidate_text, feedback_text)
            semantic = 0.0
            if semantic_ready and embeddings:
                semantic = max(
                    0.0,
                    _cosine(
                        embeddings[candidate_index],
                        embeddings[negative_offset + feedback_index],
                    ),
                )
            negative_similarity = max(
                negative_similarity,
                0.72 * semantic + 0.28 * lexical if semantic_ready else lexical,
            )
        adjusted_score = min(
            1.0,
            max(0.0, best_score + 0.20 * positive_similarity - 0.18 * negative_similarity),
        )
        ranked.append(
            RankedRecommendation(
                paper=candidate,
                matched_paper_title=best_title,
                matched_terms=best_terms[:3],
                match_type="semantic" if semantic_ready and best_semantic > 0 else "topic",
                score=adjusted_score,
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.paper.arxiv_id))
    return ranked[:limit]


async def embed_discovery_texts(
    config: Any,
    model_router: Any,
    texts: list[str],
) -> list[list[float]] | None:
    """使用现有 Embedding 路由；任何故障都只让推荐退回关键词排序。"""

    if not texts or not model_router.has_provider("embedding"):
        return None
    from langchain_openai import OpenAIEmbeddings

    async def invoke(provider: Any) -> list[list[float]]:
        kwargs: dict[str, Any] = {
            "model": provider.embedding_model,
            "api_key": provider.api_key,
            "base_url": provider.base_url,
            "max_retries": 0,
            "check_embedding_ctx_length": False,
        }
        if config.embedding_dimensions:
            kwargs["dimensions"] = config.embedding_dimensions
        embeddings = OpenAIEmbeddings(**kwargs)
        vectors: list[list[float]] = []
        size = config.embedding_batch_size
        for index in range(0, len(texts), size):
            vectors.extend(await embeddings.aembed_documents(texts[index : index + size]))
        return vectors

    try:
        return await model_router.execute("embedding", invoke)
    except Exception:
        return None


async def collect_recommendations(
    profile: DiscoveryProfile,
    candidates: Sequence[ArxivPaper],
    *,
    config: Any,
    model_router: Any,
    excluded_arxiv_ids: set[str],
    positive_feedback_texts: Sequence[str] = (),
    negative_feedback_texts: Sequence[str] = (),
    limit: int,
    embedder: Callable[[Any, Any, list[str]], Awaitable[list[list[float]] | None]] = (
        embed_discovery_texts
    ),
) -> tuple[list[RankedRecommendation], str]:
    excluded = {normalize_arxiv_id(value) for value in excluded_arxiv_ids}
    existing = set(profile.existing_arxiv_ids)
    filtered: list[ArxivPaper] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = normalize_arxiv_id(candidate.arxiv_id)
        if not normalized or normalized in excluded or normalized in existing or normalized in seen:
            continue
        seen.add(normalized)
        filtered.append(candidate)
    if not filtered:
        return [], "keyword"
    texts = [f"{candidate.title} {candidate.abstract}"[:4000] for candidate in filtered]
    texts.extend(_paper_text(paper)[:4000] for paper in profile.papers)
    texts.extend(text[:4000] for text in positive_feedback_texts)
    texts.extend(text[:4000] for text in negative_feedback_texts)
    embeddings = await embedder(config, model_router, texts)
    ranked = rank_recommendations(
        profile,
        filtered,
        excluded_arxiv_ids=excluded_arxiv_ids,
        embeddings=embeddings,
        positive_feedback_texts=positive_feedback_texts,
        negative_feedback_texts=negative_feedback_texts,
        limit=limit,
    )
    return ranked, "semantic_keyword" if embeddings else "keyword"
