"""论文发现任务的确定性数量与学术来源策略。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SOURCE_ALIASES = {
    "mcp__academic__search_openalex": (
        r"open\s*alex",
    ),
    "mcp__academic__search_semantic_scholar": (
        r"semantic\s*scholar",
        r"semanticscholar",
    ),
    "search_arxiv": (
        r"arxiv",
        r"arXiv",
    ),
}
_NEGATION_PREFIX_RE = re.compile(
    r"(?:不要(?:再)?(?:使用|用)?|不(?:要)?(?:使用|用)|不用|别(?:再)?(?:使用|用)|"
    r"禁止(?:使用)?|排除|避开|without|do\s+not\s+use|don't\s+use|"
    r"exclude|excluding)\s*$",
    re.IGNORECASE,
)
_NEGATION_SUFFIX_RE = re.compile(
    r"^\s*(?:不要|不用|除外|排除|禁用|not\s+allowed|excluded)",
    re.IGNORECASE,
)
_NEGATION_CLAUSE_RE = re.compile(
    r"(?:不要|不使用|不用|别用|别使用|禁止|排除|避开|without|"
    r"do\s+not\s+use|don't\s+use|exclude|excluding)",
    re.IGNORECASE,
)
_EXCLUSIVE_SOURCE_RE = re.compile(
    r"(?<!不)(?:只|仅)(?:使用|用|调用|搜索|检索)?|\bonly\s+(?:use|query|search)",
    re.IGNORECASE,
)
_ARABIC_COUNT_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:篇|papers?\b)", re.IGNORECASE)
_CHINESE_COUNT_RE = re.compile(r"([一二两三四五六七八九十]{1,3})\s*篇")
_ENGLISH_COUNT_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+papers?\b",
    re.IGNORECASE,
)
_CHINESE_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_ENGLISH_DIGITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _chinese_number(value: str) -> int | None:
    if value in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[value]
    if value.startswith("十") and len(value) == 2:
        return 10 + _CHINESE_DIGITS.get(value[1], 0)
    if value.endswith("十") and len(value) == 2:
        return _CHINESE_DIGITS.get(value[0], 0) * 10
    if "十" in value and len(value) == 3:
        left, right = value.split("十", 1)
        return _CHINESE_DIGITS.get(left, 0) * 10 + _CHINESE_DIGITS.get(right, 0)
    return None


def requested_paper_count(text: str, *, default: int | None = None) -> int | None:
    """解析中英文 1～10 篇表达；超出产品上限时稳定收敛到 10。"""

    arabic = _ARABIC_COUNT_RE.search(text)
    if arabic:
        return min(10, max(1, int(arabic.group(1))))
    chinese = _CHINESE_COUNT_RE.search(text)
    if chinese:
        value = _chinese_number(chinese.group(1))
        if value is not None:
            return min(10, max(1, value))
    english = _ENGLISH_COUNT_RE.search(text)
    if english:
        return _ENGLISH_DIGITS[english.group(1).casefold()]
    return default


@dataclass(frozen=True)
class AcademicSourcePolicy:
    requested_tools: frozenset[str]
    denied_tools: frozenset[str]

    @property
    def has_explicit_source(self) -> bool:
        return bool(self.requested_tools or self.denied_tools)


def academic_source_policy(text: str) -> AcademicSourcePolicy:
    """识别明确指定和明确排除的数据源，否定语义优先。"""

    requested: set[str] = set()
    denied: set[str] = set()
    for tool, aliases in _SOURCE_ALIASES.items():
        for alias in aliases:
            for match in re.finditer(alias, text, re.IGNORECASE):
                prefix = text[max(0, match.start() - 40) : match.start()]
                suffix = text[match.end() : match.end() + 20]
                clause_prefix = re.split(r"[，,。.!！?？;；]", prefix)[-1]
                if (
                    _NEGATION_PREFIX_RE.search(prefix)
                    or _NEGATION_CLAUSE_RE.search(clause_prefix)
                    or _NEGATION_SUFFIX_RE.search(suffix)
                ):
                    denied.add(tool)
                else:
                    requested.add(tool)
    requested.difference_update(denied)
    if requested and _EXCLUSIVE_SOURCE_RE.search(text):
        denied.update(set(_SOURCE_ALIASES) - requested)
    return AcademicSourcePolicy(frozenset(requested), frozenset(denied))
