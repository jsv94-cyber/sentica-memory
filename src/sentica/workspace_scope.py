"""Keep each database transaction inside one workspace.

The setting is ``sentica.workspace``. Row-level security returns no rows
when it is missing or empty. A token names the workspaces it may use, and
a request for any other workspace is refused.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple, cast

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from src.exceptions import AuthenticationException

_workspace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sentica_workspace", default=None
)
_installed = False
_listener_targets: list[tuple[Any, str, Any]] = []


class PendingQueueCount(NamedTuple):
    """Queue depth for one workspace. The claim loop reads only this."""

    workspace_name: str
    pending_queue: int


class PendingCount(NamedTuple):
    """Names and counts only. The reconciler gates read this."""

    workspace_name: str
    pending_queue: int
    pending_embeddings: int
    pending_backfills: int
    soft_deleted_documents: int
    dreams_due: int


def current_workspace() -> str | None:
    """Return the workspace bound to this task, if one is bound."""
    return _workspace.get()


def set_workspace(name: str | None) -> contextvars.Token[str | None]:
    """Bind later statements in this task to one workspace."""
    return _workspace.set(name)


def reset_workspace(token: contextvars.Token[str | None]) -> None:
    """Restore the workspace binding from ``set_workspace``."""
    _workspace.reset(token)


def allowed_from_claims(workspace: str | None, workspaces: object) -> set[str]:
    """The token's allowed set. A missing list means ``{w}``.

    A blank or non-string item, or a ``w`` claim that is not in the list, is
    refused. An empty list is refused for the same reason.
    """
    if not isinstance(workspace, str) or not workspace.strip():
        raise AuthenticationException("token workspace is missing")
    primary = workspace.strip()
    if workspaces is None:
        return {primary}
    if not isinstance(workspaces, list):
        raise AuthenticationException("token workspaces claim is malformed")
    names: set[str] = set()
    items = cast("list[object]", workspaces)
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise AuthenticationException("token workspaces claim is malformed")
        names.add(item.strip())
    if primary not in names:
        raise AuthenticationException("token workspace is outside workspaces")
    return names


def token_allowed(jwt_params: object) -> list[str] | None:
    """Workspaces the token may use, whenever authentication is on.

    ``None`` means authentication is off. An empty list means the token names
    no workspace and every target is refused.
    """
    from src.config import settings
    from src.sentica.runtime import production_enabled

    if not production_enabled() and not settings.AUTH.USE_AUTH:
        return None
    if getattr(jwt_params, "ad", False):
        return None
    listed: object = getattr(jwt_params, "workspaces", None)
    if isinstance(listed, list) and listed:
        names = [
            item
            for item in cast("list[object]", listed)
            if isinstance(item, str) and item
        ]
        if names:
            return names
    primary = getattr(jwt_params, "w", None)
    if isinstance(primary, str) and primary:
        return [primary]
    return []


def require_workspace(name: str, allowed: Iterable[str]) -> None:
    """Refuse a workspace that is not in the token's allowed set."""
    if name not in set(allowed):
        raise AuthenticationException("workspace is outside the token set")


def enforce_targets(
    path_workspace: str,
    requested: Sequence[str] | None,
    allowed: Sequence[str] | None,
) -> list[str]:
    """Workspaces for this request. Anything outside ``allowed`` is refused."""
    targets = workspace_targets(path_workspace, requested)
    if allowed is not None:
        for name in targets:
            require_workspace(name, allowed)
    return targets


def install_workspace_scope(*engines: AsyncEngine) -> None:
    """Set ``sentica.workspace`` on each connection before the first statement."""
    global _installed
    if _installed:
        return

    def _apply(
        connection: object,
        cursor: object,
        _statement: object,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        info_raw = getattr(connection, "info", None)
        if not isinstance(info_raw, dict):
            return
        info = cast("dict[str, Any]", info_raw)
        if info.pop("_sentica_skip_next", False):
            return
        if info.get("_sentica_applying"):
            return
        workspace = current_workspace() or ""
        previous = info.get("sentica_workspace")
        in_transaction = getattr(connection, "in_transaction", None)
        open_transaction = bool(in_transaction()) if callable(in_transaction) else False
        if previous is not None and previous != workspace and open_transaction:
            raise RuntimeError("workspace cannot change inside an open transaction")
        reapply = bool(info.pop("_sentica_reapply", False))
        if previous == workspace and open_transaction and not reapply:
            return
        info["_sentica_applying"] = True
        try:
            # Always transaction-local. A read runs inside BEGIN READ ONLY,
            # so the setting dies when that transaction commits or rolls back.
            cast_cursor: Any = cursor
            cast_cursor.execute(
                "SELECT set_config('sentica.workspace', %s, true)",
                (workspace,),
            )
            info["sentica_workspace"] = workspace
        finally:
            info["_sentica_applying"] = False

    def _clear(connection: object, *_extra: object) -> None:
        raw = getattr(connection, "info", None)
        if isinstance(raw, dict):
            cast("dict[str, Any]", raw).pop("sentica_workspace", None)

    def _clear_savepoint(connection: object, *_extra: object) -> None:
        raw = getattr(connection, "info", None)
        if isinstance(raw, dict):
            info = cast("dict[str, Any]", raw)
            # The pin stays for the transaction. The next statement re-applies
            # the same workspace. A different one is refused while it is open.
            # Skip the rollback statement itself so it does not re-apply early.
            info["_sentica_reapply"] = True
            info["_sentica_skip_next"] = True

    for async_engine in engines:
        sync_engine = async_engine.sync_engine
        for name, fn in (
            ("before_cursor_execute", _apply),
            ("checkin", _clear),
            ("commit", _clear),
            ("rollback", _clear),
            ("rollback_savepoint", _clear_savepoint),
        ):
            event.listen(sync_engine, name, fn)
            _listener_targets.append((sync_engine, name, fn))
    _installed = True


def remove_workspace_scope(*engines: AsyncEngine) -> None:
    """Drop workspace listeners from these engines. Other engines stay."""
    global _installed
    wanted = {engine.sync_engine for engine in engines}
    kept: list[tuple[Any, str, Any]] = []
    for target, name, fn in _listener_targets:
        if target not in wanted:
            kept.append((target, name, fn))
            continue
        if event.contains(target, name, fn):
            event.remove(target, name, fn)
    _listener_targets[:] = kept
    _installed = bool(kept)


def workspace_targets(
    path_workspace: str, requested: Sequence[str] | None
) -> list[str]:
    """One path workspace, or the requested set with the path workspace first."""
    ordered: list[str] = []
    for name in (path_workspace, *(requested or ())):
        if name and name not in ordered:
            ordered.append(name)
    return ordered


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


def fair_workspace_order(names: Sequence[str], cursor: int) -> tuple[list[str], int]:
    """Rotate a stable name order so one workspace cannot fill every batch."""
    ordered = sorted({name for name in names if name})
    if not ordered:
        return [], cursor
    start = cursor % len(ordered)
    return ordered[start:] + ordered[:start], cursor + 1


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


def apply_per_workspace(connection: Any, schema: str, statement: str) -> int:
    """Run one statement inside each workspace and commit that workspace.

    The caller is the migration role. Row-level security is forced, so an
    update that is not wrapped this way changes no rows. Each workspace is
    its own transaction, so one failure does not roll back the others.
    """
    from sqlalchemy import text

    quoted = _ident(schema)
    if connection.in_transaction():
        connection.commit()
    with connection.begin():
        rows = connection.execute(
            text(f"SELECT name FROM {quoted}.workspaces ORDER BY name")
        ).fetchall()
    updated = 0
    for (name,) in rows:
        with connection.begin():
            connection.execute(
                text("SELECT set_config('sentica.workspace', :name, true)"),
                {"name": name},
            )
            result = connection.execute(text(statement))
            updated += int(result.rowcount or 0)
    return updated


async def open_workspace(
    name: str,
    jwt_params: object,
    *,
    peer_name: str | None = None,
    session_name: str | None = None,
    allow_member_read: bool = False,
) -> contextvars.Token[str | None]:
    """Bind one workspace and run the full auth check inside that transaction."""
    from src.security import JWTParams, enforce_scope

    token = set_workspace(name)
    try:
        if isinstance(jwt_params, JWTParams):
            await enforce_scope(
                jwt_params,
                workspace_name=name,
                peer_name=peer_name,
                session_name=session_name,
                allow_member_read=allow_member_read,
            )
    except Exception:
        reset_workspace(token)
        raise
    return token


async def _function_rows(db: Any, function_name: str) -> list[Any] | None:
    from sqlalchemy import text

    from src.config import settings

    schema = settings.DB.SCHEMA
    if not schema.replace("_", "").isalnum():
        raise RuntimeError("unexpected schema name")
    if not function_name.replace("_", "").isalnum():
        raise RuntimeError("unexpected function name")
    present = await db.scalar(
        text("SELECT to_regprocedure(:name)"),
        {"name": f"{schema}.{function_name}()"},
    )
    if present is None:
        return None
    result = await db.execute(
        text("SELECT * FROM " + _ident(schema) + "." + function_name + "()")
    )
    return list(result.all())


async def fetch_pending_queue_counts(db: Any) -> list[PendingQueueCount] | None:
    """Queue depths from the claim-loop function, or None when it is absent."""
    rows = await _function_rows(db, "sentica_pending_queue_counts")
    if rows is None:
        return None
    return [
        PendingQueueCount(workspace_name=str(row[0]), pending_queue=int(row[1]))
        for row in rows
    ]


async def fetch_pending_counts(db: Any) -> list[PendingCount] | None:
    """Reconciler counts, or None when that function is not installed."""
    rows = await _function_rows(db, "sentica_pending_counts")
    if rows is None:
        return None
    return [
        PendingCount(
            workspace_name=str(row[0]),
            pending_queue=int(row[1]),
            pending_embeddings=int(row[2]),
            pending_backfills=int(row[3]),
            soft_deleted_documents=int(row[4]),
            dreams_due=int(row[5]),
        )
        for row in rows
    ]
