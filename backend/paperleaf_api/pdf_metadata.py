"""保守提取并回填 PDF 内置元数据与首页出版信息。

标题优先读取 PDF 自带的 metadata 字典；作者、年份、DOI 与出版物另有保守的首页
后备规则。提取和回填分开实现，使 Worker 可以在最终提交前依据数据库中的最新值
决定是否回填，从而避免覆盖解析期间发生的用户编辑。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE_RE = re.compile(r"\s+")
_SPACING_DIAERESIS_RE = re.compile(r"([AEIOUYaeiouy])\u00a8\s*")
_STRONG_AUTHOR_SEPARATOR_RE = re.compile(
    r"\s*(?:;|；|\||、|\r?\n)\s*|\s+(?:and|&)\s+",
    flags=re.IGNORECASE,
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_MISSING_TITLE_VALUES = {"n/a", "none", "null", "unknown", "untitled"}
_MISSING_AUTHOR_VALUES = {"n/a", "none", "null", "unknown"}
_PRODUCTION_TITLE_RE = re.compile(
    r"^(?:op|proof|manuscript|document|article)[-_: ]?[a-z0-9._ -]*\d[a-z0-9._ -]*$",
    re.IGNORECASE,
)
_ABSTRACT_RE = re.compile(r"^\s*(?:abstract|摘要)\s*(?:[-—:]|$)", re.IGNORECASE)
_AFFILIATION_RE = re.compile(
    r"(?:university|department|institute|laboratory|school|college|hospital|"
    r"correspondence|corresponding|@|大学|学院|研究所|实验室)",
    re.IGNORECASE,
)
_AUTHOR_MARK_RE = re.compile(r"(?:,|，|\band\b|\s&\s)", re.IGNORECASE)
_AUTHOR_FOOTNOTE_RE = re.compile(r"(?<=[^\W\d_])[\d*†‡]+(?=\s|[,，;；]|$)")
_PUBLICATION_YEAR_PATTERNS = (
    re.compile(r"date of publication.{0,80}?\b((?:19|20)\d{2})\b", re.IGNORECASE),
    re.compile(r"arxiv:[^\n]{0,100}?\b((?:19|20)\d{2})\b", re.IGNORECASE),
    re.compile(r"(?:©|copyright|the author\(s\))\s*\b((?:19|20)\d{2})\b", re.IGNORECASE),
    re.compile(r"\bpublished\b[^\n]{0,80}?\b((?:19|20)\d{2})\b", re.IGNORECASE),
)
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_DOI_PREFIX_RE = re.compile(
    r"^\s*(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE
)
_DOI_CONTEXT_RE = re.compile(r"(?:\bdoi\s*:|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
_PUBLICATION_LABEL_RE = re.compile(
    r"^\s*(?:published\s+in|publication|journal|venue)\s*[:\-–—]\s*(?P<value>.+)$",
    re.IGNORECASE,
)
_PROCEEDINGS_RE = re.compile(
    r"\b(?:in\s+)?proceedings\s+of\s+(?:the\s+)?[^\n]{4,240}", re.IGNORECASE
)
_NAMED_VENUE_RE = re.compile(
    r"(?:\bjournal\s+of\b|\btransactions\s+on\b|\bproceedings\s+of\b|"
    r"\bconference\s+on\b|\bsymposium\s+on\b|\bworkshop\s+on\b)",
    re.IGNORECASE,
)
_VOLUME_HEADER_RE = re.compile(
    r"^(?P<venue>[A-Z][^\n]{2,160}?),\s*"
    r"(?:vol(?:ume)?\.?\s*)?\d{1,4}(?:\s*\(\s*\d+\s*\))?\s*,\s*"
    r"(?:19|20)\d{2}(?:\s*[,;:]|$)",
)
_PUBLICATION_TRAILER_RE = re.compile(
    r"\s*(?:[,;|]\s*)?(?:vol(?:ume)?\.?\s*\d|issue\s*\d|no\.?\s*\d|"
    r"pp?\.?\s*\d|pages?\s+\d|(?:19|20)\d{2}\b|doi\s*:).*$",
    re.IGNORECASE,
)
_NON_PUBLICATION_RE = re.compile(
    r"(?:\barxiv\b|\bpreprint\b|\bdownloaded\s+from\b|\blocal\s+(?:file|document)\b|"
    r"\bdepartment\b|\buniversity\b|\binstitute\b|\blaboratory\b|\bschool\b|"
    r"\bcollege\b|\bhospital\b|\bcorrespondence\b|@)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PdfMetadata:
    """经过清洗、可以安全参与回填的 PDF 元数据。"""

    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    publication: str | None = None
    doi: str | None = None


class PaperMetadataTarget(Protocol):
    title: str
    authors: list[str]
    year: int | None
    publication: str | None
    doi: str | None
    filename: str
    arxiv_id: str | None


def _clean_text(value: object, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value)
    normalized = _SPACING_DIAERESIS_RE.sub(
        lambda match: unicodedata.normalize("NFC", f"{match.group(1)}\u0308"),
        normalized,
    )
    normalized = _CONTROL_RE.sub(" ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    if not normalized:
        return None
    return normalized[:max_length].rstrip()


def _metadata_value(metadata: Mapping[str, object], key: str) -> object | None:
    for candidate, value in metadata.items():
        if isinstance(candidate, str) and candidate.casefold() == key.casefold():
            return value
    return None


def _clean_title(value: object) -> str | None:
    title = _clean_text(value, max_length=1000)
    if (
        not title
        or title.casefold().rstrip(".") in _MISSING_TITLE_VALUES
        or _PRODUCTION_TITLE_RE.fullmatch(title)
    ):
        return None
    return title


def normalize_doi(value: object) -> str | None:
    """规范化 DOI；拒绝空白、URL 参数和非 ``10.`` 注册前缀。"""

    cleaned = _clean_text(value, max_length=500)
    if not cleaned:
        return None
    cleaned = _DOI_PREFIX_RE.sub("", cleaned, count=1)
    cleaned = cleaned.split("?", 1)[0].split("#", 1)[0].strip()
    if len(cleaned) > 255:
        return None
    match = _DOI_RE.fullmatch(cleaned)
    if not match:
        return None
    doi = match.group(0).rstrip(".,;:")
    # 句末右括号经常紧跟 DOI；仅删除没有配对的结束括号。
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    return doi.casefold() if _DOI_RE.fullmatch(doi) else None


def extract_first_page_doi(text: str) -> str | None:
    """只从带 ``doi:`` 或 ``doi.org`` 明确信号的首页行提取 DOI。"""

    for raw_line in text.splitlines()[:80]:
        line = _clean_text(raw_line, max_length=1000)
        if not line:
            continue
        if _ABSTRACT_RE.match(line) or re.match(
            r"^\s*(?:1\.?\s+)?introduction\b", line, re.IGNORECASE
        ):
            break
        if not _DOI_CONTEXT_RE.search(line):
            continue
        match = _DOI_RE.search(line)
        if match and (doi := normalize_doi(match.group(0))):
            return doi
    return None


def _clean_publication(value: object) -> str | None:
    publication = _clean_text(value, max_length=300)
    if not publication or _NON_PUBLICATION_RE.search(publication):
        return None
    publication = _PUBLICATION_TRAILER_RE.sub("", publication).strip(" ,;:|.-–—")
    if not publication or len(publication) < 4:
        return None
    return publication


def extract_first_page_publication(text: str) -> str | None:
    """从首页提取高置信出版物候选，宁可留空也不把单位或 arXiv 当期刊。"""

    lines = [
        line
        for raw in text.splitlines()[:80]
        if (line := _clean_text(raw, max_length=500))
    ]
    for line in lines:
        if _ABSTRACT_RE.match(line) or re.match(
            r"^\s*(?:1\.?\s+)?introduction\b", line, re.IGNORECASE
        ):
            break
        if _NON_PUBLICATION_RE.search(line):
            continue
        if match := _PUBLICATION_LABEL_RE.match(line):
            if publication := _clean_publication(match.group("value")):
                return publication
        if match := _VOLUME_HEADER_RE.match(line):
            if publication := _clean_publication(match.group("venue")):
                return publication
        if match := _PROCEEDINGS_RE.search(line):
            if publication := _clean_publication(match.group(0)):
                return publication
        if _NAMED_VENUE_RE.search(line):
            if publication := _clean_publication(line):
                return publication
    return None


def extract_pdf_publication(metadata: Mapping[str, object] | None) -> str | None:
    """读取 PDF 内置的明确出版物字段；普通 subject 不会被无条件采用。"""

    if not metadata:
        return None
    for key in ("publication", "journal", "journal_title", "container-title"):
        if publication := _clean_publication(_metadata_value(metadata, key)):
            return publication
    subject = _clean_text(_metadata_value(metadata, "subject"), max_length=500)
    if subject:
        if match := _PUBLICATION_LABEL_RE.match(subject):
            return _clean_publication(match.group("value"))
        if _NAMED_VENUE_RE.search(subject):
            return _clean_publication(subject)
    return None


def _looks_like_complete_name(value: str) -> bool:
    words = value.split()
    return len(words) >= 2 or (bool(_CJK_RE.search(value)) and len(value) >= 2)


def _split_authors(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    raw = unicodedata.normalize("NFC", value).replace("\x00", " ").strip()
    if not raw:
        return ()

    has_strong_separator = bool(_STRONG_AUTHOR_SEPARATOR_RE.search(raw))
    parts = _STRONG_AUTHOR_SEPARATOR_RE.split(raw) if has_strong_separator else [raw]
    if not has_strong_separator and ("," in raw or "，" in raw):
        comma_parts = [
            item
            for item in (_clean_text(part, max_length=300) for part in re.split(r"[,，]", raw))
            if item
        ]
        # 单个逗号也可能是 `姓, 名`。只有每段都像完整姓名时才把逗号当作者分隔符。
        if len(comma_parts) > 1 and all(_looks_like_complete_name(item) for item in comma_parts):
            parts = comma_parts

    authors: list[str] = []
    seen: set[str] = set()
    for part in parts:
        author = _clean_text(part, max_length=300)
        if not author or author.casefold().rstrip(".") in _MISSING_AUTHOR_VALUES:
            continue
        identity = author.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        authors.append(author)
        if len(authors) == 50:
            break
    return tuple(authors)


def extract_pdf_metadata(metadata: Mapping[str, object] | None) -> PdfMetadata:
    """从 PyMuPDF ``document.metadata`` 的结果中提取可信字段。"""

    if not metadata:
        return PdfMetadata()
    doi = None
    for key in ("doi", "identifier"):
        if doi := normalize_doi(_metadata_value(metadata, key)):
            break
    if doi is None:
        subject = _clean_text(_metadata_value(metadata, "subject"), max_length=500)
        doi = extract_first_page_doi(subject or "")
    return PdfMetadata(
        title=_clean_title(_metadata_value(metadata, "title")),
        authors=_split_authors(_metadata_value(metadata, "author")),
        # PDF CreationDate 表示文件被创建/导出的时间，并不等于论文发表年份。
        # 发表年份只从首页带明确出版语义的文本中提取，宁可留空也不写入伪元数据。
        year=None,
        publication=extract_pdf_publication(metadata),
        doi=doi,
    )


def _looks_like_author_name(value: str) -> bool:
    if not value or _AFFILIATION_RE.search(value):
        return False
    words = value.replace("-", " ").split()
    if _CJK_RE.search(value):
        return 2 <= len(value.replace(" ", "")) <= 20
    return 2 <= len(words) <= 7 and all(
        any(character.isalpha() for character in word) for word in words
    )


def _parse_author_line(value: str) -> tuple[str, ...]:
    normalized = _AUTHOR_FOOTNOTE_RE.sub("", value)
    normalized = re.sub(r"\s*[,，]\s*[,，]+\s*", ", ", normalized)
    normalized = re.sub(r"\s+(?:and|&)\s+", ", ", normalized, flags=re.IGNORECASE)
    parts = [
        item
        for item in (
            _clean_text(part.strip(" ,，;；*†‡"), max_length=200)
            for part in re.split(r"[,，;；]", normalized)
        )
        if item
    ]
    if len(parts) < 2 or not all(_looks_like_author_name(item) for item in parts):
        return ()
    return tuple(dict.fromkeys(parts))


def extract_first_page_authors(text: str, title: str | None = None) -> tuple[str, ...]:
    """从首页标题之后、摘要或单位之前提取至少两位高置信作者。

    这是 PDF 内置元数据缺失时的保守后备。首个候选行必须包含逗号或 ``and``，
    且拆出的每一项都像姓名；无法稳定判断的版式保持未识别。
    """

    lines = [line for raw in text.splitlines() if (line := _clean_text(raw, max_length=500))]
    if not lines:
        return ()
    start = 0
    title_key = _compact_key(title)
    if title_key:
        for index, line in enumerate(lines[:20]):
            line_key = _compact_key(line)
            if line_key and (line_key.startswith(title_key) or title_key.startswith(line_key)):
                start = index + 1
                break

    author_start: int | None = None
    for index in range(start, min(len(lines), 30)):
        line = lines[index]
        if _ABSTRACT_RE.match(line) or _AFFILIATION_RE.search(line):
            break
        if _AUTHOR_MARK_RE.search(line) and _parse_author_line(line):
            author_start = index
            break
    if author_start is None:
        return ()

    author_lines: list[str] = []
    for line in lines[author_start : min(author_start + 8, len(lines))]:
        if _ABSTRACT_RE.match(line) or _AFFILIATION_RE.search(line):
            break
        author_lines.append(line)
    return _parse_author_line(" ".join(author_lines))


def extract_first_page_year(text: str) -> int | None:
    """从带明确出版语义的首页行提取年份，不把下载或 PDF 创建时间当出版年。"""

    for pattern in _PUBLICATION_YEAR_PATTERNS:
        match = pattern.search(text)
        if match:
            year = int(match.group(1))
            if 1000 <= year <= datetime.now(timezone.utc).year + 1:
                return year
    return None


def _title_key(value: str | None) -> str:
    cleaned = _clean_text(value, max_length=1000)
    return cleaned.casefold() if cleaned else ""


def _compact_key(value: str | None) -> str:
    return "".join(character for character in _title_key(value) if character.isalnum())


def is_generated_title(title: str | None, filename: str, arxiv_id: str | None) -> bool:
    """判断标题是否仍是上传或 arXiv 导入时生成的机器占位值。"""

    current = _title_key(title)
    if not current:
        return True
    filename_path = Path(filename)
    if current in {_title_key(filename_path.name), _title_key(filename_path.stem)}:
        return True
    if arxiv_id and _compact_key(title) == f"arxiv{_compact_key(arxiv_id)}":
        return True
    return False


def backfill_pdf_metadata(paper: PaperMetadataTarget, metadata: PdfMetadata) -> bool:
    """只回填空字段或机器生成标题，返回是否实际修改了对象。"""

    changed = False
    if metadata.title and is_generated_title(paper.title, paper.filename, paper.arxiv_id):
        if paper.title != metadata.title:
            paper.title = metadata.title
            changed = True
    current_authors = paper.authors or []
    if metadata.authors and not any(_clean_text(item, max_length=300) for item in current_authors):
        paper.authors = list(metadata.authors)
        changed = True
    if metadata.year is not None and paper.year is None:
        paper.year = metadata.year
        changed = True
    if metadata.publication and not getattr(paper, "publication", None):
        paper.publication = metadata.publication
        changed = True
    if metadata.doi and not getattr(paper, "doi", None):
        paper.doi = metadata.doi
        changed = True
    return changed
