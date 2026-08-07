from paperleaf_api.agent_execution import _citation_dicts
from paperleaf_api.rag.citations import CitationClaim, Evidence, validate_citations


def test_illegal_citation_is_rejected() -> None:
    evidence = [Evidence("chunk-1", "paper-1", "论文", 7, "证据原文")]
    claims = [CitationClaim("chunk-forged", "paper-1", 7, "证据原文")]

    valid, errors = validate_citations(claims, evidence)

    assert valid is False
    assert "不在本次召回证据" in errors[0]


def test_mismatched_page_and_excerpt_are_rejected() -> None:
    evidence = [Evidence("chunk-1", "paper-1", "论文", 7, "证据原文")]
    claims = [CitationClaim("chunk-1", "paper-1", 8, "伪造片段")]

    valid, errors = validate_citations(claims, evidence)

    assert valid is False
    assert len(errors) == 2


def test_citation_viewer_url_keeps_the_physical_page() -> None:
    evidence = [Evidence("paper-1:p7:c0", "paper-1", "论文", 7, "第七页证据")]
    claims = [CitationClaim("paper-1:p7:c0", "paper-1", 7)]

    citations = _citation_dicts(claims, evidence)

    assert citations[0]["physical_page"] == 7
    assert citations[0]["viewer_url"] == "/api/v1/papers/paper-1/file#page=7"
