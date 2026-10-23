#!/usr/bin/env python3
"""Read-only wall check for schema memory.

Run it with the Python selection in ``docker/migrate-entrypoint.sh``, as the
runtime role. Stdout is table names and booleans. The script selects counts
only, never row text.

``SENTICA_MODE=production`` connects with the passwordless proxy settings
(``SENTICA_DB_USER`` and the client certificate). A ``_ddl`` user, a
superuser, and ``SENTICA_RLS_PROBE_DSN`` are refused. ``SENTICA_MODE=test``
uses ``SENTICA_RLS_PROBE_DSN`` when it is set. A production image
(``/app/.sentica-production-image``) refuses test mode.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
from psycopg import sql

# Deparsed form of the USING and WITH CHECK clauses in
# migrations/versions/a9c1e3f5b7d2_workspace_row_level_security.py lines 36-47.
_EXPECTED_CLAUSE = (
    "((COALESCE(current_setting('sentica.workspace'::text, true), ''::text) "
    + "<> ''::text) AND (workspace_name = current_setting("
    + "'sentica.workspace'::text, true)))"
)

# Same map and reasons as TABLES_WITHOUT_WORKSPACE_RLS in
# tests/sentica/test_part_b_guards.py (test_every_memory_table_has_rls_or_a_reason).
EXEMPT_TABLES: dict[str, str] = {
    "workspaces": (
        "The workspace id is the name column. The row holds configuration, "
        "not messages or conclusions."
    ),
    "active_queue_sessions": (
        "Columns are the work-unit key and a timestamp. There is no message "
        "or conclusion text."
    ),
    "alembic_version": "The only value is the migration revision id.",
}

_SCHEMA = "memory"
_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_HOST = re.compile(r"[A-Za-z0-9.-]+\Z")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PASSWORD_ENV = (
    "DB_CONNECTION_URI",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "DB_PASSWORD",
    "PGPASSWORD",
)
_CERT_ENV = (
    "SENTICA_DB_USER",
    "SENTICA_DB_PROXY_HOST",
    "SENTICA_DB_PROXY_PORT",
    "SENTICA_DB_NAME",
    "SENTICA_CLIENT_CERT_PATH",
    "SENTICA_CLIENT_KEY_PATH",
    "SENTICA_PROXY_CA_PATH",
)
_PRODUCTION_IMAGE = Path("/app/.sentica-production-image")

_CATALOG = """
SELECT c.relname,
    c.relrowsecurity,
    c.relforcerowsecurity,
    c.relowner = (
        SELECT oid FROM pg_roles WHERE rolname = current_user
    ),
    EXISTS (
        SELECT 1
        FROM information_schema.columns AS cols
        WHERE cols.table_schema = 'memory'
            AND cols.table_name = c.relname
            AND cols.column_name = 'workspace_name'
    ),
    (
        SELECT count(*)
        FROM pg_policy AS p
        WHERE p.polrelid = c.oid AND p.polname = 'sentica_workspace'
    ),
    (
        SELECT pg_get_expr(p.polqual, p.polrelid)
        FROM pg_policy AS p
        WHERE p.polrelid = c.oid AND p.polname = 'sentica_workspace'
    ),
    (
        SELECT pg_get_expr(p.polwithcheck, p.polrelid)
        FROM pg_policy AS p
        WHERE p.polrelid = c.oid AND p.polname = 'sentica_workspace'
    )
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'memory' AND c.relkind = 'r'
ORDER BY c.relname
"""


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _production_dsn() -> str:
    missing = [name for name in _CERT_ENV if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("missing proxy settings")
    user = os.environ["SENTICA_DB_USER"].strip()
    host = os.environ["SENTICA_DB_PROXY_HOST"].strip()
    port = os.environ["SENTICA_DB_PROXY_PORT"].strip()
    name = os.environ["SENTICA_DB_NAME"].strip()
    if (
        _IDENT.fullmatch(user) is None
        or user.endswith("_ddl")
        or _HOST.fullmatch(host) is None
        or not port.isdigit()
        or not 0 < int(port) <= 65535
        or _IDENT.fullmatch(name) is None
    ):
        raise RuntimeError("refusing proxy target")
    return (
        "postgresql://"
        + quote(user, safe="")
        + "@"
        + host
        + ":"
        + port
        + "/"
        + quote(name, safe="")
        + "?sslmode=verify-full&sslrootcert="
        + quote(os.environ["SENTICA_PROXY_CA_PATH"].strip(), safe="")
        + "&sslcert="
        + quote(os.environ["SENTICA_CLIENT_CERT_PATH"].strip(), safe="")
        + "&sslkey="
        + quote(os.environ["SENTICA_CLIENT_KEY_PATH"].strip(), safe="")
    )


def _connect() -> Any:
    mode = os.environ.get("SENTICA_MODE", "").strip()
    if mode not in {"production", "test"}:
        raise RuntimeError("refusing mode")
    if mode == "test" and _PRODUCTION_IMAGE.is_file():
        raise RuntimeError("production image refuses test mode")
    probe_dsn = os.environ.get("SENTICA_RLS_PROBE_DSN", "").strip()
    if mode == "production":
        if probe_dsn:
            raise RuntimeError("refusing probe dsn in production")
        for name in _PASSWORD_ENV:
            if os.environ.get(name, "").strip():
                raise RuntimeError("refusing database password")
        return psycopg.connect(_production_dsn())
    if probe_dsn:
        return psycopg.connect(probe_dsn)
    if all(os.environ.get(name, "").strip() for name in _CERT_ENV):
        return psycopg.connect(_production_dsn())
    raise RuntimeError("refusing connection")


def _run() -> int:
    for reason in EXEMPT_TABLES.values():
        if not reason.strip():
            raise RuntimeError("blank exemption reason")
    with _connect() as conn:
        role = conn.execute(
            """
            SELECT current_user, rolsuper, rolbypassrls
            FROM pg_roles
            WHERE rolname = current_user
            """
        ).fetchone()
        if role is None:
            raise RuntimeError("missing role")
        role_name = str(role[0])
        superuser = bool(role[1])
        bypass = bool(role[2])
        runtime = _IDENT.fullmatch(role_name) is not None and not role_name.endswith(
            "_ddl"
        )
        schema = conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s",
            (_SCHEMA,),
        ).fetchone()
        if schema is None:
            raise RuntimeError("missing schema")
        rows = conn.execute(_CATALOG).fetchall()
        if not rows:
            raise RuntimeError("no tables")
        conn.execute("SELECT set_config('sentica.workspace', '', true)")
        conn.execute("SELECT set_config('row_security', 'on', true)")
        cleared = conn.execute(
            "SELECT coalesce(current_setting('sentica.workspace', true), '')"
        ).fetchone()
        if cleared is None or str(cleared[0]) != "":
            raise RuntimeError("workspace setting stayed set")
        lines: list[str] = []
        ok = runtime and not superuser and not bypass
        owns_any = False
        seen: set[str] = set()
        for row in rows:
            name = str(row[0])
            if _NAME.fullmatch(name) is None:
                raise RuntimeError("unexpected table name")
            seen.add(name)
            enabled = bool(row[1])
            forced = bool(row[2])
            owned = bool(row[3])
            has_workspace = bool(row[4])
            policy_count = int(row[5])
            using = str(row[6] or "")
            check = str(row[7] or "")
            policy = (
                policy_count == 1
                and using == _EXPECTED_CLAUSE
                and check == _EXPECTED_CLAUSE
            )
            counted = conn.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(_SCHEMA),
                    sql.Identifier(name),
                )
            ).fetchone()
            if counted is None:
                raise RuntimeError("missing count")
            unset_zero = int(counted[0]) == 0
            exempt = name in EXEMPT_TABLES
            if owned:
                owns_any = True
                ok = False
            if exempt:
                if has_workspace:
                    ok = False
            elif not (enabled and forced and policy and unset_zero and has_workspace):
                ok = False
            lines.append(
                name
                + " rls="
                + _flag(enabled)
                + " forced="
                + _flag(forced)
                + " policy="
                + _flag(policy)
                + " unset_zero="
                + _flag(unset_zero)
                + " owned="
                + _flag(owned)
                + " exempt="
                + _flag(exempt)
            )
        if not set(EXEMPT_TABLES).issubset(seen):
            raise RuntimeError("missing exempt table")
        if owns_any or superuser or bypass or not runtime:
            ok = False
        lines.append(
            "role nobypassrls="
            + _flag(not bypass)
            + " superuser="
            + _flag(superuser)
            + " owns_table="
            + _flag(owns_any)
            + " runtime="
            + _flag(runtime)
        )
        sys.stdout.write("\n".join(lines) + "\n")
        conn.rollback()
        return 0 if ok else 1


def main() -> int:
    """Run the probe. Unexpected failures exit 1 with no row text."""
    try:
        return _run()
    except Exception:
        sys.stderr.write("probe failed\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
