from sqlalchemy import create_engine

from paperleaf_api.models import Base, DiscoveryBatch, DiscoveryItem, PaperArtifact, PaperChunk


def test_postgres_retrieval_indexes_are_part_of_model_metadata() -> None:
    indexes = {index.name: index for index in PaperChunk.__table__.indexes}

    assert indexes["ix_paper_chunks_fts"].dialect_options["postgresql"]["using"] == "gin"
    assert indexes["ix_paper_chunks_trgm"].dialect_options["postgresql"]["using"] == "gin"
    assert indexes["ix_paper_chunks_trgm"].dialect_options["postgresql"]["ops"] == {
        "text": "gin_trgm_ops"
    }


def test_paper_artifact_schema_and_named_cycles_create_and_drop() -> None:
    assert PaperArtifact.__table__.c.source_revision.type.length == 64
    assert {constraint.name for constraint in PaperArtifact.__table__.constraints} >= {
        "uq_paper_artifact_type"
    }
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Base.metadata.drop_all(engine)


def test_discovery_schema_keeps_batches_feedback_and_unique_items() -> None:
    assert {constraint.name for constraint in DiscoveryBatch.__table__.constraints} >= {
        "uq_discovery_user_batch"
    }
    assert {constraint.name for constraint in DiscoveryItem.__table__.constraints} >= {
        "uq_discovery_batch_arxiv"
    }
    assert DiscoveryItem.__table__.c.feedback.nullable is True
    assert DiscoveryItem.__table__.c.opened_at.nullable is True
    assert DiscoveryItem.__table__.c.imported_at.nullable is True
