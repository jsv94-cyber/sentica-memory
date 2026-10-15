"""Enable row-level security on workspace-scoped memory tables.

Revision ID: a9c1e3f5b7d2
Revises: 3e7a1c9d5b20
Create Date: 2026-10-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from src.config import settings

revision: str = "a9c1e3f5b7d2"
down_revision: str | None = "3e7a1c9d5b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quote(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise RuntimeError(f"unexpected identifier {name}")
    return '"' + name + '"'


def workspace_policy_sql(schema: str, table_name: str) -> str:
    """ENABLE and FORCE the fail-closed workspace policy on one table."""
    if not schema.replace("_", "").isalnum():
        raise RuntimeError("unexpected schema name")
    if not table_name.replace("_", "").isalnum():
        raise RuntimeError("unexpected table name")
    table = f'"{schema}"."{table_name}"'
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS sentica_workspace ON {table};
        CREATE POLICY sentica_workspace ON {table}
            USING (
                coalesce(current_setting('sentica.workspace', true), '') <> ''
                AND workspace_name = current_setting('sentica.workspace', true)
            )
            WITH CHECK (
                coalesce(current_setting('sentica.workspace', true), '') <> ''
                AND workspace_name = current_setting('sentica.workspace', true)
            )
    """


def upgrade() -> None:
    schema = settings.DB.SCHEMA
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = :schema AND column_name = 'workspace_name'
            ORDER BY table_name
            """
        ),
        {"schema": schema},
    ).fetchall()
    for (table_name,) in rows:
        op.execute(sa.text(workspace_policy_sql(schema, table_name)))


def downgrade() -> None:
    schema = settings.DB.SCHEMA
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = :schema AND column_name = 'workspace_name'
            ORDER BY table_name
            """
        ),
        {"schema": schema},
    ).fetchall()
    quoted_schema = _quote(schema)
    for (table_name,) in rows:
        table = quoted_schema + "." + _quote(table_name)
        op.execute(sa.text(f"DROP POLICY IF EXISTS sentica_workspace ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
