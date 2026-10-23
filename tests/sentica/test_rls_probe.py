"""The memory wall probe fails closed, and it stays out of the RLS job."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine.url import make_url

from src.config import settings
from src.db import Base
from tests.sentica.rls_job import EXPECTED_M2_NODE_IDS
from tests.sentica.test_part_b_guards import TABLES_WITHOUT_WORKSPACE_RLS

ROOT = Path(__file__).resolve().parents[2]
_PROBE = ROOT / "scripts" / "sentica_rls_probe.py"
_ENTRY_PYTHON = (
    "if [ -x /app/.venv/bin/python ]; then\n"
    "    python_bin=/app/.venv/bin/python\n"
    "else\n"
    '    python_bin="uv run python"\n'
    "fi\n"
)
_TABLE = re.compile(
    r"(?P<table>[a-z_][a-z0-9_]*)"
    + r" rls=(?P<rls>true|false)"
    + r" forced=(?P<forced>true|false)"
    + r" policy=(?P<policy>true|false)"
    + r" unset_zero=(?P<unset_zero>true|false)"
    + r" owned=(?P<owned>true|false)"
    + r" exempt=(?P<exempt>true|false)\Z"
)
_ROLE = re.compile(
    r"role nobypassrls=(?P<nobypassrls>true|false)"
    + r" superuser=(?P<superuser>true|false)"
    + r" owns_table=(?P<owns_table>true|false)"
    + r" runtime=(?P<runtime>true|false)\Z"
)
_FIELDS = ("rls", "forced", "policy", "unset_zero", "owned", "exempt")
_ROLE_FIELDS = ("nobypassrls", "superuser", "owns_table", "runtime")


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load " + path.name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_probe() -> Any:
    return _load(_PROBE, "sentica_rls_probe")


def _policy_sql(schema: str, table: str) -> str:
    path = (
        ROOT
        / "migrations"
        / "versions"
        / "a9c1e3f5b7d2_workspace_row_level_security.py"
    )
    module = _load(path, "workspace_rls_migration")
    sql_text = module.workspace_policy_sql(schema, table)
    if not isinstance(sql_text, str) or "sentica_workspace" not in sql_text:
        raise RuntimeError("workspace policy sql is missing")
    return sql_text


def _sync(url: Any) -> str:
    rendered = url.set(drivername="postgresql").render_as_string(hide_password=False)
    if not rendered.startswith("postgresql://"):
        raise RuntimeError("test database url is not postgresql")
    return rendered


def _admin_url() -> Any:
    raw = settings.DB.CONNECTION_URI
    if not raw:
        raise RuntimeError("DB_CONNECTION_URI is required")
    return make_url(raw)


def _exec(dsn: str, statement: str) -> None:
    query = cast(LiteralString, statement)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(query)


def _exec_sql(dsn: str, statement: sql.Composed) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(statement)


def _parse(stdout: str) -> tuple[dict[str, dict[str, bool]], dict[str, bool]]:
    lines = stdout.splitlines()
    if not lines or stdout != "\n".join(lines) + "\n":
        raise AssertionError("probe stdout is not one record per line")
    role_match = _ROLE.fullmatch(lines[-1])
    if role_match is None:
        raise AssertionError("probe role line is not booleans")
    tables: dict[str, dict[str, bool]] = {}
    for line in lines[:-1]:
        match = _TABLE.fullmatch(line)
        if match is None:
            raise AssertionError("probe table line is not a name and booleans")
        name = match.group("table")
        if name in tables:
            raise AssertionError("probe repeated a table")
        tables[name] = {key: match.group(key) == "true" for key in _FIELDS}
    role = {key: role_match.group(key) == "true" for key in _ROLE_FIELDS}
    return tables, role


def _run_probe(dsn: str) -> subprocess.CompletedProcess[str]:
    entry = (ROOT / "docker" / "migrate-entrypoint.sh").read_text(encoding="utf-8")
    if _ENTRY_PYTHON not in entry:
        raise AssertionError("migrate entrypoint python selection changed")
    env = os.environ.copy()
    env["SENTICA_MODE"] = "test"
    env["SENTICA_RLS_PROBE_DSN"] = dsn
    env.pop("PGPASSWORD", None)
    return subprocess.run(
        [
            "sh",
            "-eu",
            "-c",
            _ENTRY_PYTHON + "$python_bin scripts/sentica_rls_probe.py\n",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_clean(
    result: subprocess.CompletedProcess[str], secrets: tuple[str, ...]
) -> None:
    for secret in secrets:
        assert secret not in result.stdout
        assert secret not in result.stderr
    assert "content" not in result.stdout
    assert "content" not in result.stderr


def _passing(
    dsn: str, secrets: tuple[str, ...]
) -> tuple[dict[str, dict[str, bool]], dict[str, bool]]:
    result = _run_probe(dsn)
    _assert_clean(result, secrets)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    tables, role = _parse(result.stdout)
    assert role == {
        "nobypassrls": True,
        "superuser": False,
        "owns_table": False,
        "runtime": True,
    }
    assert set(TABLES_WITHOUT_WORKSPACE_RLS).issubset(tables)
    for name, flags in tables.items():
        assert flags["owned"] is False
        if name in TABLES_WITHOUT_WORKSPACE_RLS:
            assert flags["exempt"] is True
            continue
        assert flags["exempt"] is False
        assert flags["rls"] is True
        assert flags["forced"] is True
        assert flags["policy"] is True
        assert flags["unset_zero"] is True
    assert tables["peers"]["unset_zero"] is True
    return tables, role


def _failing(
    dsn: str, secrets: tuple[str, ...]
) -> tuple[dict[str, dict[str, bool]], dict[str, bool]]:
    result = _run_probe(dsn)
    _assert_clean(result, secrets)
    assert result.returncode != 0
    assert result.stderr == ""
    return _parse(result.stdout)


def test_rls_probe_is_outside_the_runtime_role_job() -> None:
    """The unit job collects this file. The pinned RLS node set does not."""
    assert _PROBE.is_file()
    assert not any("test_rls_probe" in node for node in EXPECTED_M2_NODE_IDS)
    module = _load_probe()
    assert module.EXEMPT_TABLES == TABLES_WITHOUT_WORKSPACE_RLS
    source = _PROBE.read_text(encoding="utf-8")
    assert "SELECT *" not in source
    assert "content" not in source


def test_rls_probe_fails_when_the_wall_is_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passing run, then each mutation, then a passing run again.

    The planted peer is visible to the migration owner and hidden from the
    runtime role while the wall is intact. Dropping the policy, turning
    force off, and granting BYPASSRLS each make the probe exit non-zero.
    """
    module = _load_probe()
    assert module.EXEMPT_TABLES == TABLES_WITHOUT_WORKSPACE_RLS
    restore_policy = _policy_sql("memory", "peers")
    entry = (ROOT / "docker" / "migrate-entrypoint.sh").read_text(encoding="utf-8")
    assert _ENTRY_PYTHON in entry
    admin = _admin_url()
    token = uuid.uuid4().hex
    runtime_role = "r6p" + token[:8]
    ddl_role = runtime_role + "_ddl"
    database = "r6prd_" + token[:8]
    workspace_id = token[:21]
    peer_id = token[11:32]
    secrets = (workspace_id, peer_id)
    password = admin.password
    previous_uri = settings.DB.CONNECTION_URI
    previous_schema = settings.DB.SCHEMA
    previous_metadata = Base.metadata.schema
    created_db = False
    try:
        with psycopg.connect(_sync(admin), autocommit=True) as conn:
            if password is None:
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS").format(
                        sql.Identifier(runtime_role)
                    )
                )
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS").format(
                        sql.Identifier(ddl_role)
                    )
                )
            else:
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS"
                    ).format(sql.Identifier(runtime_role), sql.Literal(password))
                )
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS"
                    ).format(sql.Identifier(ddl_role), sql.Literal(password))
                )
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(ddl_role)
                )
            )
            created_db = True
        created = admin.set(database=database)
        admin_dsn = _sync(created)
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
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
        os.environ["DB_SCHEMA"] = "memory"
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", ddl_url)
        command.upgrade(cfg, "head")
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO memory.workspaces (id, name) VALUES (%s, %s)",
                (workspace_id, workspace_id),
            )
            conn.execute(
                "INSERT INTO memory.peers (id, name, workspace_name) VALUES (%s, %s, %s)",
                (peer_id, peer_id, workspace_id),
            )
            planted = conn.execute("SELECT count(*) FROM memory.peers").fetchone()
        assert planted is not None and int(planted[0]) == 1
        runtime_dsn = _sync(created.set(username=runtime_role, password=password))
        _passing(runtime_dsn, secrets)

        _exec(admin_dsn, "DROP POLICY sentica_workspace ON memory.peers")
        dropped, _role = _failing(runtime_dsn, secrets)
        assert dropped["peers"]["policy"] is False
        assert dropped["peers"]["rls"] is True
        assert dropped["peers"]["forced"] is True
        assert dropped["messages"]["policy"] is True
        for statement in restore_policy.split(";"):
            piece = statement.strip()
            if piece:
                _exec(admin_dsn, piece)
        _passing(runtime_dsn, secrets)

        _exec(admin_dsn, "ALTER TABLE memory.peers NO FORCE ROW LEVEL SECURITY")
        unforced, _role = _failing(runtime_dsn, secrets)
        assert unforced["peers"]["forced"] is False
        assert unforced["peers"]["rls"] is True
        assert unforced["peers"]["policy"] is True
        assert unforced["peers"]["unset_zero"] is True
        _exec(admin_dsn, "ALTER TABLE memory.peers FORCE ROW LEVEL SECURITY")
        _passing(runtime_dsn, secrets)

        _exec_sql(
            admin_dsn,
            sql.SQL("ALTER ROLE {} BYPASSRLS").format(sql.Identifier(runtime_role)),
        )
        bypassed, role = _failing(runtime_dsn, secrets)
        assert role["nobypassrls"] is False
        assert role["superuser"] is False
        assert bypassed["peers"]["unset_zero"] is False
        assert bypassed["peers"]["policy"] is True
        _exec_sql(
            admin_dsn,
            sql.SQL("ALTER ROLE {} NOBYPASSRLS").format(sql.Identifier(runtime_role)),
        )
        _passing(runtime_dsn, secrets)
    finally:
        settings.DB.CONNECTION_URI = previous_uri
        settings.DB.SCHEMA = previous_schema
        Base.metadata.schema = previous_metadata
        os.environ["DB_CONNECTION_URI"] = previous_uri
        if previous_schema:
            os.environ["DB_SCHEMA"] = previous_schema
        else:
            os.environ.pop("DB_SCHEMA", None)
        with psycopg.connect(_sync(admin), autocommit=True) as conn:
            if created_db:
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
