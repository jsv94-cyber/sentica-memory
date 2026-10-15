"""Keyword half of conclusion retrieval.

Revision ID: b8e3c1a94d20
Revises: a7d4e2c91b08
Create Date: 2026-10-15

The SQL is literal. The index matches the one on the document model.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from migrations.utils import get_schema, index_exists

revision: str = "b8e3c1a94d20"
down_revision: str | None = "a7d4e2c91b08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema = get_schema()
    if not schema.replace("_", "").isalnum():
        raise RuntimeError("unexpected schema name")
    if not index_exists("documents", "ix_documents_content_gin"):
        op.execute(
            sa.text(
                "CREATE INDEX ix_documents_content_gin ON "
                + schema
                + ".documents USING gin (to_tsvector('english', content))"
            )
        )


def downgrade() -> None:
    schema = get_schema()
    if index_exists("documents", "ix_documents_content_gin"):
        op.drop_index("ix_documents_content_gin", table_name="documents", schema=schema)
