from paperleaf_api.evaluation_formal_protocol import FORMAL_VARIANTS
from paperleaf_api.evaluation_formal_run import VARIANTS, _variant_settings


def test_formal_variants_are_exactly_preregistered() -> None:
    assert tuple(VARIANTS) == FORMAL_VARIANTS
    assert VARIANTS["production_baseline"].reranker is False
    assert VARIANTS["plain_embedding_control"].embedding_input_format == "chunk_text_v1"
    assert VARIANTS["multigranular_page_reranker"].reranker is True
    assert VARIANTS["final_combined"].retrieval_mode == "per_paper_specific"


def test_variant_settings_never_enable_legacy_minilm() -> None:
    for spec in VARIANTS.values():
        config = _variant_settings(spec)
        assert config.rag_reranker_strategy == "multigranular_v1"
        assert config.rag_reranker_enabled is spec.reranker
