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
    """The token's allowed set. A single ``w`` claim is a one-item set."""
    if isinstance(workspaces, list):
        names: list[str] = []
        items = cast("list[object]", workspaces)
        for item in items:
            if not isinstance(item, str) or not item.strip():
                break
            names.append(item.strip())
        else:
            return set(names)
    if isinstance(workspace, str) and workspace.strip():
        return {workspace.strip()}
    return set()


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
        if info.get("_sentica_applying"):
            return
        workspace = current_workspace() or ""
        if info.get("sentica_workspace") == workspace:
            return
        info["_sentica_applying"] = True
        try:
            # SET LOCAL inside a transaction so the setting dies at commit.
            # An autocommit statement is its own transaction, so the same
            # call has to be session-level or the next statement loses it.
            read_options_fn = getattr(connection, "get_execution_options", None)
            read_options = cast(
                "dict[str, Any]",
                read_options_fn() if callable(read_options_fn) else {},
            )
            autocommit = read_options.get("isolation_level") == "AUTOCOMMIT"
            cast_cursor: Any = cursor
            cast_cursor.execute(
                "SELECT set_config('sentica.workspace', %s, %s)",
                (workspace, not autocommit),
            )
            info["sentica_workspace"] = workspace
        finally:
            info["_sentica_applying"] = False

    def _clear(connection: object, *_extra: object) -> None:
        record = _extra[0] if _extra else connection
        raw = getattr(record, "info", None)
        if not isinstance(raw, dict):
            raw = getattr(connection, "info", None)
        if isinstance(raw, dict):
            cast("dict[str, Any]", raw).pop("sentica_workspace", None)

    for async_engine in engines:
        sync_engine = async_engine.sync_engine
        event.listen(sync_engine, "before_cursor_execute", _apply)
        event.listen(sync_engine, "checkin", _clear)
        event.listen(sync_engine, "commit", _clear)
        event.listen(sync_engine, "rollback", _clear)
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
