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


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise RuntimeError("unexpected identifier")
    return '"' + name + '"'


def _explicit_documents(quoted: str) -> str:
    """The explicit, live documents list_due_dreams counts."""
    return (
        f"FROM {quoted}.documents AS d "
        "WHERE d.workspace_name = c.workspace_name "
        "AND d.observer = c.observer "
        "AND d.observed = c.observed "
        "AND d.level = 'explicit' "
        "AND d.deleted_at IS NULL"
    )


def _dreams_due_sql(quoted: str) -> str:
    """Dreams due as this revision created it at main cdb74439.

    The gates are the DreamSettings defaults of that revision: 50 documents,
    60 idle minutes, and 8 hours between dreams. They are literals.
    """
    threshold = 50
    idle = 60
    hours = 8
    explicit = _explicit_documents(quoted)
    newest = f"(SELECT d.session_name {explicit} ORDER BY d.created_at DESC LIMIT 1)"
    return f"""
        (
            SELECT count(*)::bigint
            FROM {quoted}.collections AS c
            JOIN {quoted}.workspaces AS dw ON dw.name = c.workspace_name
            WHERE c.workspace_name = w.name
                AND coalesce(
                    (
                        SELECT (s.configuration #>> '{{dream,enabled}}')::boolean
                        FROM {quoted}.sessions AS s
                        WHERE s.workspace_name = c.workspace_name
                            AND s.name = {newest}
                    ),
                    (dw.configuration #>> '{{dream,enabled}}')::boolean,
                    true
                )
                AND (
                    SELECT count(*) {explicit}
                ) - coalesce(
                    (c.internal_metadata->'dream'->>'last_dream_document_count')::int,
                    0
                ) >= {threshold}
                AND (
                    SELECT max(d.created_at) {explicit}
                ) < now() - interval '{idle} minutes'
                AND {newest} IS NOT NULL
                AND (
                    c.internal_metadata->'dream'->>'last_dream_at' IS NULL
                    OR NOT (
                        (c.internal_metadata->'dream'->>'last_dream_at')
                            ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
                        AND (c.internal_metadata->'dream'->>'last_dream_at')::timestamptz
                            > now() - interval '{hours} hours'
                    )
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM {quoted}.queue AS q
                    WHERE q.processed = false
                        AND q.task_type = 'representation'
                        AND q.workspace_name = c.workspace_name
                        AND (
                            (
                                split_part(q.work_unit_key, ':', 5) = ''
                                AND split_part(q.work_unit_key, ':', 4) = c.observed
                            )
                            OR split_part(q.work_unit_key, ':', 5) = c.observed
                        )
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM {quoted}.queue AS q
                    WHERE q.task_type = 'dream'
                        AND q.workspace_name = c.workspace_name
                        AND q.created_at >= (SELECT max(d.created_at) {explicit})
                        AND split_part(q.work_unit_key, ':', 4) = c.observer
                        AND split_part(q.work_unit_key, ':', 5) = c.observed
                )
        )
    """


def pending_queue_counts_function_sql(schema: str) -> str:
    """Queue depth only. The claim loop uses this and nothing else."""
    quoted = _ident(schema)
    return f"""
        CREATE OR REPLACE FUNCTION {quoted}.sentica_pending_queue_counts()
        RETURNS TABLE (
            workspace_name text,
            pending_queue bigint
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = {schema}, pg_temp
        AS $$
            SELECT w.name,
                (
                    SELECT count(*)::bigint
                    FROM {quoted}.queue AS q
                    WHERE q.processed = false AND q.workspace_name = w.name
                )
            FROM {quoted}.workspaces AS w
            ORDER BY w.name
        $$
    """


def pending_counts_function_sql(schema: str) -> str:
    """Reconciler gates. Names and counts only, never message or document text."""
    quoted = _ident(schema)
    dreams = _dreams_due_sql(quoted)
    return f"""
        CREATE OR REPLACE FUNCTION {quoted}.sentica_pending_counts()
        RETURNS TABLE (
            workspace_name text,
            pending_queue bigint,
            pending_embeddings bigint,
            pending_backfills bigint,
            soft_deleted_documents bigint,
            dreams_due bigint
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = {schema}, pg_temp
        AS $$
            SELECT w.name,
                (
                    SELECT count(*)::bigint
                    FROM {quoted}.queue AS q
                    WHERE q.processed = false AND q.workspace_name = w.name
                ),
                (
                    SELECT count(*)::bigint
                    FROM {quoted}.message_embeddings AS e
                    WHERE e.sync_state = 'pending' AND e.workspace_name = w.name
                ),
                (
                    SELECT count(*)::bigint
                    FROM {quoted}.documents AS d
                    WHERE d.workspace_name = w.name
                        AND (
                            d.source_ids IS NOT NULL
                            OR d.internal_metadata ?| ARRAY['source_ids', 'premise_ids']
                        )
                ),
                (
                    SELECT count(*)::bigint
                    FROM {quoted}.documents AS d
                    WHERE d.workspace_name = w.name
                        AND d.deleted_at IS NOT NULL
                        AND d.deleted_at < now() - interval '5 minutes'
                ),
                {dreams}
            FROM {quoted}.workspaces AS w
            ORDER BY w.name
        $$
    """


def ddl_select_policy_sql(schema: str, table_name: str, role: str) -> str:
    """Let the migration role read one table. The runtime role is not that role."""
    table = _ident(schema) + "." + _ident(table_name)
    return f"""
        DROP POLICY IF EXISTS sentica_ddl_read ON {table};
        CREATE POLICY sentica_ddl_read ON {table}
            FOR SELECT
            TO {_ident(role)}
            USING (true)
    """


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
