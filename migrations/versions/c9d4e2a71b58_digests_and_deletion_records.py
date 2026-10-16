"""Move digests and deletion records out of workspace metadata.

Revision ID: c9d4e2a71b58
Revises: b8e3c1a94d20
Create Date: 2026-10-16

The SQL is literal. Both tables are workspace-keyed, with ENABLE, FORCE,
and the sentica_workspace policy. Deletion rows store hashes, never names.
Existing JSON is copied, then those keys are removed.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from src.config import settings

revision: str = "c9d4e2a71b58"
down_revision: str | None = "b8e3c1a94d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = """
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
    );
"""


def _quote(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise RuntimeError(f"unexpected identifier {name}")
    return '"' + name + '"'


def _runtime_role(bind: sa.Connection) -> str:
    current = bind.execute(sa.text("SELECT current_user")).scalar()
    if not isinstance(current, str) or not current.endswith("_ddl"):
        raise RuntimeError("Refusing to migrate: the migrating role must end in _ddl")
    runtime = current[: -len("_ddl")]
    if not runtime.replace("_", "").isalnum():
        raise RuntimeError("Refusing to migrate: the migrating role must end in _ddl")
    exists = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
        {"name": runtime},
    ).scalar()
    if exists is None:
        raise RuntimeError("Refusing to migrate: the runtime role does not exist")
    return runtime


def upgrade() -> None:
    schema = _quote(settings.DB.SCHEMA)
    bind = op.get_bind()
    runtime_name = _runtime_role(bind)
    runtime = _quote(runtime_name)
    ddl = _quote(runtime_name + "_ddl")
    digests = schema + ".dream_digests"
    deletions = schema + ".deletion_records"
    workspaces = schema + ".workspaces"
    op.execute(
        sa.text(
            f"""
            CREATE TABLE {digests} (
                id TEXT PRIMARY KEY,
                workspace_name TEXT NOT NULL
                    REFERENCES {workspaces} (name),
                kind TEXT NOT NULL,
                period TEXT NOT NULL,
                content TEXT NOT NULL,
                source_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                tokens INTEGER NOT NULL DEFAULT 0,
                external BOOLEAN NOT NULL DEFAULT false,
                skipped BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT dream_digests_kind_valid
                    CHECK (kind IN ('daily', 'weekly')),
                CONSTRAINT dream_digests_id_length CHECK (length(id) = 21),
                CONSTRAINT dream_digests_workspace_period
                    UNIQUE (workspace_name, kind, period)
            )
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TABLE {deletions} (
                id TEXT PRIMARY KEY,
                workspace_name TEXT NOT NULL
                    REFERENCES {workspaces} (name),
                peer_hash TEXT,
                subject_hash TEXT,
                document_count INTEGER NOT NULL,
                embedding_count INTEGER NOT NULL,
                summary_count INTEGER NOT NULL,
                legacy BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT deletion_records_id_length CHECK (length(id) = 21)
            )
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO {digests} (
                id, workspace_name, kind, period, content, source_ids,
                tokens, external, skipped, created_at
            )
            SELECT
                elem->>'id',
                w.name,
                elem->>'kind',
                elem->>'period',
                COALESCE(elem->>'content', ''),
                COALESCE(elem->'source_ids', '[]'::jsonb),
                COALESCE((elem->>'tokens')::integer, 0),
                COALESCE((elem->>'external')::boolean, false),
                COALESCE((elem->>'skipped')::boolean, false),
                COALESCE((elem->>'created_at')::timestamptz, now())
            FROM {workspaces} AS w
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(w.internal_metadata->'dreams') = 'array'
                    THEN w.internal_metadata->'dreams'
                    ELSE '[]'::jsonb
                END
            ) AS elem
            WHERE length(elem->>'id') = 21
                AND elem->>'kind' IN ('daily', 'weekly')
                AND elem->>'period' IS NOT NULL
            ON CONFLICT (workspace_name, kind, period) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO {deletions} (
                id, workspace_name, peer_hash, subject_hash,
                document_count, embedding_count, summary_count, legacy, created_at
            )
            SELECT
                elem->>'id',
                w.name,
                NULL,
                NULL,
                COALESCE((elem->>'document_count')::integer, 0),
                COALESCE((elem->>'embedding_count')::integer, 0),
                COALESCE((elem->>'summary_count')::integer, 0),
                true,
                now()
            FROM {workspaces} AS w
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(w.internal_metadata->'deletion_records')
                        = 'array'
                    THEN w.internal_metadata->'deletion_records'
                    ELSE '[]'::jsonb
                END
            ) AS elem
            WHERE length(elem->>'id') = 21
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {workspaces}
            SET internal_metadata = COALESCE(internal_metadata, '{{}}'::jsonb)
                - 'dreams' - 'deletion_records'
            """
        )
    )
    op.execute(sa.text(_POLICY.format(table=digests)))
    op.execute(sa.text(_POLICY.format(table=deletions)))
    for table in (digests, deletions):
        op.execute(
            sa.text(
                f"""
                DROP POLICY IF EXISTS sentica_ddl_read ON {table};
                CREATE POLICY sentica_ddl_read ON {table}
                    FOR SELECT TO {ddl} USING (true)
                """
            )
        )
    op.execute(
        sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {digests} TO {runtime}")
    )
    op.execute(
        sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {deletions} TO {runtime}")
    )


def downgrade() -> None:
    schema = _quote(settings.DB.SCHEMA)
    digests = schema + ".dream_digests"
    deletions = schema + ".deletion_records"
    workspaces = schema + ".workspaces"
    op.execute(
        sa.text(
            f"""
            UPDATE {workspaces} AS workspace
            SET internal_metadata = COALESCE(workspace.internal_metadata, '{{}}'::jsonb)
                || jsonb_build_object(
                    'dreams',
                    COALESCE((
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'id', digest.id,
                                'kind', digest.kind,
                                'period', digest.period,
                                'content', digest.content,
                                'source_ids', digest.source_ids,
                                'tokens', digest.tokens,
                                'external', digest.external,
                                'skipped', digest.skipped
                            )
                            ORDER BY digest.created_at, digest.id
                        )
                        FROM {digests} AS digest
                        WHERE digest.workspace_name = workspace.name
                    ), '[]'::jsonb),
                    'deletion_records',
                    COALESCE((
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'id', record.id,
                                'document_count', record.document_count,
                                'embedding_count', record.embedding_count,
                                'summary_count', record.summary_count,
                                'legacy', record.legacy
                            )
                            ORDER BY record.created_at, record.id
                        )
                        FROM {deletions} AS record
                        WHERE record.workspace_name = workspace.name
                    ), '[]'::jsonb)
                )
            """
        )
    )
    op.execute(sa.text(f"DROP TABLE IF EXISTS {deletions}"))
    op.execute(sa.text(f"DROP TABLE IF EXISTS {digests}"))
