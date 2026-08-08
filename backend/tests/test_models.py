from sqlalchemy import create_engine

from paperleaf_api.models import (
    AgentRun,
    AgentToolArtifact,
    AgentToolCall,
    Base,
    ChatSession,
    DiscoveryBatch,
    DiscoveryItem,
    MemoryItem,
    MemoryItemVersion,
    PaperArtifact,
    PaperChunk,
)


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


def test_agent_run_schema_contains_context_snapshot_fields() -> None:
    assert {
        "context_snapshot",
        "context_version",
        "resolved_query",
        "reference_confidence",
        "selected_skill",
        "skill_version",
        "harness_trace",
    } <= {
        column.name for column in AgentRun.__table__.columns
    }


def test_context_summary_and_versioned_memory_are_persistent_models() -> None:
    assert {
        "compact_summary",
        "summary_version",
        "compacted_through_message_id",
        "entity_state",
    } <= {column.name for column in ChatSession.__table__.columns}
    assert {constraint.name for constraint in MemoryItem.__table__.constraints} >= {
        "uq_memory_user_hash"
    }
    assert {constraint.name for constraint in MemoryItemVersion.__table__.constraints} >= {
        "uq_memory_item_version"
    }


def test_function_tool_calls_and_large_artifacts_are_persistent_models() -> None:
    assert {constraint.name for constraint in AgentToolCall.__table__.constraints} >= {
        "uq_agent_tool_call_run_call"
    }
    assert AgentToolCall.__table__.c.arguments.nullable is False
    assert AgentToolArtifact.__table__.c.tool_call_id.nullable is False
