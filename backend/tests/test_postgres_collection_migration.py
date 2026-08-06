"""可选的 PostgreSQL 0005 迁移结构验证。"""

import asyncio
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

TEST_DATABASE_URL = os.getenv("PAPERLEAF_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要设置 PAPERLEAF_TEST_DATABASE_URL 并先执行 Alembic upgrade head",
)


def test_postgres_0005_schema_contract() -> None:
    async def scenario() -> dict:
        assert TEST_DATABASE_URL
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(_inspect_schema)
        finally:
            await engine.dispose()

    def _inspect_schema(connection) -> dict:
        inspector = sa.inspect(connection)
        return {
            "tables": set(inspector.get_table_names()),
            "paper_columns": {item["name"] for item in inspector.get_columns("papers")},
            "collection_columns": {
                item["name"] for item in inspector.get_columns("collections")
            },
            "unique_names": {
                item["name"] for item in inspector.get_unique_constraints("collections")
            },
            "foreign_key_names": {
                item["name"] for item in inspector.get_foreign_keys("collections")
            },
        }

    schema = asyncio.run(scenario())
    assert "tags" not in schema["tables"]
    assert "paper_tags" not in schema["tables"]
    assert "publication" in schema["paper_columns"]
    assert "parent_id" in schema["collection_columns"]
    assert "uq_collection_owner_parent_name" in schema["unique_names"]
    assert "fk_collections_parent_id_collections" in schema["foreign_key_names"]
