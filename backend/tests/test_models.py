from paperleaf_api.models import PaperChunk


def test_postgres_retrieval_indexes_are_part_of_model_metadata() -> None:
    indexes = {index.name: index for index in PaperChunk.__table__.indexes}

    assert indexes["ix_paper_chunks_fts"].dialect_options["postgresql"]["using"] == "gin"
    assert indexes["ix_paper_chunks_trgm"].dialect_options["postgresql"]["using"] == "gin"
    assert indexes["ix_paper_chunks_trgm"].dialect_options["postgresql"]["ops"] == {
        "text": "gin_trgm_ops"
    }
