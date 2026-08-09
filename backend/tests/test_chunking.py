from paperleaf_api.rag.chunking import (
    PageText,
    chunk_pages,
    chunk_pages_fixed_window,
    sanitize_pdf_text,
)


def test_chunk_never_crosses_physical_page() -> None:
    pages = [
        PageText(paper_id="p1", physical_page=1, text="第一页内容 " * 40),
        PageText(paper_id="p1", physical_page=2, text="第二页内容 " * 40),
    ]

    chunks = chunk_pages(pages, target_tokens=20, overlap_tokens=5)

    assert {chunk.physical_page for chunk in chunks} == {1, 2}
    assert all("第二页" not in chunk.text for chunk in chunks if chunk.physical_page == 1)
    assert all("第一页" not in chunk.text for chunk in chunks if chunk.physical_page == 2)
    assert all(chunk.id.startswith(f"p1:p{chunk.physical_page}:") for chunk in chunks)


def test_chunk_parameters_are_validated() -> None:
    pages = [PageText(paper_id="p1", physical_page=1, text="内容")]

    try:
        chunk_pages(pages, target_tokens=10, overlap_tokens=10)
    except ValueError as exc:
        assert "overlap_tokens" in str(exc)
    else:
        raise AssertionError("重叠达到窗口大小时应拒绝")


def test_structure_aware_chunking_preserves_paragraphs_and_headings() -> None:
    page = PageText(
        paper_id="p1",
        physical_page=1,
        text=(
            "1 Introduction\n\n"
            "The first paragraph explains the research problem and motivation.\n\n"
            "2 Method\n\n"
            "The second paragraph describes the model architecture and training objective."
        ),
    )

    chunks = chunk_pages([page], target_tokens=30, overlap_tokens=0, max_unit_tokens=20)

    assert len(chunks) >= 2
    assert "1 Introduction\n\nThe first paragraph" in chunks[0].text
    assert any(chunk.text.startswith("2 Method") for chunk in chunks)
    assert all("\n\n" in chunk.text for chunk in chunks)


def test_chunk_size_has_a_hard_limit_for_long_paragraphs() -> None:
    page = PageText(paper_id="p1", physical_page=1, text="word " * 180)
    chunks = chunk_pages([page], target_tokens=40, overlap_tokens=5, max_unit_tokens=20)

    assert len(chunks) >= 5
    assert all(chunk.token_count <= 40 for chunk in chunks)


def test_long_paragraph_overlap_prefers_complete_sentences() -> None:
    sentences = [f"Sentence {index} explains a complete result." for index in range(12)]
    page = PageText(paper_id="p1", physical_page=1, text=" ".join(sentences))

    chunks = chunk_pages([page], target_tokens=30, overlap_tokens=8, max_unit_tokens=18)

    assert len(chunks) > 2
    assert all(chunk.text.rstrip().endswith(".") for chunk in chunks)
    assert all(chunk.token_count <= 30 for chunk in chunks)


def test_chinese_formula_table_and_empty_page_are_safe() -> None:
    pages = [
        PageText(
            paper_id="p1",
            physical_page=1,
            text=(
                "2 方法\n\n本文提出一个混合检索框架。它同时使用关键词与向量召回。\n\n"
                "L = L_task + λL_reg (1)\n\n"
                "模型 | 准确率 | 召回率\nPaperLeaf | 0.91 | 0.88"
            ),
        ),
        PageText(paper_id="p1", physical_page=2, text="   \n\n"),
    ]

    chunks = chunk_pages(pages, target_tokens=80, overlap_tokens=10, max_unit_tokens=30)

    assert chunks
    assert {chunk.physical_page for chunk in chunks} == {1}
    rendered = "\n".join(chunk.text for chunk in chunks)
    assert "L = L_task + λL_reg (1)" in rendered
    assert "模型 | 准确率 | 召回率\nPaperLeaf | 0.91 | 0.88" in rendered


def test_chunking_is_deterministic_and_ids_are_stable() -> None:
    pages = [PageText("paper", 3, "3 Results\n\nFirst result. Second result.\n\nThird result.")]

    first = chunk_pages(pages, target_tokens=16, overlap_tokens=4, max_unit_tokens=10)
    second = chunk_pages(pages, target_tokens=16, overlap_tokens=4, max_unit_tokens=10)

    assert first == second
    assert [chunk.id for chunk in first] == [f"paper:p3:c{index}" for index in range(len(first))]


def test_pdf_text_sanitizer_removes_nul_but_preserves_structure() -> None:
    raw = "2 Method\x00\n\nA\tB\x0cC\x01D"

    sanitized = sanitize_pdf_text(raw)
    chunks = chunk_pages(
        [PageText("p1", 1, raw)], target_tokens=40, overlap_tokens=5
    )

    assert sanitized == "2 Method\n\nA\tB\nCD"
    assert chunks
    assert all("\x00" not in chunk.text and "\x01" not in chunk.text for chunk in chunks)


def test_fixed_window_fallback_remains_page_safe() -> None:
    pages = [PageText("p1", 1, "alpha beta gamma " * 10), PageText("p1", 2, "delta " * 10)]

    chunks = chunk_pages_fixed_window(pages, target_tokens=12, overlap_tokens=3)

    assert all(chunk.token_count <= 12 for chunk in chunks)
    assert all("delta" not in chunk.text for chunk in chunks if chunk.physical_page == 1)
