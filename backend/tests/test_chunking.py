from paperleaf_api.rag.chunking import PageText, chunk_pages


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
