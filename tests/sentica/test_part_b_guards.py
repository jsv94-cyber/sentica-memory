"""Guards that fail when a new table or a new provider call slips in."""

from __future__ import annotations

import ast
import os
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine.url import make_url

from src.config import settings
from src.db import Base

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# These tables are not workspace-scoped. A table absent from this map must
# have ENABLE, FORCE, and the sentica_workspace policy.
TABLES_WITHOUT_WORKSPACE_RLS: dict[str, str] = {
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

# M3 shrinks this map to the gateway module. A new provider SDK import fails
# until that file is added here with a reason.
ALLOWED_PROVIDER_MODULES: dict[str, str] = {
    "src/embedding_client.py": (
        "Embedding calls use the OpenAI and Gemini SDKs until M3 routes them "
        "through the gateway."
    ),
    "src/llm/backends/anthropic.py": (
        "The Anthropic backend speaks the Anthropic SDK until M3."
    ),
    "src/llm/backends/gemini.py": (
        "The Gemini backend speaks the Google GenAI SDK until M3."
    ),
    "src/llm/backends/openai.py": (
        "The OpenAI backend speaks the OpenAI SDK until M3."
    ),
    "src/llm/errors.py": (
        "Provider exceptions are translated here, including httpx errors the "
        "SDKs raise."
    ),
    "src/llm/registry.py": "The registry constructs provider clients until M3.",
    "src/llm/types.py": "Provider client types are imported for type checking.",
    "src/config.py": (
        "Embedding setup treats api.openai.com as base64-capable until M3 "
        "sends every embedding through the gateway."
    ),
}

_PROVIDER_HOSTS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
)


def _provider_module(name: str) -> bool:
    return (
        name == "openai"
        or name.startswith("openai.")
        or name == "anthropic"
        or name.startswith("anthropic.")
        or name.startswith("google.genai")
        or name.startswith("google.generativeai")
    )


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _is_docstring(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    if not isinstance(parent, ast.Expr):
        return False
    grand = parents.get(parent)
    return isinstance(
        grand, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    )


def _file_uses_provider(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = _parents(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _provider_module(alias.name):
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if _provider_module(node.module):
                return True
            if node.module == "google" and any(
                alias.name == "genai" or alias.name.startswith("genai.")
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else None
            if called is None and isinstance(func, ast.Attribute):
                called = func.attr
            if called in {"import_module", "__import__"} and node.args:
                target = _literal(node.args[0])
                if target is None or _provider_module(target):
                    return True
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and not _is_docstring(node, parents)
            and any(host in node.value for host in _PROVIDER_HOSTS)
        ):
            return True
    return False


def test_provider_sdks_stay_on_the_allow_list() -> None:
    """A new provider SDK import or provider host outside the list fails."""
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        if _file_uses_provider(path):
            found.add(str(path.relative_to(ROOT)))
    assert found == set(ALLOWED_PROVIDER_MODULES)
    for path, reason in ALLOWED_PROVIDER_MODULES.items():
        assert reason.strip()
        assert (ROOT / path).is_file()


def _admin_url() -> Any:
    raw = settings.DB.CONNECTION_URI
    if not raw:
        raise RuntimeError("DB_CONNECTION_URI is required")
    return make_url(raw)


def _sync(url: Any) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def test_every_memory_table_has_rls_or_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new table in the memory schema needs RLS, or an entry in the map."""
    for name, reason in TABLES_WITHOUT_WORKSPACE_RLS.items():
        assert reason.strip(), name
    admin = _admin_url()
    token = uuid.uuid4().hex[:8]
    runtime_role = "m2g" + token
    ddl_role = runtime_role + "_ddl"
    database = "m2grd_" + token
    password = admin.password
    previous_uri = settings.DB.CONNECTION_URI
    try:
        with psycopg.connect(_sync(admin), autocommit=True) as conn:
            if password is None:
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS").format(
                        sql.Identifier(runtime_role)
                    )
                )
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS").format(
                        sql.Identifier(ddl_role)
                    )
                )
            else:
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOBYPASSRLS").format(
                        sql.Identifier(runtime_role), sql.Literal(password)
                    )
                )
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOBYPASSRLS").format(
                        sql.Identifier(ddl_role), sql.Literal(password)
                    )
                )
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(ddl_role)
                )
            )
        created = admin.set(database=database)
        with psycopg.connect(_sync(created), autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE SCHEMA memory AUTHORIZATION {}").format(
                    sql.Identifier(ddl_role)
                )
            )
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        ddl_url = created.set(
            drivername="postgresql+psycopg",
            username=ddl_role,
            password=password,
        ).render_as_string(hide_password=False)
        monkeypatch.setattr(settings.DB, "CONNECTION_URI", ddl_url)
        monkeypatch.setattr(settings.DB, "SCHEMA", "memory")
        monkeypatch.setattr(Base.metadata, "schema", "memory")
        monkeypatch.setenv("DB_CONNECTION_URI", ddl_url)
        monkeypatch.setenv("DB_SCHEMA", "memory")
        os.environ["DB_CONNECTION_URI"] = ddl_url
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", ddl_url)
        command.upgrade(cfg, "head")
        with psycopg.connect(_sync(created), autocommit=True) as conn:
            rows = conn.execute(
                """
                SELECT c.relname,
                    c.relrowsecurity,
                    c.relforcerowsecurity,
                    EXISTS (
                        SELECT 1
                        FROM pg_policy AS p
                        WHERE p.polrelid = c.oid
                            AND p.polname = 'sentica_workspace'
                    ),
                    EXISTS (
                        SELECT 1
                        FROM information_schema.columns AS cols
                        WHERE cols.table_schema = 'memory'
                            AND cols.table_name = c.relname
                            AND cols.column_name = 'workspace_name'
                    )
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'memory' AND c.relkind = 'r'
                ORDER BY c.relname
                """
            ).fetchall()
        catalog = {str(row[0]) for row in rows}
        model_tables = {table.name for table in Base.metadata.tables.values()}
        assert model_tables <= catalog
        assert set(TABLES_WITHOUT_WORKSPACE_RLS) <= catalog
        for row in rows:
            name = str(row[0])
            enabled = bool(row[1])
            forced = bool(row[2])
            has_policy = bool(row[3])
            has_workspace = bool(row[4])
            if name in TABLES_WITHOUT_WORKSPACE_RLS:
                assert not has_workspace, name
                continue
            assert enabled and forced and has_policy, name
            assert has_workspace, name
    finally:
        settings.DB.CONNECTION_URI = previous_uri
        os.environ["DB_CONNECTION_URI"] = previous_uri
        with psycopg.connect(_sync(admin), autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database)
                )
            )
            conn.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(ddl_role))
            )
            conn.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime_role))
            )
