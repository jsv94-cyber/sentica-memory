"""Keep each database transaction inside one workspace.

The setting is ``sentica.workspace``. Row-level security returns no rows
when it is missing or empty. A token names the workspaces it may use, and
a request for any other workspace is refused.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable, Sequence
from typing import Any, cast

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from src.exceptions import AuthenticationException

_workspace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sentica_workspace", default=None
)
_installed = False


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
        if previous == workspace and open_transaction:
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
        _clear(connection)
        raw = getattr(connection, "info", None)
        if isinstance(raw, dict):
            # The rollback statement itself is the next cursor event. Skip it
            # so it does not cache a workspace the savepoint just undid.
            cast("dict[str, Any]", raw)["_sentica_skip_next"] = True

    for async_engine in engines:
        sync_engine = async_engine.sync_engine
        event.listen(sync_engine, "before_cursor_execute", _apply)
        event.listen(sync_engine, "checkin", _clear)
        event.listen(sync_engine, "commit", _clear)
        event.listen(sync_engine, "rollback", _clear)
        event.listen(sync_engine, "rollback_savepoint", _clear_savepoint)
    _installed = True


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


def pending_counts_function_sql(schema: str) -> str:
    """One read-only function. It returns counts, never message or document text."""
    quoted = _ident(schema)
    return f"""
        CREATE OR REPLACE FUNCTION {quoted}.sentica_pending_counts()
        RETURNS TABLE (
            workspace_name text,
            pending_queue bigint,
            pending_embeddings bigint
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
                )
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
    """Run one statement inside each workspace, with the setting local to it.

    The caller is the migration role. Row-level security is forced, so an
    update that is not wrapped this way changes no rows.
    """
    from sqlalchemy import text

    quoted = _ident(schema)
    rows = connection.execute(
        text(f"SELECT name FROM {quoted}.workspaces ORDER BY name")
    ).fetchall()
    updated = 0
    for (name,) in rows:
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


async def fetch_pending_counts(
    db: Any,
) -> list[tuple[str, int, int]] | None:
    """Counts from the migration-owned function, or None when it is not installed."""
    from sqlalchemy import text

    from src.config import settings

    schema = settings.DB.SCHEMA
    if not schema.replace("_", "").isalnum():
        raise RuntimeError("unexpected schema name")
    present = await db.scalar(
        text("SELECT to_regprocedure(:name)"),
        {"name": f"{schema}.sentica_pending_counts()"},
    )
    if present is None:
        return None
    result = await db.execute(
        text(
            "SELECT workspace_name, pending_queue, pending_embeddings FROM "
            + _ident(schema)
            + ".sentica_pending_counts()"
        )
    )
    return [(str(row[0]), int(row[1]), int(row[2])) for row in result.all()]
