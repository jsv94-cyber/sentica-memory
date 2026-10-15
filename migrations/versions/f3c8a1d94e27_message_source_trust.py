"""Add messages.source_trust.

Revision ID: f3c8a1d94e27
Revises: e6b2d8f04a17
Create Date: 2026-10-15

Messages may carry owner, staff, external, or system. A missing value stays
null. The SQL is literal.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from migrations.utils import column_exists, get_schema

revision: str = "f3c8a1d94e27"
down_revision: str | None = "e6b2d8f04a17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema = get_schema()
    if column_exists("messages", "source_trust"):
        return
    op.add_column(
        "messages",
        sa.Column("source_trust", sa.Text(), nullable=True),
        schema=schema,
    )
    op.create_check_constraint(
        "messages_source_trust_valid",
        "messages",
        "source_trust IS NULL OR source_trust IN ('owner', 'staff', 'external', 'system')",
        schema=schema,
    )


def downgrade() -> None:
    schema = get_schema()
    op.drop_constraint(
        "messages_source_trust_valid",
        "messages",
        schema=schema,
        type_="check",
    )
    op.drop_column("messages", "source_trust", schema=schema)
