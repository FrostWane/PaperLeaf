"""持久化逐页全文翻译。

Revision ID: 20260806_0006
Revises: 20260806_0005
"""

import sqlalchemy as sa

from alembic import op

revision = "20260806_0006"
down_revision = "20260806_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "paper_translations" not in tables:
        op.create_table(
            "paper_translations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("paper_id", sa.String(length=36), nullable=False),
            sa.Column("owner_id", sa.String(length=36), nullable=False),
            sa.Column("target_language", sa.String(length=32), nullable=False),
            sa.Column("source_revision", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("total_pages", sa.Integer(), nullable=False),
            sa.Column("completed_pages", sa.Integer(), nullable=False),
            sa.Column("failed_pages", sa.Integer(), nullable=False),
            sa.Column("priority_page", sa.Integer(), nullable=True),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "paper_id",
                "target_language",
                name="uq_paper_translation_language",
            ),
        )
        op.create_index("ix_paper_translations_owner_id", "paper_translations", ["owner_id"])
        op.create_index("ix_paper_translations_paper_id", "paper_translations", ["paper_id"])
        op.create_index("ix_paper_translations_status", "paper_translations", ["status"])

    tables = set(sa.inspect(bind).get_table_names())
    if "paper_translation_pages" not in tables:
        op.create_table(
            "paper_translation_pages",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("translation_id", sa.String(length=36), nullable=False),
            sa.Column("physical_page", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("translated_text", sa.Text(), nullable=True),
            sa.Column("source_text_hash", sa.String(length=64), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("max_attempts", sa.Integer(), nullable=False),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["translation_id"], ["paper_translations.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "translation_id",
                "physical_page",
                name="uq_translation_physical_page",
            ),
        )
        op.create_index(
            "ix_paper_translation_pages_translation_id",
            "paper_translation_pages",
            ["translation_id"],
        )
        op.create_index(
            "ix_translation_pages_work",
            "paper_translation_pages",
            ["translation_id", "status", "priority"],
        )

    job_columns = {item["name"] for item in sa.inspect(bind).get_columns("jobs")}
    if "claimed_at" not in job_columns:
        op.add_column("jobs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    if "claim_token" not in job_columns:
        op.add_column("jobs", sa.Column("claim_token", sa.String(length=36), nullable=True))
    if "translation_id" not in job_columns:
        op.add_column(
            "jobs", sa.Column("translation_id", sa.String(length=36), nullable=True)
        )
        op.create_foreign_key(
            "fk_jobs_translation_id_paper_translations",
            "jobs",
            "paper_translations",
            ["translation_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            "uq_jobs_translation_id", "jobs", ["translation_id"], unique=True
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "jobs" in tables:
        job_columns = {item["name"] for item in sa.inspect(bind).get_columns("jobs")}
        if "translation_id" in job_columns:
            index_names = {item["name"] for item in sa.inspect(bind).get_indexes("jobs")}
            if "uq_jobs_translation_id" in index_names:
                op.drop_index("uq_jobs_translation_id", table_name="jobs")
            foreign_keys = {
                item["name"] for item in sa.inspect(bind).get_foreign_keys("jobs")
            }
            if "fk_jobs_translation_id_paper_translations" in foreign_keys:
                op.drop_constraint(
                    "fk_jobs_translation_id_paper_translations",
                    "jobs",
                    type_="foreignkey",
                )
            op.drop_column("jobs", "translation_id")
        if "claim_token" in job_columns:
            op.drop_column("jobs", "claim_token")
        if "claimed_at" in job_columns:
            op.drop_column("jobs", "claimed_at")
    tables = set(sa.inspect(bind).get_table_names())
    if "paper_translation_pages" in tables:
        op.drop_table("paper_translation_pages")
    tables = set(sa.inspect(bind).get_table_names())
    if "paper_translations" in tables:
        op.drop_table("paper_translations")
