"""Regression tests for the M2 review fixes."""

from __future__ import annotations

import math
import os
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.exceptions import AuthenticationException
from src.sentica.runtime import SenticaStartupError, db_call_timeout_seconds
from src.sentica.workspace_scope import (
    allowed_from_claims,
    apply_per_workspace,
    pending_counts_function_sql,
    set_workspace,
)

ROOT = Path(__file__).resolve().parents[2]


def _exec(conn: Any, query: str, params: tuple[Any, ...] = ()) -> Any:
    return conn.execute(query, params)


def _sync_dsn(worker_id: str) -> str:
    from sqlalchemy.engine.url import make_url

    run_id = os.environ.get("HONCHO_TEST_RUN_ID", "local")
    suffix = "" if worker_id == "master" else f"_{worker_id}"
    url = make_url(os.environ["DB_CONNECTION_URI"]).set(
        drivername="postgresql",
        database=f"test_db_{run_id}{suffix}",
    )
    return url.render_as_string(hide_password=False)


def test_ambiguous_workspace_claims_are_refused() -> None:
    assert allowed_from_claims("alpha", None) == {"alpha"}
    with pytest.raises(AuthenticationException):
        allowed_from_claims("alpha", ["alpha", ""])
    with pytest.raises(AuthenticationException):
        allowed_from_claims("alpha", ["alpha", 1])
    with pytest.raises(AuthenticationException):
        allowed_from_claims("alpha", ["beta"])
    with pytest.raises(AuthenticationException):
        allowed_from_claims("alpha", [])


def test_pending_counts_function_has_no_content_columns() -> None:
    sql = pending_counts_function_sql("memory")
    assert "SECURITY DEFINER" in sql
    assert "search_path = memory, pg_temp" in sql
    assert "pending_queue" in sql
    assert "pending_embeddings" in sql
    assert "content" not in sql


def test_db_call_timeout_rejects_nan_and_infinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for raw in ("nan", "inf", "-inf"):
        monkeypatch.setenv("SENTICA_DB_CALL_TIMEOUT_SECONDS", raw)
        with pytest.raises(SenticaStartupError):
            db_call_timeout_seconds()
    assert not math.isfinite(float("nan"))


def test_workflow_job_builds_schema_as_the_ddl_role() -> None:
    text_value = (ROOT / ".github" / "workflows" / "unittest.yml").read_text(
        encoding="utf-8"
    )
    assert "test-m2-rls:" in text_value
    assert "alembic upgrade head" in text_value
    assert "tenant_rls_ddl" in text_value
    assert "SENTICA_MODE: production" in text_value
    assert "export SENTICA_MODE=test" not in text_value
    assert (
        "DB_CONNECTION_URI:" not in text_value.split("Run the runtime-role proof", 1)[1]
    )
    assert "tests/sentica/test_m2_" in text_value
    assert "needs: [test-python, test-m1-stack, test-m2-rls]" in text_value


def test_data_migration_updates_each_workspace(worker_id: str) -> None:
    from sqlalchemy import create_engine

    left = "a" + uuid.uuid4().hex[:20]
    right = "b" + uuid.uuid4().hex[:20]
    sync = _sync_dsn(worker_id)
    url = sync.replace("postgresql://", "postgresql+psycopg://", 1)
    with psycopg.connect(sync, autocommit=True) as conn:
        for name, public_id in (
            (left, "l" + uuid.uuid4().hex[:20]),
            (right, "r" + uuid.uuid4().hex[:20]),
        ):
            conn.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, %s)",
                (public_id, name),
            )
            conn.execute(
                "INSERT INTO peers (id, name, workspace_name) VALUES (%s, %s, %s)",
                ("p" + uuid.uuid4().hex[:20], "peer", name),
            )
        schema = conn.execute("SELECT current_schema()").fetchone()
        assert schema is not None
    engine = create_engine(url)
    with engine.begin() as sql_conn:
        apply_per_workspace(
            sql_conn,
            schema[0],
            "UPDATE peers SET internal_metadata = "
            + "jsonb_build_object('seen', current_setting('sentica.workspace', true)) "
            + "WHERE name = 'peer' AND workspace_name = "
            + "current_setting('sentica.workspace', true)",
        )
    with psycopg.connect(sync, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT workspace_name, internal_metadata->>'seen' FROM peers "
            + "WHERE workspace_name IN (%s, %s) ORDER BY workspace_name",
            (left, right),
        ).fetchall()
        assert rows == [(left, left), (right, right)]
        conn.execute(
            "DELETE FROM peers WHERE workspace_name IN (%s, %s)", (left, right)
        )
        conn.execute("DELETE FROM workspaces WHERE name IN (%s, %s)", (left, right))


def test_queue_workspace_name_is_required(worker_id: str) -> None:
    with (
        psycopg.connect(_sync_dsn(worker_id)) as conn,
        pytest.raises(psycopg.Error),
    ):
        conn.execute(
            "INSERT INTO queue (work_unit_key, task_type, payload) "
            + "VALUES ('missing-workspace', 'representation', '{}')"
        )


async def test_workspace_setting_is_not_inherited(
    worker_id: str, db_session: object
) -> None:
    """After a transaction bound to A commits, the next one sees no rows."""
    del db_session
    import src.sentica.workspace_scope as scope
    from src.sentica.workspace_scope import reset_workspace, workspace_policy_sql

    left = "a" + uuid.uuid4().hex[:20]
    right = "b" + uuid.uuid4().hex[:20]
    runtime = "inh" + uuid.uuid4().hex[:8]
    sync = _sync_dsn(worker_id)
    with psycopg.connect(sync, autocommit=True) as conn:
        _exec(conn, "CREATE ROLE " + runtime + " NOLOGIN NOBYPASSRLS")
        schema = conn.execute("SELECT current_schema()").fetchone()
        assert schema is not None
        _exec(conn, workspace_policy_sql(schema[0], "peers"))
        _exec(conn, 'GRANT SELECT ON "' + schema[0] + '"."peers" TO ' + runtime)
        for name in (left, right):
            conn.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, %s)",
                ("w" + uuid.uuid4().hex[:20], name),
            )
            conn.execute(
                "INSERT INTO peers (id, name, workspace_name) VALUES (%s, %s, %s)",
                ("p" + uuid.uuid4().hex[:20], name, name),
            )
    url = sync.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(url)
    scope._installed = False  # pyright: ignore[reportPrivateUsage]
    scope.install_workspace_scope(engine)
    try:
        bound = set_workspace(left)
        async with engine.connect() as conn:
            await conn.execute(text("SET ROLE " + runtime))
            count = await conn.scalar(text("SELECT count(*) FROM peers"))
            await conn.commit()
            reset_workspace(bound)
            later = await conn.scalar(text("SELECT count(*) FROM peers"))
            await conn.commit()
            assert count == 1
            assert later == 0
            held = set_workspace(left)
            await conn.execute(text("SELECT 1"))
            reset_workspace(held)
            with pytest.raises(RuntimeError, match="open transaction"):
                changed = set_workspace(right)
                try:
                    await conn.execute(text("SELECT 1"))
                finally:
                    reset_workspace(changed)
            await conn.rollback()
            again = set_workspace(left)
            await conn.execute(text("SELECT 1"))
            nested = await conn.begin_nested()
            await nested.rollback()
            reset_workspace(again)
            switched = set_workspace(right)
            await conn.execute(text("SELECT 1"))
            reset_workspace(switched)
            await conn.rollback()
    finally:
        await engine.dispose()
        with psycopg.connect(sync, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM peers WHERE workspace_name IN (%s, %s)", (left, right)
            )
            conn.execute("DELETE FROM workspaces WHERE name IN (%s, %s)", (left, right))
            _exec(conn, 'REVOKE SELECT ON "' + schema[0] + '"."peers" FROM ' + runtime)
            _exec(conn, "DROP ROLE " + runtime)


def test_revision_check_refuses_a_stale_schema(worker_id: str) -> None:
    from sqlalchemy import create_engine

    from src.sentica.schema_revision import assert_revision, head_revision

    sync = _sync_dsn(worker_id)
    url = sync.replace("postgresql://", "postgresql+psycopg://", 1)
    with psycopg.connect(sync, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) "
            + "NOT NULL)",
            (),
        )
        conn.execute("DELETE FROM alembic_version")
        conn.execute(
            "INSERT INTO alembic_version (version_num) VALUES ('not-the-head')"
        )
    engine = create_engine(url)
    with engine.connect() as conn:
        with pytest.raises(SenticaStartupError, match="schema revision is behind"):
            assert_revision(conn, "public")
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
            {"head": head_revision()},
        )
        conn.commit()
        assert_revision(conn, "public")
    engine.dispose()
    with psycopg.connect(sync, autocommit=True) as conn:
        conn.execute("DELETE FROM alembic_version")


def test_pagination_cache_key_includes_the_workspace() -> None:
    source = (ROOT / "src" / "utils" / "pagination.py").read_text(encoding="utf-8")
    assert "current_workspace()" in source
    from src.sentica.workspace_scope import current_workspace, reset_workspace

    left = set_workspace("left-workspace")
    try:
        assert current_workspace() == "left-workspace"
        right = set_workspace("right-workspace")
        try:
            assert current_workspace() == "right-workspace"
            assert current_workspace() != "left-workspace"
        finally:
            reset_workspace(right)
    finally:
        reset_workspace(left)
