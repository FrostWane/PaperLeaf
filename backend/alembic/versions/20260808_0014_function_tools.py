"""persist controlled function tool calls

Revision ID: 20260808_0014
Revises: 20260808_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0014"
down_revision: str | None = "20260808_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("call_id", sa.String(length=100), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("skill_name", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=80), nullable=False),
        sa.Column("tool_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="running", nullable=False),
        sa.Column("arguments", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("result_preview", sa.JSON(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("requires_approval", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "call_id", name="uq_agent_tool_call_run_call"),
    )
    op.create_index("ix_agent_tool_calls_run_id", "agent_tool_calls", ["run_id"])
    op.create_index("ix_agent_tool_calls_user_id", "agent_tool_calls", ["user_id"])
    op.create_index(
        "ix_agent_tool_calls_run_created", "agent_tool_calls", ["run_id", "created_at"]
    )
    op.create_index(
        "ix_agent_tool_calls_name_status", "agent_tool_calls", ["tool_name", "status"]
    )
    op.create_table(
        "agent_tool_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tool_call_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("content", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tool_call_id"], ["agent_tool_calls.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_tool_artifacts_tool_call_id", "agent_tool_artifacts", ["tool_call_id"]
    )
    op.create_index(
        "ix_agent_tool_artifacts_user_id", "agent_tool_artifacts", ["user_id"]
    )


def downgrade() -> None:
    op.drop_table("agent_tool_artifacts")
    op.drop_table("agent_tool_calls")
