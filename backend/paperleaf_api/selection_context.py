"""PDF 文本层选文的确定性校验。

浏览器 PDF.js 文本层与服务端 PyMuPDF 抽取结果可能在连字、换行断词和
Unicode 标点上不同。这里仅做可复现的规范化与有界模糊匹配，不调用模型。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

_LIGATURES = str.maketrans(
    {
        "ﬀ": "ff",
        "ﬁ": "fi",
        "ﬂ": "fl",
        "ﬃ": "ffi",
        "ﬄ": "ffl",
        "ﬅ": "st",
        "ﬆ": "st",
        "\u00ad": "",
    }
)
_PUNCTUATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "‐": "-",
        "‑": "-",
        " ": " ",
    }
)
_BROKEN_WORD = re.compile(r"(?<=[A-Za-z])[-‐‑]\s+(?=[A-Za-z])")
_WHITESPACE = re.compile(r"\s+")
_COMPACT_IGNORED = re.compile(r"[^0-9a-z\u3400-\u9fff]+")


def canonicalize_pdf_text(value: str) -> str:
    """统一常见 PDF 文本层差异，同时保留可读空格。"""

    normalized = unicodedata.normalize("NFKC", value).translate(_LIGATURES)
    normalized = normalized.translate(_PUNCTUATION)
    normalized = _BROKEN_WORD.sub("", normalized)
    return _WHITESPACE.sub(" ", normalized).strip().casefold()


def selection_hash(value: str) -> str:
    return hashlib.sha256(canonicalize_pdf_text(value).encode("utf-8")).hexdigest()


def _compact(value: str) -> str:
    return _COMPACT_IGNORED.sub("", canonicalize_pdf_text(value))


def _compact_with_positions(value: str) -> tuple[str, list[int]]:
    canonical = canonicalize_pdf_text(value)
    characters: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(canonical):
        if _COMPACT_IGNORED.fullmatch(character):
            continue
        characters.append(character)
        positions.append(index)
    return "".join(characters), positions


@dataclass(frozen=True)
class SelectionMatch:
    accepted: bool
    canonical_text: str
    canonical_hash: str
    mode: str
    score: float


def _candidate_starts(page: str, selected: str) -> list[int]:
    if not page or not selected:
        return []
    gram_length = min(8, max(4, len(selected) // 8))
    grams = {
        selected[index : index + gram_length]
        for index in range(0, max(1, len(selected) - gram_length + 1), gram_length)
        if len(selected[index : index + gram_length]) == gram_length
    }
    starts: set[int] = set()
    for gram in grams:
        offset = 0
        while True:
            found = page.find(gram, offset)
            if found < 0:
                break
            starts.add(max(0, found - len(selected) // 2))
            offset = found + 1
            if len(starts) >= 80:
                return sorted(starts)
    if starts:
        return sorted(starts)
    step = max(8, len(selected) // 3)
    return list(range(0, max(1, len(page) - len(selected) + 1), step))[:200]


def match_selection_to_page(selected_text: str, page_text: str) -> SelectionMatch:
    """验证选文确实来自指定物理页。

    短文本只接受规范化后的精确包含。较长文本允许 PDF 文本层的小范围差异，
    但要求匹配字符覆盖率和窗口相似度同时达到门槛。
    """

    selected = canonicalize_pdf_text(selected_text)
    page = canonicalize_pdf_text(page_text)
    digest = selection_hash(selected_text)
    if not selected or not page:
        return SelectionMatch(False, selected, digest, "empty", 0.0)
    if selected in page:
        start = page.index(selected)
        trusted = page[start : start + len(selected)]
        return SelectionMatch(True, trusted, selection_hash(trusted), "canonical_exact", 1.0)

    selected_compact = _compact(selected)
    page_compact, page_positions = _compact_with_positions(page)
    if len(selected_compact) < 20:
        return SelectionMatch(False, selected, digest, "short_not_exact", 0.0)

    best_ratio = 0.0
    best_coverage = 0.0
    best_span: tuple[int, int] | None = None
    for start in _candidate_starts(page_compact, selected_compact):
        for factor in (0.9, 1.0, 1.1):
            length = max(20, round(len(selected_compact) * factor))
            candidate = page_compact[start : start + length]
            if not candidate:
                continue
            matcher = SequenceMatcher(None, selected_compact, candidate, autojunk=False)
            ratio = matcher.ratio()
            coverage = sum(block.size for block in matcher.get_matching_blocks()) / len(
                selected_compact
            )
            if (coverage, ratio) > (best_coverage, best_ratio):
                best_coverage, best_ratio = coverage, ratio
                best_span = (start, min(len(page_compact), start + length))
    accepted = best_coverage >= 0.92 and best_ratio >= 0.88
    trusted = selected
    if accepted and best_span and page_positions:
        compact_start, compact_end = best_span
        if compact_start < len(page_positions) and compact_end > compact_start:
            page_start = page_positions[compact_start]
            page_end = page_positions[min(compact_end - 1, len(page_positions) - 1)] + 1
            trusted = page[page_start:page_end].strip()
    return SelectionMatch(
        accepted,
        trusted,
        selection_hash(trusted),
        "ordered_fuzzy" if accepted else "not_on_page",
        round(min(best_coverage, best_ratio), 6),
    )
