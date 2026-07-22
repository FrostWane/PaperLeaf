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

