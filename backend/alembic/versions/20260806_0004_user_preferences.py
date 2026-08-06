"""增加用户昵称与持久化偏好。

Revision ID: 20260806_0004
Revises: 20260729_0003
"""

import sqlalchemy as sa

from alembic import op

revision = "20260806_0004"
down_revision = "20260729_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("users")}
    if "display_name" not in columns:
        op.add_column("users", sa.Column("display_name", sa.String(length=100), nullable=True))
    if "preferences" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "preferences",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )
        op.alter_column("users", "preferences", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("users")}
    if "preferences" in columns:
        op.drop_column("users", "preferences")
    if "display_name" in columns:
        op.drop_column("users", "display_name")
