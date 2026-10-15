"""SQL the merged migrations produced at main cdb74439.

The freeze test renders this and the literal SQL in the migration files.
"""

from __future__ import annotations


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
    """Count collections list_due_dreams would return.

    The numeric gates are the DreamSettings defaults at the moment this SQL
    is created. list_due_dreams applies the same comparisons to the same rows.
    """
    from src.config import settings

    if not settings.DREAM.ENABLED or "omni" not in settings.DREAM.ENABLED_TYPES:
        return "0::bigint"
    threshold = int(settings.DREAM.DOCUMENT_THRESHOLD)
    idle = int(settings.DREAM.IDLE_TIMEOUT_MINUTES)
    hours = int(settings.DREAM.MIN_HOURS_BETWEEN_DREAMS)
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
