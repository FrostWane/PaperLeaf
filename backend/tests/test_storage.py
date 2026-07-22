import asyncio

from paperleaf_api.storage import LocalObjectStorage, parse_byte_range, validate_pdf


def test_range_supports_regular_and_suffix_ranges() -> None:
    assert parse_byte_range("bytes=10-19", 100).content_range == "bytes 10-19/100"
    assert parse_byte_range("bytes=-10", 100).start == 90


def test_pdf_header_validation_rejects_renamed_file() -> None:
    try:
        validate_pdf(b"not really a pdf", "paper.pdf", 1024)
    except ValueError as exc:
        assert "文件头" in str(exc)
    else:
        raise AssertionError("伪 PDF 应被拒绝")


def test_local_delete_is_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        storage = LocalObjectStorage(tmp_path)
        await storage.put("user/paper.pdf", b"pdf", "application/pdf")
        await storage.delete("user/paper.pdf")
        await storage.delete("user/paper.pdf")
        assert not (tmp_path / "user" / "paper.pdf").exists()

    asyncio.run(scenario())
