"""持久化问答会话、消息、Agent 事件与后台作业。

Revision ID: 20260806_0007
Revises: 20260806_0006
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260806_0007"
down_revision = "20260806_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "chat_sessions" not in tables:
        op.create_table(
            "chat_sessions",
            sa.Column("id", sa.String(length=100), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("type", sa.String(length=32), nullable=False),
            sa.Column("paper_id", sa.String(length=36), nullable=True),
            sa.Column("collection_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_chat_sessions_user_id", "chat_sessions", ["user_id"])
        op.create_index(
            "ix_chat_sessions_user_updated",
            "chat_sessions",
            ["user_id", "updated_at"],
        )

    agent_columns = {item["name"] for item in sa.inspect(bind).get_columns("agent_runs")}
    if "cancel_requested" not in agent_columns:
        op.add_column(
            "agent_runs",
            sa.Column(
                "cancel_requested",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if "scope_snapshot" not in agent_columns:
        op.add_column(
            "agent_runs",
            sa.Column("scope_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        )
    for column_name, column in (
        ("user_message_id", sa.Column("user_message_id", sa.String(length=36), nullable=True)),
        (
            "assistant_message_id",
            sa.Column("assistant_message_id", sa.String(length=36), nullable=True),
        ),
        ("request_hash", sa.Column("request_hash", sa.String(length=64), nullable=True)),
        (
            "legacy_session_id",
            sa.Column("legacy_session_id", sa.String(length=100), nullable=True),
        ),
        (
            "resume_action_id",
            sa.Column("resume_action_id", sa.String(length=100), nullable=True),
        ),
        (
            "resume_decision",
            sa.Column("resume_decision", sa.String(length=32), nullable=True),
        ),
    ):
        if column_name not in agent_columns:
            op.add_column("agent_runs", column)
    agent_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("agent_runs")}
    for index_name, column_name in (
        ("uq_agent_runs_user_message_id", "user_message_id"),
        ("uq_agent_runs_assistant_message_id", "assistant_message_id"),
    ):
        if index_name not in agent_indexes:
            op.create_index(index_name, "agent_runs", [column_name], unique=True)

    # 旧版 session_id 不是业务会话外键，且不同用户可能同名。每个历史 Run
    # 建立独立只读会话，避免迁移时合并用户数据或产生主键冲突。
    agent_runs = sa.table(
        "agent_runs",
        sa.column("id", sa.String),
        sa.column("user_id", sa.String),
        sa.column("session_id", sa.String),
        sa.column("legacy_session_id", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    chat_sessions = sa.table(
        "chat_sessions",
        sa.column("id", sa.String),
        sa.column("user_id", sa.String),
        sa.column("title", sa.String),
        sa.column("type", sa.String),
        sa.column("paper_id", sa.String),
        sa.column("collection_id", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    existing_session_ids = set(
        bind.execute(sa.select(chat_sessions.c.id)).scalars().all()
    )
    for row in bind.execute(sa.select(agent_runs)).mappings():
        # Alembic 正常只执行一次；此门禁同时保护人工恢复后的重复执行，避免把
        # legacy_session_id 覆盖为 legacy-<run_id> 而丢失原始值。
        if row["legacy_session_id"] is not None:
            continue
        if row["session_id"] in existing_session_ids:
            continue
        legacy_id = f"legacy-{row['id']}"
        if legacy_id not in existing_session_ids:
            bind.execute(
                chat_sessions.insert().values(
                    id=legacy_id,
                    user_id=row["user_id"],
                    title="历史问答",
                    type="library",
                    paper_id=None,
                    collection_id=None,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
            existing_session_ids.add(legacy_id)
        bind.execute(
            agent_runs.update()
            .where(agent_runs.c.id == row["id"])
            .values(
                legacy_session_id=row["session_id"],
                session_id=legacy_id,
            )
        )

    foreign_keys = sa.inspect(bind).get_foreign_keys("agent_runs")
    has_session_foreign_key = any(
        item.get("constrained_columns") == ["session_id"]
        and item.get("referred_table") == "chat_sessions"
        for item in foreign_keys
    )
    if not has_session_foreign_key:
        op.create_foreign_key(
            "fk_agent_runs_session_id_chat_sessions",
            "agent_runs",
            "chat_sessions",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "chat_messages" not in tables:
        op.create_table(
            "chat_messages",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("session_id", sa.String(length=100), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("citations", sa.JSON(), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=True),
            sa.Column("client_message_id", sa.String(length=100), nullable=True),
            sa.Column("request_hash", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["chat_sessions.id"],
                name="fk_chat_messages_session_id_chat_sessions",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["run_id"],
                ["agent_runs.id"],
                name="fk_chat_messages_run_id_agent_runs",
                ondelete="CASCADE",
                deferrable=True,
                initially="DEFERRED",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "session_id",
                "client_message_id",
                name="uq_chat_message_client_id",
            ),
            sa.UniqueConstraint(
                "session_id", "sequence", name="uq_chat_message_sequence"
            ),
        )
        op.create_index("ix_chat_messages_run_id", "chat_messages", ["run_id"])
        op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])
        op.create_index(
            "ix_chat_messages_session_created",
            "chat_messages",
            ["session_id", "created_at"],
        )

    agent_foreign_keys = {
        item.get("name") for item in sa.inspect(bind).get_foreign_keys("agent_runs")
    }
    for constraint_name, column_name in (
        ("fk_agent_runs_user_message_id_chat_messages", "user_message_id"),
        (
            "fk_agent_runs_assistant_message_id_chat_messages",
            "assistant_message_id",
        ),
    ):
        if constraint_name not in agent_foreign_keys:
            op.create_foreign_key(
                constraint_name,
                "agent_runs",
                "chat_messages",
                [column_name],
                ["id"],
                ondelete="SET NULL",
                deferrable=True,
                initially="DEFERRED",
            )

    agent_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("agent_runs")}
    if "uq_agent_runs_active_session" not in agent_indexes:
        op.create_index(
            "uq_agent_runs_active_session",
            "agent_runs",
            ["session_id"],
            unique=True,
            postgresql_where=sa.text(
                "status IN ('pending', 'running', 'interrupted')"
            ),
            sqlite_where=sa.text(
                "status IN ('pending', 'running', 'interrupted')"
            ),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "agent_run_events" not in tables:
        op.create_table(
            "agent_run_events",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_key", sa.String(length=120), nullable=True),
            sa.Column("event", sa.String(length=64), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "run_id", "sequence", name="uq_agent_run_event_sequence"
            ),
            sa.UniqueConstraint("run_id", "event_key", name="uq_agent_run_event_key"),
        )
        op.create_index("ix_agent_run_events_run_id", "agent_run_events", ["run_id"])
        op.create_index(
            "ix_agent_run_events_run_sequence",
            "agent_run_events",
            ["run_id", "sequence"],
        )

    job_columns = {item["name"] for item in sa.inspect(bind).get_columns("jobs")}
    if "agent_run_id" not in job_columns:
        op.add_column("jobs", sa.Column("agent_run_id", sa.String(length=36), nullable=True))
        op.create_foreign_key(
            "fk_jobs_agent_run_id_agent_runs",
            "jobs",
            "agent_runs",
            ["agent_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            "uq_jobs_agent_run_id", "jobs", ["agent_run_id"], unique=True
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "jobs" in tables:
        job_columns = {item["name"] for item in inspector.get_columns("jobs")}
        if "agent_run_id" in job_columns:
            job_indexes = {item["name"] for item in inspector.get_indexes("jobs")}
            if "uq_jobs_agent_run_id" in job_indexes:
                op.drop_index("uq_jobs_agent_run_id", table_name="jobs")
            job_fks = {
                item.get("name") for item in inspector.get_foreign_keys("jobs")
            }
            if "fk_jobs_agent_run_id_agent_runs" in job_fks:
                op.drop_constraint(
                    "fk_jobs_agent_run_id_agent_runs", "jobs", type_="foreignkey"
                )
            op.drop_column("jobs", "agent_run_id")

    if "agent_run_events" in tables:
        op.drop_table("agent_run_events")

    if "agent_runs" in tables:
        agent_fks = {
            item.get("name") for item in inspector.get_foreign_keys("agent_runs")
        }
        for constraint_name in (
            "fk_agent_runs_assistant_message_id_chat_messages",
            "fk_agent_runs_user_message_id_chat_messages",
        ):
            if constraint_name in agent_fks:
                op.drop_constraint(
                    constraint_name, "agent_runs", type_="foreignkey"
                )

    if "chat_messages" in tables:
        op.drop_table("chat_messages")

    if "agent_runs" in tables:
        inspector = sa.inspect(bind)
        agent_indexes = {
            item["name"] for item in inspector.get_indexes("agent_runs")
        }
        for index_name in (
            "uq_agent_runs_active_session",
            "uq_agent_runs_assistant_message_id",
            "uq_agent_runs_user_message_id",
        ):
            if index_name in agent_indexes:
                op.drop_index(index_name, table_name="agent_runs")
        agent_fks = inspector.get_foreign_keys("agent_runs")
        for foreign_key in agent_fks:
            if (
                foreign_key.get("constrained_columns") == ["session_id"]
                and foreign_key.get("referred_table") == "chat_sessions"
                and foreign_key.get("name")
            ):
                op.drop_constraint(
                    foreign_key["name"], "agent_runs", type_="foreignkey"
                )
        agent_columns = {
            item["name"] for item in sa.inspect(bind).get_columns("agent_runs")
        }
        if "legacy_session_id" in agent_columns:
            bind.execute(
                sa.text(
                    "UPDATE agent_runs SET session_id = legacy_session_id "
                    "WHERE legacy_session_id IS NOT NULL"
                )
            )
        for column_name in (
            "scope_snapshot",
            "cancel_requested",
            "resume_decision",
            "resume_action_id",
            "request_hash",
            "legacy_session_id",
            "assistant_message_id",
            "user_message_id",
        ):
            if column_name in agent_columns:
                op.drop_column("agent_runs", column_name)

    if "chat_sessions" in tables:
        op.drop_table("chat_sessions")
