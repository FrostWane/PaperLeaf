"""回答引用的服务端验证。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Evidence:
    chunk_id: str
    paper_id: str
    paper_title: str
    physical_page: int
    text: str


@dataclass(frozen=True)
class CitationClaim:
    chunk_id: str
    paper_id: str
    physical_page: int
    excerpt: str = ""


def validate_citations(
    claims: list[CitationClaim], evidence: list[Evidence], *, require_citation: bool = True
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    by_chunk = {item.chunk_id: item for item in evidence}
    if require_citation and evidence and not claims:
        errors.append("回答缺少引用")
    for claim in claims:
        source = by_chunk.get(claim.chunk_id)
        if not source:
            errors.append(f"引用 {claim.chunk_id} 不在本次召回证据中")
            continue
        if source.paper_id != claim.paper_id or source.physical_page != claim.physical_page:
            errors.append(f"引用 {claim.chunk_id} 的论文或页码与证据不一致")
        if claim.excerpt and claim.excerpt not in source.text:
            errors.append(f"引用 {claim.chunk_id} 的片段不属于证据原文")
    return not errors, errors
