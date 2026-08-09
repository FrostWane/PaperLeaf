from paperleaf_api.selection_context import canonicalize_pdf_text, match_selection_to_page


def test_normalizes_pdf_ligatures_quotes_and_broken_words() -> None:
    selected = "The ﬁnal drug–target affinity prediction"
    page = "The fi-\nnal drug-target affinity prediction is reported."

    result = match_selection_to_page(selected, page)

    assert result.accepted is True
    assert result.mode == "canonical_exact"


def test_long_selection_allows_small_pdf_text_layer_differences() -> None:
    selected = (
        "Protein sequences are encoded as integer vectors and passed to a convolutional "
        "neural network for representation learning."
    )
    page = (
        "Protein sequences are encoded as integer vector and passed to the convolutional "
        "neural network for representation learning."
    )

    result = match_selection_to_page(selected, page)

    assert result.accepted is True
    assert result.mode == "ordered_fuzzy"
    assert result.score >= 0.88
    assert result.canonical_text in canonicalize_pdf_text(page)


def test_short_selection_must_match_exactly() -> None:
    result = match_selection_to_page("CNN model", "The paper uses a convolution model.")

    assert result.accepted is False
    assert result.mode == "short_not_exact"


def test_selection_from_another_page_is_rejected() -> None:
    selected = "This selection is long enough but belongs to a completely different page."
    page = "The current page describes datasets and experimental settings only."

    result = match_selection_to_page(selected, page)

    assert result.accepted is False
    assert result.mode == "not_on_page"


def test_empty_page_is_safe() -> None:
    result = match_selection_to_page("some selected content", "")

    assert result.accepted is False
    assert result.mode == "empty"


def test_normalization_is_deterministic() -> None:
    value = "Drug–target  afﬁnity\n prediction"

    assert canonicalize_pdf_text(value) == canonicalize_pdf_text(value)
    assert match_selection_to_page(value, value) == match_selection_to_page(value, value)
