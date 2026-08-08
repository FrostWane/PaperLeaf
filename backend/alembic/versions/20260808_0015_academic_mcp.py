"""persist controlled academic MCP configuration

Revision ID: 20260808_0015
Revises: 20260808_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0015"
down_revision: str | None = "20260808_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_server_configs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("endpoint_url", sa.String(length=1000), nullable=False),
        sa.Column(
            "transport",
            sa.String(length=32),
            server_default="streamable_http",
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "allowed_hosts", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False
        ),
        sa.Column("health_status", sa.String(length=32), server_default="unknown", nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("circuit_open_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "mcp_tool_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.String(length=64), nullable=False),
        sa.Column("normalized_name", sa.String(length=160), nullable=False),
        sa.Column("remote_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "input_schema", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False
        ),
        sa.Column(
            "annotations", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False
        ),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["server_id"], ["mcp_server_configs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "normalized_name", name="uq_mcp_tool_server_name"),
    )
    op.create_index("ix_mcp_tool_snapshots_server_id", "mcp_tool_snapshots", ["server_id"])
    op.create_index(
        "ix_mcp_tool_snapshots_server",
        "mcp_tool_snapshots",
        ["server_id", "discovered_at"],
    )


def downgrade() -> None:
    op.drop_table("mcp_tool_snapshots")
    op.drop_table("mcp_server_configs")
