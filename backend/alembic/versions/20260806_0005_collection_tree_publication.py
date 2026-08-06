"""层级集合、出版物字段并移除标签。

Revision ID: 20260806_0005
Revises: 20260806_0004

升级会永久删除既有标签及论文标签关系。降级只重建空表，无法恢复标签数据。
"""

import sqlalchemy as sa

from alembic import op

revision = "20260806_0005"
down_revision = "20260806_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "paper_tags" in tables:
        op.drop_table("paper_tags")
    if "tags" in tables:
        op.drop_table("tags")

    paper_columns = {item["name"] for item in sa.inspect(bind).get_columns("papers")}
    if "publication" not in paper_columns:
        op.add_column(
            "papers",
            sa.Column("publication", sa.String(length=1000), nullable=True),
        )

    collection_columns = {
        item["name"] for item in sa.inspect(bind).get_columns("collections")
    }
    if "parent_id" not in collection_columns:
        op.add_column(
            "collections",
            sa.Column("parent_id", sa.String(length=36), nullable=True),
        )

    unique_names = {
        item["name"] for item in sa.inspect(bind).get_unique_constraints("collections")
    }
    if "uq_collection_owner_name" in unique_names:
        op.drop_constraint("uq_collection_owner_name", "collections", type_="unique")

    foreign_key_names = {
        item["name"] for item in sa.inspect(bind).get_foreign_keys("collections")
    }
    if "fk_collections_parent_id_collections" not in foreign_key_names:
        op.create_foreign_key(
            "fk_collections_parent_id_collections",
            "collections",
            "collections",
            ["parent_id"],
            ["id"],
            ondelete="SET NULL",
        )

    index_names = {item["name"] for item in sa.inspect(bind).get_indexes("collections")}
    if "ix_collections_parent_id" not in index_names:
        op.create_index("ix_collections_parent_id", "collections", ["parent_id"])

    unique_names = {
        item["name"] for item in sa.inspect(bind).get_unique_constraints("collections")
    }
    if "uq_collection_owner_parent_name" not in unique_names:
        op.create_unique_constraint(
            "uq_collection_owner_parent_name",
            "collections",
            ["owner_id", "parent_id", "name"],
            postgresql_nulls_not_distinct=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    unique_names = {
        item["name"] for item in sa.inspect(bind).get_unique_constraints("collections")
    }
    if "uq_collection_owner_parent_name" in unique_names:
        op.drop_constraint(
            "uq_collection_owner_parent_name",
            "collections",
            type_="unique",
        )

    collection_columns = {
        item["name"] for item in sa.inspect(bind).get_columns("collections")
    }
    if "parent_id" in collection_columns:
        # 旧版要求同一用户全局同名唯一。层级版允许不同父节点下同名，因此降级前
        # 为重复名称追加稳定 ID，避免恢复旧约束时迁移失败。
        op.execute(
            "WITH ranked AS ("
            " SELECT id, row_number() OVER (PARTITION BY owner_id, name ORDER BY id) AS position"
            " FROM collections"
            ") UPDATE collections AS target"
            " SET name = left(target.name, 157) || ' (' || target.id || ')'"
            " FROM ranked WHERE ranked.id = target.id AND ranked.position > 1"
        )
        op.execute("UPDATE collections SET parent_id = NULL")
        index_names = {item["name"] for item in sa.inspect(bind).get_indexes("collections")}
        if "ix_collections_parent_id" in index_names:
            op.drop_index("ix_collections_parent_id", table_name="collections")
        foreign_key_names = {
            item["name"] for item in sa.inspect(bind).get_foreign_keys("collections")
        }
        if "fk_collections_parent_id_collections" in foreign_key_names:
            op.drop_constraint(
                "fk_collections_parent_id_collections",
                "collections",
                type_="foreignkey",
            )
        op.drop_column("collections", "parent_id")

    unique_names = {
        item["name"] for item in sa.inspect(bind).get_unique_constraints("collections")
    }
    if "uq_collection_owner_name" not in unique_names:
        op.create_unique_constraint(
            "uq_collection_owner_name",
            "collections",
            ["owner_id", "name"],
        )

    paper_columns = {item["name"] for item in sa.inspect(bind).get_columns("papers")}
    if "publication" in paper_columns:
        op.drop_column("papers", "publication")

    tables = set(sa.inspect(bind).get_table_names())
    if "tags" not in tables:
        op.create_table(
            "tags",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("owner_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("color", sa.String(length=20), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("owner_id", "name", name="uq_tag_owner_name"),
        )
        op.create_index("ix_tags_owner_id", "tags", ["owner_id"])

    tables = set(sa.inspect(bind).get_table_names())
    if "paper_tags" not in tables:
        op.create_table(
            "paper_tags",
            sa.Column("paper_id", sa.String(length=36), nullable=False),
            sa.Column("tag_id", sa.String(length=36), nullable=False),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("paper_id", "tag_id"),
        )
