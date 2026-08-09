"""保持物理页与文档结构的确定性混合切分。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PageText:
    paper_id: str
    physical_page: int
    text: str


@dataclass(frozen=True)
class SemanticUnit:
    """可嵌入的页内最小语义单元。"""

    id: str
    paper_id: str
    physical_page: int
    unit_index: int
    text: str
    token_count: int
    kind: str


@dataclass(frozen=True)
class PageChunk:
    id: str
    paper_id: str
    physical_page: int
    chunk_index: int
    text: str
    token_count: int


_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*")
_SECTION_HEADING_RE = re.compile(
    r"^(?:\d+(?:\.\d+){0,3}[.)]?\s+|[IVXLC]+[.)]\s+)?"
    r"(?:abstract|introduction|background|related\s+work|method(?:ology)?|"
    r"materials?\s+and\s+methods?|experiments?|results?|discussion|conclusion|"
    r"limitations?|references|appendix|摘要|引言|背景|相关工作|方法|实验|结果|"
    r"讨论|结论|局限|参考文献|附录)\b",
    re.IGNORECASE,
)
_NUMBERED_HEADING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+){0,3}|[IVXLC]+)[.)]?\s+\S+")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+(?=[A-Z0-9(])")
_FORMULA_RE = re.compile(
    r"(?:[=≈≃≤≥∑∏∫√]|\\(?:frac|sum|prod|int|sqrt|alpha|beta)|\([0-9]{1,3}\)\s*$)"
)
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def sanitize_pdf_text(value: str) -> str:
    """移除 NUL 与无语义控制字符，同时保留表格和段落边界。"""

    normalized = value.replace("\x0c", "\n")
    return _UNSAFE_CONTROL_RE.sub("", normalized)


def _tokens(text: str) -> list[str]:
    """中英文兼容的稳定近似 Token；实际嵌入长度仍由模型适配器约束。"""

    return re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*|[^\s]", text)


def _token_count(text: str) -> int:
    return len(_tokens(text))


def _is_heading(text: str) -> bool:
    value = re.sub(r"\s+", " ", text).strip()
    if not value or len(value) > 140 or value.endswith(("。", ".", ";", "；", ",", "，")):
        return False
    if _SECTION_HEADING_RE.match(value) or _NUMBERED_HEADING_RE.match(value):
        return True
    ascii_letters = re.sub(r"[^A-Za-z]", "", value)
    return bool(ascii_letters) and len(ascii_letters) >= 4 and ascii_letters.isupper()


def _line_kind(text: str) -> str:
    if _is_heading(text):
        return "heading"
    if text.count("|") >= 2 or "\t" in text or re.search(r"\S\s{3,}\S", text):
        return "table"
    if _FORMULA_RE.search(text) and len(text) <= 240:
        return "formula"
    return "paragraph"


def _structural_blocks(text: str) -> list[tuple[str, str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    result: list[tuple[str, str]] = []
    for raw_block in re.split(r"\n\s*\n+", normalized):
        lines = [line.strip() for line in raw_block.splitlines() if line.strip()]
        if not lines:
            continue
        current_kind: str | None = None
        current: list[str] = []

        def flush() -> None:
            nonlocal current_kind, current
            if not current or current_kind is None:
                return
            separator = "\n" if current_kind in {"table", "formula"} else " "
            result.append((separator.join(current).strip(), current_kind))
            current_kind, current = None, []

        for line in lines:
            kind = _line_kind(line)
            # 标题始终独立；表格/公式只与相同结构的相邻行合并。
            if kind == "heading":
                flush()
                result.append((re.sub(r"[ \t]+", " ", line), kind))
                continue
            if current_kind is not None and current_kind != kind:
                flush()
            current_kind = kind
            current.append(line if kind in {"table", "formula"} else re.sub(r"[ \t]+", " ", line))
        flush()
    return result


def _join_tokens(units: list[str]) -> str:
    result: list[str] = []
    previous_ascii = False
    for unit in units:
        ascii_word = bool(_ASCII_WORD_RE.fullmatch(unit))
        if result and previous_ascii and ascii_word:
            result.append(" ")
        result.append(unit)
        previous_ascii = ascii_word
    return "".join(result)


def _token_windows(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    raw = _tokens(text)
    if not raw:
        return []
    overlap = min(max(0, overlap_tokens), max_tokens - 1)
    step = max_tokens - overlap
    return [
        _join_tokens(raw[start : start + max_tokens])
        for start in range(0, len(raw), step)
        if raw[start : start + max_tokens]
    ]


def _sentence_overlap_tail(sentences: list[str], overlap_tokens: int) -> list[str]:
    tail: list[str] = []
    total = 0
    for sentence in reversed(sentences):
        size = _token_count(sentence)
        if size > overlap_tokens or (tail and total + size > overlap_tokens):
            break
        tail.append(sentence)
        total += size
    return list(reversed(tail))


def _split_oversized(
    text: str, max_tokens: int, overlap_tokens: int, *, kind: str
) -> list[str]:
    if _token_count(text) <= max_tokens:
        return [text.strip()]
    if kind in {"table", "formula"} and "\n" in text:
        sentences = [item.strip() for item in text.splitlines() if item.strip()]
        separator = "\n"
    else:
        sentences = [item.strip() for item in _SENTENCE_BOUNDARY_RE.split(text) if item.strip()]
        separator = " "
    if len(sentences) <= 1:
        return _token_windows(text, max_tokens, overlap_tokens)
    groups: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in sentences:
        sentence_count = _token_count(sentence)
        if sentence_count > max_tokens:
            if current:
                groups.append(separator.join(current).strip())
                current, count = [], 0
            groups.extend(_token_windows(sentence, max_tokens, overlap_tokens))
            continue
        if current and count + sentence_count > max_tokens:
            groups.append(separator.join(current).strip())
            current = _sentence_overlap_tail(current, overlap_tokens)
            count = sum(_token_count(item) for item in current)
            if current and count + sentence_count > max_tokens:
                current, count = [], 0
        current.append(sentence)
        count += sentence_count
    if current:
        rendered = separator.join(current).strip()
        if not groups or rendered != groups[-1]:
            groups.append(rendered)
    return groups


def build_semantic_units(
    pages: list[PageText], *, max_unit_tokens: int = 220, overlap_tokens: int = 40
) -> list[SemanticUnit]:
    if max_unit_tokens <= 0:
        raise ValueError("max_unit_tokens 必须为正数")
    units: list[SemanticUnit] = []
    for page in pages:
        if page.physical_page < 1:
            raise ValueError("物理页码必须从 1 开始")
        page_units: list[tuple[str, str]] = []
        for paragraph, kind in _structural_blocks(sanitize_pdf_text(page.text).strip()):
            for part in _split_oversized(
                paragraph,
                max_unit_tokens,
                min(overlap_tokens, max_unit_tokens - 1),
                kind=kind,
            ):
                if part:
                    page_units.append((part, kind))
        for unit_index, (text, kind) in enumerate(page_units):
            units.append(
                SemanticUnit(
                    id=f"{page.paper_id}:p{page.physical_page}:u{unit_index}",
                    paper_id=page.paper_id,
                    physical_page=page.physical_page,
                    unit_index=unit_index,
                    text=text,
                    token_count=_token_count(text),
                    kind=kind,
                )
            )
    return units


def _join_units(units: list[SemanticUnit]) -> str:
    return "\n\n".join(unit.text.strip() for unit in units if unit.text.strip()).strip()


def _overlap_tail(units: list[SemanticUnit], overlap_tokens: int) -> list[SemanticUnit]:
    if overlap_tokens <= 0:
        return []
    tail: list[SemanticUnit] = []
    total = 0
    for unit in reversed(units):
        if unit.kind == "heading" or (tail and total + unit.token_count > overlap_tokens):
            break
        if unit.token_count > overlap_tokens and not tail:
            break
        tail.append(unit)
        total += unit.token_count
        if total >= overlap_tokens:
            break
    return list(reversed(tail))


def chunk_pages(
    pages: list[PageText],
    *,
    target_tokens: int = 700,
    overlap_tokens: int = 100,
    max_unit_tokens: int = 220,
) -> list[PageChunk]:
    """以段落/标题为优先边界，在页内生成可引用且有长度保护的 Chunk。"""

    if target_tokens <= 0:
        raise ValueError("target_tokens 必须为正数")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens 必须小于 target_tokens")
    if max_unit_tokens <= 0:
        raise ValueError("max_unit_tokens 必须为正数")
    max_unit_tokens = min(max_unit_tokens, target_tokens)

    all_units = build_semantic_units(
        pages,
        max_unit_tokens=max_unit_tokens,
        overlap_tokens=min(overlap_tokens, max(0, max_unit_tokens // 3)),
    )
    by_page: dict[tuple[str, int], list[SemanticUnit]] = {}
    for unit in all_units:
        by_page.setdefault((unit.paper_id, unit.physical_page), []).append(unit)

    chunks: list[PageChunk] = []
    for page in pages:
        units = by_page.get((page.paper_id, page.physical_page), [])
        if not units:
            continue
        page_chunks: list[list[SemanticUnit]] = []
        current: list[SemanticUnit] = []
        current_tokens = 0

        for unit in units:
            structural_break = unit.kind == "heading" and current and any(
                item.kind != "heading" for item in current
            )
            would_overflow = bool(current) and current_tokens + unit.token_count > target_tokens
            if structural_break or would_overflow:
                emitted = list(current)
                page_chunks.append(emitted)
                current = _overlap_tail(emitted, overlap_tokens)
                current_tokens = sum(item.token_count for item in current)
                # 重叠不能挤占新标题或导致新单元再次溢出。
                if structural_break or current_tokens + unit.token_count > target_tokens:
                    current, current_tokens = [], 0
            current.append(unit)
            current_tokens += unit.token_count
        if current:
            # 若尾部只剩下上一块的纯重叠副本，不重复写入。
            if not page_chunks or _join_units(current) != _join_units(page_chunks[-1]):
                page_chunks.append(current)

        for chunk_index, group in enumerate(page_chunks):
            text = _join_units(group)
            chunks.append(
                PageChunk(
                    id=f"{page.paper_id}:p{page.physical_page}:c{chunk_index}",
                    paper_id=page.paper_id,
                    physical_page=page.physical_page,
                    chunk_index=chunk_index,
                    text=text,
                    token_count=_token_count(text),
                )
            )
    return chunks


def chunk_pages_fixed_window(
    pages: list[PageText], *, target_tokens: int = 700, overlap_tokens: int = 100
) -> list[PageChunk]:
    """V1 固定窗口降级算法；只在结构切分异常或离线对照评测时使用。"""

    if target_tokens <= 0:
        raise ValueError("target_tokens 必须为正数")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens 必须小于 target_tokens")
    chunks: list[PageChunk] = []
    step = target_tokens - overlap_tokens
    for page in pages:
        if page.physical_page < 1:
            raise ValueError("物理页码必须从 1 开始")
        units = _tokens(sanitize_pdf_text(page.text).strip())
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
                    text=_join_tokens(window),
                    token_count=len(window),
                )
            )
            if start + target_tokens >= len(units):
                break
    return chunks
