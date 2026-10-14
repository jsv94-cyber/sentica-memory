"""Enable row-level security on workspace-scoped memory tables.

Revision ID: a9c1e3f5b7d2
Revises: 3e7a1c9d5b20
Create Date: 2026-10-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from src.config import settings
from src.sentica.workspace_scope import workspace_policy_sql

revision: str = "a9c1e3f5b7d2"
down_revision: str | None = "3e7a1c9d5b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quote(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise RuntimeError(f"unexpected identifier {name}")
    return '"' + name + '"'


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
    quoted_schema = _quote(schema)
    for (table_name,) in rows:
        op.execute(sa.text(workspace_policy_sql(schema, table_name)))

    can_create = bind.execute(
        sa.text(
            """
            SELECT rolsuper OR rolcreaterole
            FROM pg_roles
            WHERE rolname = current_user
            """
        )
    ).scalar()
    with op.get_context().autocommit_block():
        if can_create:
            op.execute(
                sa.text(
                    """
                    DO $$ BEGIN
                        CREATE ROLE sentica_ddl NOLOGIN NOBYPASSRLS;
                    EXCEPTION WHEN duplicate_object THEN NULL;
                    END $$
                    """
                )
            )
            op.execute(sa.text("GRANT sentica_ddl TO current_user"))
            op.execute(
                sa.text(
                    """
                    DO $$ BEGIN
                        CREATE ROLE sentica_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
                    EXCEPTION WHEN duplicate_object THEN NULL;
                    END $$
                    """
                )
            )
            op.execute(sa.text("ALTER ROLE sentica_app NOBYPASSRLS"))
            op.execute(sa.text("GRANT sentica_app TO current_user"))
    exists = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'sentica_ddl'")
    ).scalar()
    if exists is not None:
        member = bind.execute(
            sa.text("SELECT pg_has_role(current_user, 'sentica_ddl', 'member')")
        ).scalar()
        if member:
            # The new owner must be allowed to create objects in the schema.
            op.execute(
                sa.text(f"GRANT USAGE, CREATE ON SCHEMA {quoted_schema} TO sentica_ddl")
            )
            for (table_name,) in rows:
                table = quoted_schema + "." + _quote(table_name)
                op.execute(sa.text(f"ALTER TABLE {table} OWNER TO sentica_ddl"))
                op.execute(
                    sa.text(
                        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} "
                        "TO current_user"
                    )
                )
    if can_create:
        op.execute(sa.text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO sentica_app"))
        op.execute(
            sa.text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                f"IN SCHEMA {quoted_schema} TO sentica_app"
            )
        )


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
