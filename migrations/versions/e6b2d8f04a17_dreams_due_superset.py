"""Replace dreams due with the superset count.

Revision ID: e6b2d8f04a17
Revises: c4e8b1a2d6f0
Create Date: 2026-10-15

sentica_pending_counts() from c4e8b1a2d6f0 baked dream settings. This
revision replaces that column with the superset: collections whose explicit
document count exceeds last_dream_document_count. The SQL is literal.

"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

from src.config import settings

revision: str = "e6b2d8f04a17"
down_revision: str | None = "c4e8b1a2d6f0"
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


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise RuntimeError("unexpected identifier")
    return '"' + name + '"'


def _dreams_due_sql(quoted: str) -> str:
    """Collections whose explicit documents exceed the last dream count.

    This is a superset of list_due_dreams. It bakes in no thresholds, idle
    window, session setting, or queue key. list_due_dreams applies those.
    """
    return f"""
        (
            SELECT count(*)::bigint
            FROM {quoted}.collections AS c
            WHERE c.workspace_name = w.name
                AND (
                    SELECT count(*)
                    FROM {quoted}.documents AS d
                    WHERE d.workspace_name = c.workspace_name
                        AND d.observer = c.observer
                        AND d.observed = c.observed
                        AND d.level = 'explicit'
                ) > coalesce(
                    (c.internal_metadata->'dream'->>'last_dream_document_count')::int,
                    0
                )
        )
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


def _frozen_counts_sql(schema: str) -> str:
    path = Path(__file__).with_name("c4e8b1a2d6f0_runtime_role_and_pending_counts.py")
    spec = importlib.util.spec_from_file_location("c4e8b1a2d6f0_sql", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("frozen counts migration is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sql = module.pending_counts_function_sql(schema)
    if not isinstance(sql, str):
        raise RuntimeError("frozen counts SQL is missing")
    return sql


def _grant(schema: str, runtime: str) -> None:
    quoted = _quote(schema)
    role = _quote(runtime)
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {quoted}.sentica_pending_counts() FROM PUBLIC")
    )
    op.execute(
        sa.text(
            f"GRANT EXECUTE ON FUNCTION {quoted}.sentica_pending_counts() TO {role}"
        )
    )


def upgrade() -> None:
    schema = settings.DB.SCHEMA
    bind = op.get_bind()
    runtime = _runtime_role(bind)
    if runtime is None:
        raise RuntimeError("Refusing to migrate: the runtime role does not exist")
    op.execute(sa.text(pending_counts_function_sql(schema)))
    _grant(schema, runtime)


def downgrade() -> None:
    schema = settings.DB.SCHEMA
    bind = op.get_bind()
    runtime = _runtime_role(bind)
    if runtime is None:
        raise RuntimeError("Refusing to migrate: the runtime role does not exist")
    op.execute(sa.text(_frozen_counts_sql(schema)))
    _grant(schema, runtime)
