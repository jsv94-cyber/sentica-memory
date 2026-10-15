"""Conclusions can go stale, be superseded, decay, and be deleted.

Revision ID: a7d4e2c91b08
Revises: f3c8a1d94e27
Create Date: 2026-10-15

The SQL is literal. Deletion counts are stored on the workspace, not a new table.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from migrations.utils import column_exists, get_schema, index_exists

revision: str = "a7d4e2c91b08"
down_revision: str | None = "f3c8a1d94e27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema = get_schema()
    if not column_exists("documents", "subject_ref"):
        op.add_column(
            "documents",
            sa.Column("subject_ref", sa.Text(), nullable=True),
            schema=schema,
        )
    if not column_exists("documents", "stale"):
        op.add_column(
            "documents",
            sa.Column(
                "stale", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
            schema=schema,
        )
    if not column_exists("documents", "superseded_by"):
        op.add_column(
            "documents",
            sa.Column("superseded_by", sa.Text(), nullable=True),
            schema=schema,
        )
    if not column_exists("documents", "weight"):
        op.add_column(
            "documents",
            sa.Column(
                "weight", sa.Float(), nullable=False, server_default=sa.text("1")
            ),
            schema=schema,
        )
    if not column_exists("documents", "retrieved_count"):
        op.add_column(
            "documents",
            sa.Column(
                "retrieved_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            schema=schema,
        )
    if not column_exists("documents", "used_count"):
        op.add_column(
            "documents",
            sa.Column(
                "used_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            schema=schema,
        )
    if not column_exists("documents", "last_used_at"):
        op.add_column(
            "documents",
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
    if not column_exists("documents", "decayed_at"):
        op.add_column(
            "documents",
            sa.Column("decayed_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
    if not index_exists("documents", "ix_documents_workspace_subject_ref"):
        op.create_index(
            "ix_documents_workspace_subject_ref",
            "documents",
            ["workspace_name", "subject_ref"],
            schema=schema,
        )


def downgrade() -> None:
    schema = get_schema()
    if index_exists("documents", "ix_documents_workspace_subject_ref"):
        op.drop_index(
            "ix_documents_workspace_subject_ref", table_name="documents", schema=schema
        )
    for column in (
        "decayed_at",
        "last_used_at",
        "used_count",
        "retrieved_count",
        "weight",
        "superseded_by",
        "stale",
        "subject_ref",
    ):
        if column_exists("documents", column):
            op.drop_column("documents", column, schema=schema)
