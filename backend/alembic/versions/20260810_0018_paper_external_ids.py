"""persist academic provider entity identifiers

Revision ID: 20260810_0018
Revises: 20260809_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0018"
down_revision: str | None = "20260809_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "papers",
        sa.Column(
            "academic_external_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("papers", "academic_external_ids")
