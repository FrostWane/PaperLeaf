"""保持物理页边界的确定性切块。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PageText:
    paper_id: str
    physical_page: int
    text: str


@dataclass(frozen=True)
class PageChunk:
    id: str
    paper_id: str
    physical_page: int
    chunk_index: int
    text: str
    token_count: int


def _units(text: str) -> list[str]:
    """中英文兼容的轻量切分器；正式 Token 计数在嵌入适配器中复核。"""
    return re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*|[^\s]", text)


def _join(units: list[str]) -> str:
    result: list[str] = []
    previous_ascii = False
    for unit in units:
        ascii_word = bool(re.fullmatch(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", unit))
        if result and previous_ascii and ascii_word:
            result.append(" ")
        result.append(unit)
        previous_ascii = ascii_word
    return "".join(result)


def chunk_pages(
    pages: list[PageText], *, target_tokens: int = 700, overlap_tokens: int = 100
) -> list[PageChunk]:
    if target_tokens <= 0:
        raise ValueError("target_tokens 必须为正数")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens 必须小于 target_tokens")

    chunks: list[PageChunk] = []
    step = target_tokens - overlap_tokens
    for page in pages:
        if page.physical_page < 1:
            raise ValueError("物理页码必须从 1 开始")
        units = _units(page.text.strip())
        if not units:
            continue
        for chunk_index, start in enumerate(range(0, len(units), step)):
            window = units[start : start + target_tokens]
            if not window:
                break
            chunks.append(
                PageChunk(
                    id=f"{page.paper_id}:p{page.physical_page}:c{chunk_index}",
                    paper_id=page.paper_id,
                    physical_page=page.physical_page,
                    chunk_index=chunk_index,
                    text=_join(window),
                    token_count=len(window),
                )
            )
            if start + target_tokens >= len(units):
                break
    return chunks
