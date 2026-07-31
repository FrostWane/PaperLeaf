from paperleaf_api.evaluation_qasper import (
    _unanimous_answerability,
    load_exclusion_manifests,
    match_evidence_to_page,
    normalize_arxiv_id,
    prepare_qasper_case,
    resolve_arxiv_versions,
)


def test_page_match_tolerates_reference_placeholders() -> None:
    pages = [
        "Background material without the requested comparison.",
        "We compare our approach with pivoting, multilingual NMT (Johnson et al., 2016), "
        "and cross-lingual transfer without pretraining.",
    ]
    evidence = (
        "We compare our approach with pivoting, multilingual NMT BIBREF19, "
        "and cross-lingual transfer without pretraining BIBREF16."
    )

    match = match_evidence_to_page(evidence, pages)

    assert match is not None
    assert match.physical_page == 2
    assert match.score >= 0.67


def test_qasper_requires_unanimous_answerability() -> None:
    assert _unanimous_answerability([{"unanswerable": False}]) is True
    assert _unanimous_answerability([{"unanswerable": True}]) is False
    assert (
        _unanimous_answerability(
            [{"unanswerable": False}, {"unanswerable": True}]
        )
        is None
    )


def test_version_batch_uses_cache_without_network(tmp_path) -> None:
    cache = tmp_path / "versions.json"
    cache.write_text('{"1912.01214":"1912.01214v1"}\n', encoding="utf-8")

    versions = resolve_arxiv_versions(["1912.01214"], cache_path=cache)

    assert versions == {"1912.01214": "1912.01214v1"}


def test_exclusion_manifest_normalizes_versions_without_leaking_local_path(
    tmp_path,
) -> None:
    manifest = tmp_path / "private-folder" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        """{
          "dataset_id": "calibration-v1",
          "version": "1",
          "created_at": "2026-07-31",
          "annotation_license": "CC-BY-4.0",
          "paper_count": 1,
          "case_count": 1,
          "answerable_count": 1,
          "unanswerable_count": 0,
          "category_counts": {"extractive": 1},
          "papers": [{
            "id": "arxiv:1912.01214v3",
            "title": "Fixture",
            "arxiv_id": "1912.01214v3",
            "source_url": "https://arxiv.org/abs/1912.01214v3",
            "pdf_url": "https://arxiv.org/pdf/1912.01214v3",
            "filename": "1912.01214v3.pdf",
            "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "page_count": 1
          }]
        }\n""",
        encoding="utf-8",
    )

    excluded, sources = load_exclusion_manifests([manifest])

    assert normalize_arxiv_id("arxiv:1912.01214v3") == "1912.01214"
    assert normalize_arxiv_id("https://arxiv.org/abs/1912.01214v3") == "1912.01214"
    assert excluded == {"1912.01214"}
    assert sources[0]["dataset_id"] == "calibration-v1"
    assert "private-folder" not in str(sources)


def test_prepare_qasper_case_builds_alternative_page_groups() -> None:
    row = {
        "id": "1912.01214",
        "title": "Paper",
        "qas": {
            "question": ["Which approaches are compared?"],
            "question_id": ["source-question"],
            "answers": [
                {
                    "answer": [
                        {
                            "unanswerable": False,
                            "extractive_spans": ["pivoting", "multilingual NMT"],
                            "yes_no": None,
                            "free_form_answer": "",
                            "evidence": [],
                            "highlighted_evidence": [
                                "We compare with pivoting and multilingual NMT BIBREF1."
                            ],
                        },
                        {
                            "unanswerable": False,
                            "extractive_spans": ["cross-lingual transfer"],
                            "yes_no": None,
                            "free_form_answer": "",
                            "evidence": [],
                            "highlighted_evidence": [
                                "A separate comparison uses cross-lingual transfer "
                                "without pretraining."
                            ],
                        },
                    ]
                }
            ],
        },
    }
    pages = [
        "We compare with pivoting and multilingual NMT (Johnson et al.).",
        "A separate comparison uses cross-lingual transfer without pretraining.",
    ]

    prepared = prepare_qasper_case(
        row=row,
        question_index=0,
        versioned_id="1912.01214v1",
        pages=pages,
        source_split="validation",
    )

    assert prepared is not None
    assert prepared.oracle.answerable is True
    assert len(prepared.oracle.acceptable_evidence_groups) == 2
    assert prepared.oracle.acceptable_evidence_groups[0].items[0].physical_page == 1
    assert prepared.oracle.acceptable_evidence_groups[1].items[0].physical_page == 2
