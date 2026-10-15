"""Queue workspace is required, and the migration role owns the counts function.

Revision ID: c4e8b1a2d6f0
Revises: a9c1e3f5b7d2
Create Date: 2026-10-14

The runtime role is ``current_user`` without the ``_ddl`` suffix. It receives
DML and EXECUTE, not ownership and not membership in the migration role.
Downgrade gives table ownership back to that runtime role.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from src.config import settings
from src.sentica.workspace_scope import (
    ddl_select_policy_sql,
    pending_counts_function_sql,
    pending_queue_counts_function_sql,
)

revision: str = "c4e8b1a2d6f0"
down_revision: str | None = "a9c1e3f5b7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quote(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise RuntimeError(f"unexpected identifier {name}")
    return '"' + name + '"'


def _runtime_role(bind: sa.Connection) -> str | None:
    current = bind.execute(sa.text("SELECT current_user")).scalar()
    if not isinstance(current, str) or not current.endswith("_ddl"):
        return None
    runtime = current[: -len("_ddl")]
    if not runtime.replace("_", "").isalnum():
        return None
    exists = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
        {"name": runtime},
    ).scalar()
    if exists is None:
        return None
    return runtime


def _tables(bind: sa.Connection, schema: str) -> list[str]:
    rows = bind.execute(
        sa.text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = :schema AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        ),
        {"schema": schema},
    ).fetchall()
    return [row[0] for row in rows]


def upgrade() -> None:
    schema = settings.DB.SCHEMA
    bind = op.get_bind()
    quoted = _quote(schema)
    # FORCE RLS hides NULL workspace rows, so the delete would match nothing
    # and the NOT NULL change would then fail. Lift the force around it.
    op.execute(sa.text(f"ALTER TABLE {quoted}.queue NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {quoted}.queue DISABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"DELETE FROM {quoted}.queue WHERE workspace_name IS NULL"  # noqa: S608
        )
    )
    op.execute(sa.text(f"ALTER TABLE {quoted}.queue ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {quoted}.queue FORCE ROW LEVEL SECURITY"))
    op.alter_column("queue", "workspace_name", nullable=False, schema=schema)
    op.execute(sa.text(pending_queue_counts_function_sql(schema)))
    op.execute(sa.text(pending_counts_function_sql(schema)))
    op.create_index(
        "ix_documents_soft_deleted_cleanup",
        "documents",
        ["workspace_name", "deleted_at"],
        schema=schema,
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )
    current = bind.execute(sa.text("SELECT current_user")).scalar()
    if not isinstance(current, str) or not current.endswith("_ddl"):
        raise RuntimeError("Refusing to migrate: the migrating role must end in _ddl")
    runtime = _runtime_role(bind)
    if runtime is None:
        raise RuntimeError("Refusing to migrate: the runtime role does not exist")
    role = _quote(runtime)
    op.execute(sa.text(f"GRANT USAGE ON SCHEMA {quoted} TO {role}"))
    op.execute(
        sa.text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {quoted} TO {role}"
        )
    )
    op.execute(
        sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {quoted} TO {role}")
    )
    op.execute(
        sa.text(
            f"REVOKE INSERT, UPDATE, DELETE ON {quoted}.alembic_version FROM {role}"
        )
    )
    op.execute(sa.text(f"GRANT SELECT ON {quoted}.alembic_version TO {role}"))
    for function_name in (
        "sentica_pending_queue_counts",
        "sentica_pending_counts",
    ):
        op.execute(
            sa.text(f"REVOKE ALL ON FUNCTION {quoted}.{function_name}() FROM PUBLIC")
        )
        op.execute(
            sa.text(f"GRANT EXECUTE ON FUNCTION {quoted}.{function_name}() TO {role}")
        )
    current = bind.execute(sa.text("SELECT current_user")).scalar()
    if isinstance(current, str):
        # The function is SECURITY DEFINER and owned by this role. FORCE RLS
        # would otherwise hide every row, so each scanned table has a
        # role-only read policy. The runtime role is not that role.
        for table_name in (
            "queue",
            "message_embeddings",
            "documents",
            "collections",
        ):
            op.execute(sa.text(ddl_select_policy_sql(schema, table_name, current)))


def downgrade() -> None:
    schema = settings.DB.SCHEMA
    bind = op.get_bind()
    quoted = _quote(schema)
    runtime = _runtime_role(bind)
    if runtime is not None:
        for table_name in _tables(bind, schema):
            table = quoted + "." + _quote(table_name)
            op.execute(sa.text(f"ALTER TABLE {table} OWNER TO {_quote(runtime)}"))
    for table_name in (
        "queue",
        "message_embeddings",
        "documents",
        "collections",
    ):
        table = quoted + "." + _quote(table_name)
        op.execute(sa.text(f"DROP POLICY IF EXISTS sentica_ddl_read ON {table}"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {quoted}.sentica_pending_counts()"))
    op.execute(
        sa.text(f"DROP FUNCTION IF EXISTS {quoted}.sentica_pending_queue_counts()")
    )
    op.drop_index(
        "ix_documents_soft_deleted_cleanup",
        table_name="documents",
        schema=schema,
    )
    op.alter_column("queue", "workspace_name", nullable=True, schema=schema)
