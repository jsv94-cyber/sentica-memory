"""Merged migrations keep the SQL they produced, and later changes are new files."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from nanoid import generate as generate_nanoid
from psycopg import sql
from sqlalchemy import event
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import settings
from tests.sentica import cdb74439_sql
from tests.sentica.cdb74439_sql_sha256 import FROZEN_SQL_SHA256

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "migrations" / "versions"

# settings.DB.SCHEMA is the schema name constant. src.config does not generate SQL.
_SCHEMA_IMPORT = ("src.config", "settings")
_SCHEMA_IMPORT_REASON = (
    "settings.DB.SCHEMA is the schema name. src.config does not generate SQL. "
    "No other attribute of settings is allowed, and migrations/utils.py is "
    "checked so a version file cannot reach src through it."
)
_DYNAMIC_IMPORTS = frozenset({"import_module", "__import__"})


def _load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load " + path.name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _check_migration_file(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = _parents(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if not node.module.startswith("src"):
                continue
            names = [alias.name for alias in node.names]
            if (
                node.module == _SCHEMA_IMPORT[0]
                and names == [_SCHEMA_IMPORT[1]]
                and all(alias.asname is None for alias in node.names)
            ):
                continue
            raise AssertionError(
                path.name + " imports " + node.module + " " + repr(names)
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "src" or alias.name.startswith("src."):
                    raise AssertionError(path.name + " imports " + alias.name)
        if isinstance(node, ast.Name) and node.id == "settings":
            db = parents.get(node)
            schema = parents.get(db) if db is not None else None
            if not (
                isinstance(db, ast.Attribute)
                and db.attr == "DB"
                and isinstance(schema, ast.Attribute)
                and schema.attr == "SCHEMA"
                and not isinstance(parents.get(schema), ast.Attribute)
            ):
                raise AssertionError(path.name + " uses settings beyond DB.SCHEMA")
        if isinstance(node, ast.Call) and _call_name(node) in _DYNAMIC_IMPORTS:
            target = _literal(node.args[0]) if node.args else None
            if target is None or target == "src" or target.startswith("src."):
                raise AssertionError(path.name + " dynamically imports src")


def test_migrations_do_not_import_sql_from_src() -> None:
    """A version file may read the schema name and nothing else from src."""
    assert "schema name" in _SCHEMA_IMPORT_REASON
    assert "migrations/utils.py" in _SCHEMA_IMPORT_REASON
    for path in sorted(VERSIONS.glob("*.py")):
        _check_migration_file(path)
    _check_migration_file(ROOT / "migrations" / "utils.py")


def test_frozen_sql_matches_main_cdb74439(monkeypatch: pytest.MonkeyPatch) -> None:
    """The merged revisions render the SQL those functions produced at that commit."""
    monkeypatch.setattr(settings.DREAM, "ENABLED", True)
    monkeypatch.setattr(settings.DREAM, "ENABLED_TYPES", ["omni"])
    monkeypatch.setattr(settings.DREAM, "DOCUMENT_THRESHOLD", 50)
    monkeypatch.setattr(settings.DREAM, "IDLE_TIMEOUT_MINUTES", 60)
    monkeypatch.setattr(settings.DREAM, "MIN_HOURS_BETWEEN_DREAMS", 8)
    policy = _load(VERSIONS / "a9c1e3f5b7d2_workspace_row_level_security.py")
    counts = _load(VERSIONS / "c4e8b1a2d6f0_runtime_role_and_pending_counts.py")
    pinned: list[tuple[str, str, str]] = []
    for schema, table in (("memory", "messages"), ("public", "queue")):
        migrated = policy.workspace_policy_sql(schema, table)
        historical = cdb74439_sql.workspace_policy_sql(schema, table)
        assert migrated == historical
        pinned.append((f"workspace_policy:{schema}:{table}", migrated, historical))
    for schema in ("memory", "public"):
        migrated_queue = counts.pending_queue_counts_function_sql(schema)
        historical_queue = cdb74439_sql.pending_queue_counts_function_sql(schema)
        assert migrated_queue == historical_queue
        pinned.append((f"pending_queue:{schema}", migrated_queue, historical_queue))
        migrated_counts = counts.pending_counts_function_sql(schema)
        historical_counts = cdb74439_sql.pending_counts_function_sql(schema)
        assert migrated_counts == historical_counts
        pinned.append((f"pending_counts:{schema}", migrated_counts, historical_counts))
        migrated_ddl = counts.ddl_select_policy_sql(
            schema, "documents", "tenant_rls_ddl"
        )
        historical_ddl = cdb74439_sql.ddl_select_policy_sql(
            schema, "documents", "tenant_rls_ddl"
        )
        assert migrated_ddl == historical_ddl
        pinned.append((f"ddl_select:{schema}", migrated_ddl, historical_ddl))
    assert set(FROZEN_SQL_SHA256) == {key for key, _migrated, _historical in pinned}
    for key, migrated, historical in pinned:
        digest = FROZEN_SQL_SHA256[key]
        assert hashlib.sha256(migrated.encode()).hexdigest() == digest
        assert hashlib.sha256(historical.encode()).hexdigest() == digest
    frozen = counts.pending_counts_function_sql("memory")
    monkeypatch.setattr(settings.DREAM, "ENABLED", False)
    monkeypatch.setattr(settings.DREAM, "DOCUMENT_THRESHOLD", 1)
    assert counts.pending_counts_function_sql("memory") == frozen
    assert ">= 50" in frozen
    assert "0::bigint" in cdb74439_sql.pending_counts_function_sql("memory")


def test_dreams_migration_matches_the_runtime_superset() -> None:
    from src.sentica.workspace_scope import pending_counts_function_sql

    migration = _load(VERSIONS / "e6b2d8f04a17_dreams_due_superset.py")
    rendered = migration.pending_counts_function_sql("memory")
    assert rendered == pending_counts_function_sql("memory")
    assert "last_dream_document_count" in rendered
    assert "split_part" not in rendered
    assert "work_unit_key" not in rendered
    text = (VERSIONS / "e6b2d8f04a17_dreams_due_superset.py").read_text(
        encoding="utf-8"
    )
    assert "REVOKE ALL ON FUNCTION" in text
    assert "GRANT EXECUTE ON FUNCTION" in text
    assert "from src.sentica" not in text


def test_rls_job_does_not_grant_function_execute() -> None:
    """The runtime-role job must get EXECUTE from the migrations, not the workflow."""
    workflow = (ROOT / ".github" / "workflows" / "unittest.yml").read_text(
        encoding="utf-8"
    )
    job = workflow.split("test-m2-rls:", 1)[1].split("\n  test-status:", 1)[0]
    assert "GRANT EXECUTE ON FUNCTION" not in job
    assert "alembic upgrade head" in job


def test_dreams_migration_names_a_role_that_does_not_end_in_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role without the _ddl suffix is refused with that reason."""
    migration = _load(VERSIONS / "e6b2d8f04a17_dreams_due_superset.py")

    class _Result:
        def scalar(self) -> str:
            return "tenant_rls"

    class _Bind:
        def execute(self, *_args: object, **_kwargs: object) -> _Result:
            return _Result()

    monkeypatch.setattr(migration.op, "get_bind", lambda: _Bind())
    with pytest.raises(RuntimeError, match="must end in _ddl"):
        migration.upgrade()


def _admin_url() -> Any:
    raw = settings.DB.CONNECTION_URI
    if not raw:
        raise RuntimeError("DB_CONNECTION_URI is required")
    return make_url(raw)


def _sync(url: Any) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _dreams_due(url: str, schema: str, workspace: str) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        due = conn.execute(
            sql.SQL(
                "SELECT dreams_due FROM {}.sentica_pending_counts() WHERE workspace_name = %s"
            ).format(sql.Identifier(schema)),
            (workspace,),
        ).fetchone()
    assert due is not None
    return int(due[0])


async def test_upgrade_from_merged_head_ignores_dream_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database at cdb74439, upgraded as the ddl role, counts dreams without settings."""
    from src.backlog import DeriverMetricsPoller
    from src.sentica.workspace_scope import current_workspace

    admin = _admin_url()
    token = uuid.uuid4().hex[:8]
    runtime_role = "m2u" + token
    ddl_role = runtime_role + "_ddl"
    database = "m2upg_" + token
    password = admin.password
    schema = settings.DB.SCHEMA
    previous_uri = settings.DB.CONNECTION_URI
    engine: AsyncEngine | None = None
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
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        ddl_url = created.set(
            drivername="postgresql+psycopg",
            username=ddl_role,
            password=password,
        ).render_as_string(hide_password=False)
        settings.DB.CONNECTION_URI = ddl_url
        os.environ["DB_CONNECTION_URI"] = ddl_url
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", ddl_url)
        command.upgrade(cfg, "c4e8b1a2d6f0")
        name = "w" + token
        created_at = datetime.now(UTC) - timedelta(days=2)
        recent = datetime.now(UTC)
        with psycopg.connect(
            _sync(created.set(username=ddl_role, password=password)),
            autocommit=True,
        ) as conn:
            conn.execute("SELECT set_config('sentica.workspace', %s, false)", (name,))
            conn.execute(
                sql.SQL("INSERT INTO {}.workspaces (id, name) VALUES (%s, %s)").format(
                    sql.Identifier(schema)
                ),
                (generate_nanoid(), name),
            )
            for peer in ("p1", "p2"):
                conn.execute(
                    sql.SQL(
                        "INSERT INTO {}.peers (id, name, workspace_name) VALUES (%s, %s, %s)"
                    ).format(sql.Identifier(schema)),
                    (generate_nanoid(), peer, name),
                )
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.sessions (id, name, workspace_name) VALUES (%s, %s, %s)"
                ).format(sql.Identifier(schema)),
                (generate_nanoid(), "s1", name),
            )
            for peer in ("p1", "p2"):
                conn.execute(
                    sql.SQL(
                        "INSERT INTO {}.collections (id, observer, observed, workspace_name) VALUES (%s, %s, %s, %s)"
                    ).format(sql.Identifier(schema)),
                    (generate_nanoid(), peer, peer, name),
                )
            for peer, stamp in (("p1", created_at), ("p2", recent)):
                for _ in range(2):
                    conn.execute(
                        sql.SQL(
                            "INSERT INTO {}.documents (id, content, observer, observed, workspace_name, session_name, level, created_at) VALUES (%s, %s, %s, %s, %s, 's1', 'explicit', %s)"
                        ).format(sql.Identifier(schema)),
                        (generate_nanoid(), "dream note", peer, peer, name, stamp),
                    )
            due = conn.execute(
                sql.SQL(
                    "SELECT dreams_due FROM {}.sentica_pending_counts() WHERE workspace_name = %s"
                ).format(sql.Identifier(schema)),
                (name,),
            ).fetchone()
        assert due is not None and int(due[0]) == 0
        monkeypatch.setattr(settings.DREAM, "ENABLED", False)
        monkeypatch.setattr(settings.DREAM, "DOCUMENT_THRESHOLD", 50)
        command.upgrade(cfg, "head")
        with psycopg.connect(
            _sync(created.set(username=ddl_role, password=password)),
            autocommit=True,
        ) as conn:
            due = conn.execute(
                sql.SQL(
                    "SELECT dreams_due FROM {}.sentica_pending_counts() WHERE workspace_name = %s"
                ).format(sql.Identifier(schema)),
                (name,),
            ).fetchone()
        assert due is not None and int(due[0]) == 2, due
        runtime_sync = _sync(created.set(username=runtime_role, password=password))
        ddl_sync = _sync(created.set(username=ddl_role, password=password))
        assert _dreams_due(runtime_sync, schema, name) == 2
        command.downgrade(cfg, "c4e8b1a2d6f0")
        assert _dreams_due(runtime_sync, schema, name) == 0
        assert _dreams_due(ddl_sync, schema, name) == 0
        command.upgrade(cfg, "head")
        monkeypatch.setattr(settings.DREAM, "ENABLED", True)
        monkeypatch.setattr(settings.DREAM, "DOCUMENT_THRESHOLD", 1)
        monkeypatch.setattr(settings.DREAM, "ENABLED_TYPES", ["omni"])
        monkeypatch.setattr(settings.DERIVER, "SCHEDULER", "api")
        engine = create_async_engine(ddl_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        def _apply(
            connection: Any,
            cursor: Any,
            _statement: object,
            _parameters: object,
            _context: object,
            _executemany: object,
        ) -> None:
            info_raw = connection.info
            if not isinstance(info_raw, dict):
                return
            info = cast("dict[str, Any]", info_raw)
            if info.get("_applying"):
                return
            info["_applying"] = True
            try:
                cursor.execute(
                    "SELECT set_config('sentica.workspace', %s, true)",
                    (current_workspace() or "",),
                )
            finally:
                info["_applying"] = False

        event.listen(engine.sync_engine, "before_cursor_execute", _apply)

        @asynccontextmanager
        async def _tracked(
            _operation: str | None = None, *, read_only: bool = False
        ) -> AsyncGenerator[AsyncSession]:
            del read_only
            session = sessions()
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        import importlib
        import sys

        import src.backlog as backlog

        package = cast(Any, sys.modules["src.deriver"])
        exported = package.enqueue
        enqueue_mod = importlib.import_module("src.deriver.enqueue")
        package.enqueue = exported
        monkeypatch.setattr(backlog, "tracked_db", _tracked)
        monkeypatch.setattr(enqueue_mod, "tracked_db", _tracked)
        poller = DeriverMetricsPoller()
        await poller.refresh()
        assert poller.snapshot.dreams_due >= 1
        with psycopg.connect(
            _sync(created.set(username=ddl_role, password=password))
        ) as conn:
            conn.execute("BEGIN")
            conn.execute("SELECT set_config('sentica.workspace', %s, true)", (name,))
            rows = conn.execute(
                sql.SQL(
                    "SELECT payload::text FROM {}.queue WHERE workspace_name = %s AND task_type = 'dream'"
                ).format(sql.Identifier(schema)),
                (name,),
            ).fetchall()
            conn.execute("COMMIT")
        assert len(rows) == 1
        payload = rows[0][0]
        assert isinstance(payload, str)
        assert "api_poll" in payload
        assert '"observed": "p1"' in payload or '"observed":"p1"' in payload
    finally:
        if engine is not None:
            await engine.dispose()
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
