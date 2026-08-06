import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from paperleaf_api import worker
from paperleaf_api.db import Base
from paperleaf_api.models import Job, JobStatus, Paper, PaperArtifact, PaperStatus, User
from paperleaf_api.pdf_metadata import (
    PdfMetadata,
    backfill_pdf_metadata,
    extract_first_page_authors,
    extract_first_page_doi,
    extract_first_page_publication,
    extract_first_page_year,
    extract_pdf_metadata,
    extract_pdf_publication,
    normalize_doi,
)
from paperleaf_api.storage import LocalObjectStorage


def test_extract_pdf_metadata_cleans_and_splits_supported_fields() -> None:
    metadata = extract_pdf_metadata(
        {
            "title": "  A Reliable\nPaper  ",
            "author": "Ada Lovelace; Alan Turing and Grace Hopper",
            "creationDate": "D:20240309121500+08'00'",
        }
    )

    assert metadata == PdfMetadata(
        title="A Reliable Paper",
        authors=("Ada Lovelace", "Alan Turing", "Grace Hopper"),
        year=None,
    )


def test_pdf_creation_date_is_not_treated_as_publication_year() -> None:
    metadata = extract_pdf_metadata({"creationDate": "D:20250309121500Z"})

    assert metadata.year is None


def test_extracts_embedded_publication_and_normalizes_doi() -> None:
    metadata = extract_pdf_metadata(
        {
            "journal": " Journal of Machine Learning Research, Vol. 25 ",
            "identifier": "https://doi.org/10.5555/ABC.123.",
        }
    )

    assert metadata.publication == "Journal of Machine Learning Research"
    assert metadata.doi == "10.5555/abc.123"


def test_first_page_extracts_journal_header_and_explicit_doi() -> None:
    text = """Bioinformatics, 34, 2018, i821–i829
doi: 10.1093/bioinformatics/bty593.
DeepDTA: deep drug–target binding affinity prediction
"""

    assert extract_first_page_publication(text) == "Bioinformatics"
    assert extract_first_page_doi(text) == "10.1093/bioinformatics/bty593"


def test_publication_extraction_accepts_proceedings_but_rejects_false_candidates() -> None:
    assert (
        extract_first_page_publication(
            "Published in: Proceedings of the 41st International Conference on Machine Learning"
        )
        == "Proceedings of the 41st International Conference on Machine Learning"
    )
    assert extract_pdf_publication({"subject": "Drug-target interaction prediction"}) is None
    assert extract_first_page_publication("arXiv:2506.06962v3 [cs.CV] 14 Jun 2025") is None
    affiliation = "Department of Computer Science, Example University"
    assert extract_first_page_publication(affiliation) is None
    assert extract_first_page_publication("Local file: document.pdf") is None
    abstract_citation = "Abstract\nCompared with Proceedings of Fake Venue, our method..."
    assert extract_first_page_publication(abstract_citation) is None


def test_doi_normalization_rejects_non_doi_and_first_page_ambiguous_numbers() -> None:
    assert normalize_doi("doi: 10.1000/example(2024)") == "10.1000/example(2024)"
    assert normalize_doi("https://example.org/10.1000/not-allowed") is None
    assert normalize_doi("11.1000/not-a-doi") is None
    ambiguous = "The baseline cites 10.1000/reference without a DOI label."
    assert extract_first_page_doi(ambiguous) is None
    assert extract_first_page_doi("Abstract\nPrior work doi: 10.1000/cited") is None


def test_worker_enrichment_prefers_local_publication_and_uses_crossref_as_fallback() -> None:
    class FakeCrossref:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def lookup_publication(self, doi: str) -> str:
            self.calls.append(doi)
            return "Journal of Crossref Results"

    async def scenario() -> None:
        client = FakeCrossref()
        local = PdfMetadata(
            publication="Bioinformatics",
            doi="10.1093/bioinformatics/bty593",
        )
        assert (
            await worker.lookup_crossref_publication(
                local,
                latest_doi=None,
                latest_publication=None,
                client=client,
            )
            is None
        )
        assert client.calls == []

        missing = PdfMetadata(doi="10.1000/crossref-fallback")
        enrichment = await worker.lookup_crossref_publication(
            missing,
            latest_doi=None,
            latest_publication=None,
            client=client,
        )
        assert enrichment is not None
        assert enrichment.publication == "Journal of Crossref Results"
        assert enrichment.queried_doi == "10.1000/crossref-fallback"
        assert client.calls == ["10.1000/crossref-fallback"]

        # 用户已填入非标准值时也不回退查询 PDF 里的旧 DOI，避免错配和额外外发。
        assert (
            await worker.lookup_crossref_publication(
                missing,
                latest_doi="用户待核对",
                latest_publication=None,
                client=client,
            )
            is None
        )
        assert client.calls == ["10.1000/crossref-fallback"]

    asyncio.run(scenario())


def test_worker_crossref_failure_never_fails_pdf_enrichment() -> None:
    class FailingCrossref:
        async def lookup_publication(self, _doi: str) -> None:
            raise TimeoutError("Crossref timeout")

    metadata = PdfMetadata(doi="10.1000/timeout")
    enrichment = asyncio.run(
        worker.lookup_crossref_publication(
            metadata,
            latest_doi=None,
            latest_publication=None,
            client=FailingCrossref(),
        )
    )
    assert enrichment is None


def test_crossref_result_is_not_applied_after_user_changes_doi_or_publication() -> None:
    enrichment = worker.CrossrefPublicationEnrichment(
        queried_doi="10.1000/original",
        publication="Original DOI Journal",
    )
    changed_doi = SimpleNamespace(doi="10.1000/user-edited", publication=None)
    assert worker.apply_crossref_publication(changed_doi, enrichment) is False
    assert changed_doi.publication is None

    filled_publication = SimpleNamespace(
        doi="10.1000/original",
        publication="用户填写的出版物",
    )
    assert worker.apply_crossref_publication(filled_publication, enrichment) is False
    assert filled_publication.publication == "用户填写的出版物"

    unchanged = SimpleNamespace(doi="10.1000/original", publication=None)
    assert worker.apply_crossref_publication(unchanged, enrichment) is True
    assert unchanged.publication == "Original DOI Journal"


def test_extract_pdf_metadata_keeps_ambiguous_surname_comma_as_one_author() -> None:
    metadata = extract_pdf_metadata({"author": "Lovelace, Ada"})

    assert metadata.authors == ("Lovelace, Ada",)


def test_extract_pdf_metadata_rejects_publisher_production_code_as_title() -> None:
    metadata = extract_pdf_metadata({"title": "OP-CBIO180619 821..829"})

    assert metadata.title is None


def test_backfill_only_fills_empty_fields_and_generated_titles() -> None:
    paper = SimpleNamespace(
        title="paper",
        authors=[],
        year=None,
        filename="paper.pdf",
        arxiv_id=None,
    )

    changed = backfill_pdf_metadata(
        paper,
        PdfMetadata("Metadata Title", ("Ada Lovelace",), 2024),
    )

    assert changed is True
    assert (paper.title, paper.authors, paper.year) == (
        "Metadata Title",
        ["Ada Lovelace"],
        2024,
    )


def test_backfill_never_overwrites_user_metadata() -> None:
    paper = SimpleNamespace(
        title="用户编辑的标题",
        authors=["用户编辑的作者"],
        year=2025,
        filename="paper.pdf",
        arxiv_id=None,
    )

    changed = backfill_pdf_metadata(
        paper,
        PdfMetadata("PDF 标题", ("PDF Author",), 2024),
    )

    assert changed is False
    assert (paper.title, paper.authors, paper.year) == (
        "用户编辑的标题",
        ["用户编辑的作者"],
        2025,
    )


def test_backfill_publication_and_doi_only_when_missing() -> None:
    paper = SimpleNamespace(
        title="用户标题",
        authors=[],
        year=None,
        publication=None,
        doi=None,
        filename="paper.pdf",
        arxiv_id=None,
    )

    changed = backfill_pdf_metadata(
        paper,
        PdfMetadata(
            publication="Bioinformatics",
            doi="10.1093/bioinformatics/bty593",
        ),
    )

    assert changed is True
    assert paper.publication == "Bioinformatics"
    assert paper.doi == "10.1093/bioinformatics/bty593"

    changed_again = backfill_pdf_metadata(
        paper,
        PdfMetadata(publication="Other Journal", doi="10.1000/other"),
    )
    assert changed_again is False
    assert paper.publication == "Bioinformatics"
    assert paper.doi == "10.1093/bioinformatics/bty593"


def test_backfill_replaces_arxiv_import_placeholder_title() -> None:
    paper = SimpleNamespace(
        title="arXiv 2401.01234",
        authors=[],
        year=None,
        filename="2401.01234.pdf",
        arxiv_id="2401.01234",
    )

    changed = backfill_pdf_metadata(paper, PdfMetadata(title="Published Paper Title"))

    assert changed is True
    assert paper.title == "Published Paper Title"


def test_missing_pdf_metadata_leaves_fields_empty() -> None:
    paper = SimpleNamespace(
        title="paper",
        authors=[],
        year=None,
        filename="paper.pdf",
        arxiv_id=None,
    )

    changed = backfill_pdf_metadata(paper, extract_pdf_metadata({}))

    assert changed is False
    assert paper.authors == []
    assert paper.year is None


def test_first_page_fallback_extracts_wrapped_attentiondta_authors() -> None:
    text = """AttentionDTA: Drug–Target Binding Affinity
Prediction by Sequence-Based Deep Learning
With Attention Mechanism
Qichang Zhao, Guihua Duan
, Mengyun Yang
, Zhongjian Cheng, Yaohang Li
, and Jianxin Wang
Abstract—The identification of drug–target relations is substantial.
"""

    assert extract_first_page_authors(text, "AttentionDTA") == (
        "Qichang Zhao",
        "Guihua Duan",
        "Mengyun Yang",
        "Zhongjian Cheng",
        "Yaohang Li",
        "Jianxin Wang",
    )


def test_first_page_fallback_strips_author_footnote_marks() -> None:
    text = """DeepDTA: deep drug–target binding
affinity prediction
Hakime O¨ ztu¨ rk1, Arzucan O¨ zgu¨ r1,* and Elif Ozkirimli2,*
1Department of Computer Engineering, Bogazici University
Abstract
"""

    assert extract_first_page_authors(text, "DeepDTA") == (
        "Hakime Öztürk",
        "Arzucan Özgür",
        "Elif Ozkirimli",
    )


def test_first_page_fallback_refuses_ambiguous_single_author() -> None:
    text = "A Study of Retrieval\nAlex Smith\nAbstract\n"

    assert extract_first_page_authors(text, "A Study of Retrieval") == ()


def test_first_page_year_prefers_explicit_publication_context() -> None:
    text = (
        "Date of publication 26 April 2022; date of current version 3 April 2023.\n"
        "Downloaded on October 24, 2025."
    )

    assert extract_first_page_year(text) == 2022


def test_first_page_year_reads_arxiv_date_but_ignores_download_date() -> None:
    assert extract_first_page_year("arXiv:2506.06962v3 [cs.CV] 14 Jun 2025") == 2025
    assert extract_first_page_year("Downloaded on October 24, 2025") is None


def test_worker_persists_embedded_pdf_authors_and_year(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        import fitz

        document = fitz.open()
        page = document.new_page()
        page.insert_text(
            (72, 72),
            (
                "Bioinformatics, 34, 2018, i821-i829\n"
                "doi: 10.1093/bioinformatics/bty593\n"
                "Published 2024. PaperLeaf metadata integration test contains enough text."
            ),
        )
        document.set_metadata(
            {
                "title": "Metadata Integration Paper",
                "author": "Ada Lovelace; Alan Turing",
                "creationDate": "D:20240309121500Z",
            }
        )
        content = document.tobytes()
        document.close()

        database_path = tmp_path / "metadata.sqlite3"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        storage = LocalObjectStorage(tmp_path / "uploads")
        storage_key = "user-1/paper-1/document.pdf"
        await storage.put(storage_key, content, "application/pdf")

        async with sessions() as session:
            session.add(
                User(
                    id="user-1",
                    email="metadata@example.com",
                    password_hash="not-used-in-this-test",
                )
            )
            session.add(
                Paper(
                    id="paper-1",
                    owner_id="user-1",
                    title="document",
                    authors=[],
                    year=None,
                    abstract=None,
                    doi=None,
                    arxiv_id=None,
                    filename="document.pdf",
                    storage_key=storage_key,
                    mime_type="application/pdf",
                    size_bytes=len(content),
                    sha256="a" * 64,
                    page_count=None,
                    status=PaperStatus.queued,
                )
            )
            session.add(
                Job(
                    id="job-1",
                    paper_id="paper-1",
                    type="parse_pdf",
                    status=JobStatus.running,
                )
            )
            session.add(
                PaperArtifact(
                    id="artifact-before-reindex",
                    paper_id="paper-1",
                    owner_id="user-1",
                    type="summary",
                    source_revision="f" * 64,
                    status="ready",
                    fallback_reason=None,
                    structured_payload={"sections": []},
                    markdown="old summary",
                )
            )
            await session.commit()

        async def embeddings_unavailable(texts: list[str], router: object | None = None) -> None:
            return None

        monkeypatch.setattr(worker, "get_session_factory", lambda: sessions)
        monkeypatch.setattr(worker, "create_storage", lambda _settings: storage)
        monkeypatch.setattr(worker, "embed_texts", embeddings_unavailable)

        try:
            await worker.process_parse_job("job-1")
            async with sessions() as session:
                paper = await session.get(Paper, "paper-1")
                job = await session.get(Job, "job-1")
                artifact = await session.get(PaperArtifact, "artifact-before-reindex")
                assert paper is not None
                assert job is not None
                assert paper.title == "Metadata Integration Paper"
                assert paper.authors == ["Ada Lovelace", "Alan Turing"]
                assert paper.year == 2024
                assert paper.publication == "Bioinformatics"
                assert paper.doi == "10.1093/bioinformatics/bty593"
                assert paper.status == PaperStatus.ready
                assert job.status == JobStatus.completed
                assert artifact is not None and artifact.status == "stale"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
