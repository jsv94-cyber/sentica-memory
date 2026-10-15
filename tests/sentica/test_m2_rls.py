"""Runtime-role proof. The CI job migrates as tenant_rls_ddl and connects as tenant_rls."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.sentica.tokens import verify_sentica_token
from tests.sentica.rls_job import EXPECTED_M2_NODE_IDS, ddl_dsn, rls_job, runtime_dsn

ROOT = Path(__file__).resolve().parents[2]


def _exec(conn: Any, statement: str, params: tuple[Any, ...] = ()) -> Any:
    return conn.execute(statement, params)


def test_signed_token_names_its_workspaces(tmp_path: object) -> None:
    import time
    from pathlib import Path

    import jwt

    from src.sentica.runtime import TOKEN_AUDIENCE, SenticaInstance

    root = tmp_path if isinstance(tmp_path, Path) else Path(str(tmp_path))
    private = ed25519.Ed25519PrivateKey.generate()
    public_path = root / "token.pub"
    public_bytes = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_path.write_bytes(public_bytes)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "sentica-rls-issuer",
            "aud": TOKEN_AUDIENCE,
            "iat": now,
            "exp": now + 60,
            "request_id": "rls-proof",
            "tenant_id": "tenant-rls",
            "w": "left",
            "workspaces": ["left", "right"],
        },
        private,
        algorithm="EdDSA",
    )
    instance = SenticaInstance(
        tenant_id="tenant-rls",
        proxy_host="localhost",
        proxy_port=5432,
        db_user="tenant_rls",
        db_name="tenant_rls",
        cert_path=public_path,
        key_path=public_path,
        ca_path=public_path,
        token_public_key_path=public_path,
        token_public_key=private.public_key(),
        token_issuer="sentica-rls-issuer",
        bind_host="127.0.0.1",
    )
    params = verify_sentica_token(token, instance)
    assert params.workspaces == ["left", "right"]


def test_rls_job_collects_every_m2_file(request: pytest.FixtureRequest) -> None:
    """The job's collected count covers every test function in test_m2_*.py."""
    if rls_job():
        collected = {
            item.nodeid
            for item in request.session.items
            if "tests/sentica/test_m2_" in item.nodeid
        }
        assert collected == EXPECTED_M2_NODE_IDS
        return
    pinned_names = {
        nodeid.split("::", 1)[1].split("[", 1)[0] for nodeid in EXPECTED_M2_NODE_IDS
    }
    defined_names: set[str] = set()
    for path in sorted((ROOT / "tests" / "sentica").glob("test_m2_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                node.name.startswith("test_")
            ):
                defined_names.add(node.name)
    assert defined_names == pinned_names
    workflow = (ROOT / ".github" / "workflows" / "unittest.yml").read_text(
        encoding="utf-8"
    )
    proof = workflow.split("Run the runtime-role proof", 1)[1]
    for name in (
        "test_m2_rls.py",
        "test_m2_fix.py",
        "test_m2_partitions.py",
        "test_m2_carryover.py",
    ):
        assert name in proof
    assert "test-m2-rls collected" in (ROOT / "tests" / "conftest.py").read_text(
        encoding="utf-8"
    )


async def test_app_engine_is_the_runtime_role() -> None:
    """current_user on the app engine is the runtime role, not a superuser."""
    from sqlalchemy import text

    from src.db import engine

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls "
                    + "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    assert row[0] == "tenant_rls"
    assert row[1] is False
    assert row[2] is False


def test_runtime_role_lockdown() -> None:
    dsn = runtime_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn:
        who = _exec(
            conn,
            "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user",
        ).fetchone()
        assert who is not None
        assert who[0] == "tenant_rls"
        assert who[1] is False
        assert who[2] is False
        member = _exec(
            conn, "SELECT pg_has_role(current_user, 'tenant_rls_ddl', 'USAGE')"
        ).fetchone()
        assert member == (False,)
        for statement in (
            "ALTER TABLE memory.messages NO FORCE ROW LEVEL SECURITY",
            "DROP POLICY sentica_workspace ON memory.messages",
            "CREATE TABLE memory.should_fail (id int)",
        ):
            with pytest.raises(psycopg.Error) as denied:
                _exec(conn, statement)
            assert denied.value.sqlstate == "42501"
        version = _exec(
            conn, "SELECT version_num FROM memory.alembic_version"
        ).fetchone()
        assert version is not None
        expected_columns = {
            "sentica_pending_counts": [
                "workspace_name",
                "pending_queue",
                "pending_embeddings",
                "pending_backfills",
                "soft_deleted_documents",
                "dreams_due",
            ],
            "sentica_pending_queue_counts": [
                "workspace_name",
                "pending_queue",
            ],
        }
        for function_name, expected in expected_columns.items():
            description = _exec(
                conn, "SELECT * FROM memory." + function_name + "() LIMIT 0"
            )
            columns = description.description
            assert columns is not None
            assert [col.name for col in columns] == expected
            proc = _exec(
                conn,
                """
                SELECT p.pronargs, p.proconfig
                FROM pg_proc AS p
                JOIN pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'memory' AND p.proname = %s
                """,
                (function_name,),
            ).fetchone()
            assert proc is not None
            assert proc[0] == 0
            assert proc[1] is not None
            assert any(
                item.startswith("search_path=") and "pg_temp" in item
                for item in proc[1]
            )
            allowed = _exec(
                conn,
                "SELECT has_function_privilege(current_user, %s::regprocedure, "
                + "'EXECUTE')",
                ("memory." + function_name + "()",),
            ).fetchone()
            assert allowed == (True,)
        left = "a" + uuid.uuid4().hex[:20]
        right = "b" + uuid.uuid4().hex[:20]
        for name in (left, right):
            _exec(
                conn,
                "INSERT INTO memory.workspaces (id, name) VALUES (%s, %s)",
                (name, name),
            )
        _exec(conn, "BEGIN")
        _exec(conn, "SELECT set_config('sentica.workspace', %s, true)", (left,))
        _exec(
            conn,
            "INSERT INTO memory.peers (id, name, workspace_name) VALUES (%s, %s, %s)",
            ("p" + uuid.uuid4().hex[:20], "peer", left),
        )
        _exec(conn, "COMMIT")
        _exec(conn, "BEGIN")
        _exec(conn, "SELECT set_config('sentica.workspace', %s, true)", (left,))
        with pytest.raises(psycopg.Error) as cross:
            _exec(
                conn,
                "INSERT INTO memory.peers (id, name, workspace_name) "
                + "VALUES (%s, %s, %s)",
                ("q" + uuid.uuid4().hex[:20], "peer", right),
            )
        assert cross.value.sqlstate == "42501"
        _exec(conn, "ROLLBACK")
        _exec(conn, "BEGIN")
        _exec(conn, "SELECT set_config('sentica.workspace', '', true)")
        empty = _exec(conn, "SELECT count(*) FROM memory.peers").fetchone()
        _exec(conn, "COMMIT")
        assert empty is not None and empty[0] == 0
        _exec(conn, "BEGIN")
        _exec(conn, "SELECT set_config('sentica.workspace', %s, true)", (left,))
        visible = _exec(conn, "SELECT workspace_name FROM memory.peers").fetchall()
        _exec(conn, "COMMIT")
        assert visible == [(left,)]
        ddl = ddl_dsn()
        try:
            with psycopg.connect(ddl, autocommit=True) as owner:
                owner.execute("ALTER TABLE memory.peers DISABLE ROW LEVEL SECURITY")
            leaked = _exec(
                conn,
                "SELECT workspace_name FROM memory.peers WHERE workspace_name = %s",
                (left,),
            ).fetchall()
            assert leaked == [(left,)]
        finally:
            with psycopg.connect(ddl, autocommit=True) as owner:
                owner.execute("ALTER TABLE memory.peers ENABLE ROW LEVEL SECURITY")
                owner.execute("ALTER TABLE memory.peers FORCE ROW LEVEL SECURITY")
        import os

        runtime_role = os.environ["SENTICA_DB_USER"]
        admin = os.environ["SENTICA_RLS_ADMIN_DSN"].replace(
            "postgresql+psycopg://", "postgresql://", 1
        )
        with psycopg.connect(admin, autocommit=True) as owner:
            owner.execute("DROP ROLE IF EXISTS m2_no_execute")
            owner.execute("CREATE ROLE m2_no_execute NOLOGIN NOSUPERUSER")
            try:
                for function_name in (
                    "sentica_pending_counts",
                    "sentica_pending_queue_counts",
                ):
                    procedure = "memory." + function_name + "()"
                    denied = owner.execute(
                        "SELECT has_function_privilege(%s, %s::regprocedure, "
                        + "'EXECUTE')",
                        ("m2_no_execute", procedure),
                    ).fetchone()
                    allowed = owner.execute(
                        "SELECT has_function_privilege(%s, %s::regprocedure, "
                        + "'EXECUTE')",
                        (runtime_role, procedure),
                    ).fetchone()
                    assert denied == (False,)
                    assert allowed == (True,)
            finally:
                owner.execute("DROP ROLE IF EXISTS m2_no_execute")


_READ_ENGINES: dict[int, AsyncEngine] = {}


def _app_dsn(worker_id: str, db_engine: AsyncEngine | None = None) -> str:
    """The database the request uses. The job uses the runtime role."""
    if rls_job() or db_engine is None:
        from tests.sentica.rls_job import sync_dsn

        return sync_dsn(worker_id)
    return db_engine.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )


def _bind_unit_db(
    monkeypatch: pytest.MonkeyPatch,
    db_engine: AsyncEngine,
    request: pytest.FixtureRequest,
) -> None:
    """Point tracked_db at the per-test engine and pin the workspace GUC on it."""
    if rls_job():
        return
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import async_sessionmaker

    import src.sentica.workspace_scope as scope

    read = _READ_ENGINES.get(id(db_engine))
    if read is None:
        read = db_engine.execution_options(
            isolation_level="READ COMMITTED",
            postgresql_readonly=True,
        )
        scope._installed = False  # pyright: ignore[reportPrivateUsage]
        scope.install_workspace_scope(db_engine, read)
        _READ_ENGINES[id(db_engine)] = read

    @asynccontextmanager
    async def _tracked(_name: str | None = None, *, read_only: bool = False):
        bind = read if read_only else db_engine
        factory = async_sessionmaker(bind=bind, expire_on_commit=False)
        async with factory() as session:
            try:
                yield session
            finally:
                await session.rollback()
                await session.close()

    import importlib

    modules = [
        importlib.import_module(name)
        for name in (
            "src.dependencies",
            "src.reconciler.scheduler",
            "src.reconciler.sync_vectors",
            "src.reconciler.backfill",
            "src.backlog",
            "src.deriver.enqueue",
            "src.deriver.queue_manager",
        )
    ]
    for module in modules:
        if hasattr(module, "tracked_db"):
            monkeypatch.setattr(module, "tracked_db", _tracked)

    def _cleanup() -> None:
        scope.remove_workspace_scope(db_engine, read)
        _READ_ENGINES.pop(id(db_engine), None)

    request.addfinalizer(_cleanup)


def _install_counts(worker_id: str, db_engine: AsyncEngine | None = None) -> bool:
    """Install the counts function on a create_all database. The job already has it."""
    if rls_job():
        return False
    from src.config import settings
    from src.sentica.workspace_scope import (
        pending_counts_function_sql,
        pending_queue_counts_function_sql,
    )

    schema = settings.DB.SCHEMA
    with psycopg.connect(_app_dsn(worker_id, db_engine), autocommit=True) as conn:
        for name in ("sentica_pending_counts", "sentica_pending_queue_counts"):
            _exec(conn, "DROP FUNCTION IF EXISTS " + '"' + schema + '".' + name + "()")
        _exec(conn, pending_queue_counts_function_sql(schema))
        _exec(conn, pending_counts_function_sql(schema))
    return True


def _drop_counts(worker_id: str, db_engine: AsyncEngine | None = None) -> None:
    from src.config import settings

    schema = settings.DB.SCHEMA
    with psycopg.connect(_app_dsn(worker_id, db_engine), autocommit=True) as conn:
        for name in ("sentica_pending_counts", "sentica_pending_queue_counts"):
            _exec(conn, "DROP FUNCTION IF EXISTS " + '"' + schema + '".' + name + "()")


async def test_reconciler_gate_enqueues_one_row_per_workspace(
    client: object,
    worker_id: str,
    db_engine: object,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Each gate enqueues only the workspaces whose own count is non-zero."""
    from starlette.testclient import TestClient

    from src.config import settings
    from src.reconciler.scheduler import RECONCILER_TASKS, ReconcilerScheduler
    from tests.sentica.rls_job import authorize

    assert isinstance(client, TestClient)
    assert isinstance(db_engine, AsyncEngine)
    _bind_unit_db(monkeypatch, db_engine, request)
    names = {
        "embed": "e" + uuid.uuid4().hex[:8],
        "backfill": "b" + uuid.uuid4().hex[:8],
        "cleanup": "c" + uuid.uuid4().hex[:8],
        "quiet": "q" + uuid.uuid4().hex[:8],
    }
    installed = _install_counts(worker_id, db_engine)
    schema = settings.DB.SCHEMA
    try:
        for name in names.values():
            authorize(client, [name])
            assert client.post("/v3/workspaces", json={"name": name}).status_code in (
                200,
                201,
            )
            assert client.post(
                f"/v3/workspaces/{name}/peers", json={"name": "p1"}
            ).status_code in (200, 201)
            assert client.post(
                f"/v3/workspaces/{name}/sessions", json={"id": "s1"}
            ).status_code in (200, 201)
        with psycopg.connect(_app_dsn(worker_id, db_engine)) as conn:
            _exec(conn, "BEGIN")
            _exec(
                conn,
                "SELECT set_config('sentica.workspace', %s, true)",
                (names["embed"],),
            )
            public_id = "m" + uuid.uuid4().hex[:20]
            _exec(
                conn,
                "INSERT INTO "
                + schema
                + ".messages (public_id, session_name, content, token_count, "
                + "seq_in_session, peer_name, workspace_name) "
                + "VALUES (%s, 's1', 'pending-embed', 1, 1, 'p1', %s)",
                (public_id, names["embed"]),
            )
            _exec(
                conn,
                "INSERT INTO "
                + schema
                + ".message_embeddings (content, message_id, workspace_name, "
                + "session_name, peer_name, sync_state) "
                + "VALUES ('pending-embed', %s, %s, 's1', 'p1', 'pending')",
                (public_id, names["embed"]),
            )
            for name, source_ids, deleted in (
                (names["backfill"], '["aaaaaaaaaaaaaaaaaaaaa"]', False),
                (names["cleanup"], None, True),
            ):
                _exec(
                    conn,
                    "SELECT set_config('sentica.workspace', %s, true)",
                    (name,),
                )
                _exec(
                    conn,
                    "INSERT INTO "
                    + schema
                    + ".collections (id, observer, observed, workspace_name) "
                    + "VALUES (%s, 'p1', 'p1', %s)",
                    ("k" + uuid.uuid4().hex[:20], name),
                )
                _exec(
                    conn,
                    "INSERT INTO "
                    + schema
                    + ".documents (id, content, observer, observed, "
                    + "workspace_name, session_name, source_ids, deleted_at) "
                    + "VALUES (%s, %s, 'p1', 'p1', %s, 's1', %s::jsonb, "
                    + "CASE WHEN %s THEN now() - interval '10 minutes' "
                    + "ELSE NULL END)",
                    (
                        "d" + uuid.uuid4().hex[:20],
                        "row",
                        name,
                        source_ids,
                        deleted,
                    ),
                )
            _exec(conn, "COMMIT")
            _exec(conn, "BEGIN")
            _exec(
                conn,
                "SELECT set_config('sentica.workspace', %s, true)",
                (names["embed"],),
            )
            pending = _exec(
                conn,
                "SELECT count(*) FROM "
                + schema
                + ".message_embeddings WHERE workspace_name = %s "
                + "AND sync_state = 'pending'",
                (names["embed"],),
            ).fetchone()
            _exec(conn, "COMMIT")
            assert pending is not None and pending[0] >= 1
            scheduler = ReconcilerScheduler()
            sync_task = RECONCILER_TASKS["sync_vectors"]
            backfill_task = RECONCILER_TASKS["backfill_document_sources"]
            cleanup_task = RECONCILER_TASKS["cleanup"]
            await scheduler._try_enqueue_task(sync_task)  # pyright: ignore[reportPrivateUsage]
            await scheduler._try_enqueue_task(backfill_task)  # pyright: ignore[reportPrivateUsage]
            await scheduler._try_enqueue_task(cleanup_task)  # pyright: ignore[reportPrivateUsage]
            keys: set[str] = set()
            for name in names.values():
                _exec(conn, "BEGIN")
                _exec(
                    conn,
                    "SELECT set_config('sentica.workspace', %s, true)",
                    (name,),
                )
                found = _exec(
                    conn,
                    "SELECT work_unit_key FROM "
                    + schema
                    + ".queue WHERE workspace_name = %s",
                    (name,),
                ).fetchall()
                _exec(conn, "ROLLBACK")
                keys.update(str(row[0]) for row in found)
        embed_key = sync_task.work_unit_key + ":" + names["embed"]
        backfill_key = backfill_task.work_unit_key + ":" + names["backfill"]
        cleanup_key = cleanup_task.work_unit_key + ":" + names["cleanup"]
        assert embed_key in keys
        assert backfill_key in keys
        assert cleanup_key in keys
        for name in (names["backfill"], names["cleanup"], names["quiet"]):
            assert sync_task.work_unit_key + ":" + name not in keys
        for name in (names["embed"], names["cleanup"], names["quiet"]):
            assert backfill_task.work_unit_key + ":" + name not in keys
        for name in (names["embed"], names["backfill"], names["quiet"]):
            assert cleanup_task.work_unit_key + ":" + name not in keys
    finally:
        if installed:
            _drop_counts(worker_id, db_engine)


async def test_due_dream_is_reported_and_triggered(
    client: object,
    worker_id: str,
    db_engine: object,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """The runtime role reports a due dream and the api poll enqueues it."""
    from datetime import UTC, datetime, timedelta

    from starlette.testclient import TestClient

    from src.backlog import DeriverMetricsPoller
    from src.config import settings
    from src.dreamer.dream_due import list_due_dreams
    from tests.sentica.rls_job import authorize

    assert isinstance(client, TestClient)
    assert isinstance(db_engine, AsyncEngine)
    _bind_unit_db(monkeypatch, db_engine, request)
    name = "d" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(settings.DREAM, "ENABLED", False)
    monkeypatch.setattr(settings.DREAM, "DOCUMENT_THRESHOLD", 50)
    installed = _install_counts(worker_id, db_engine)
    schema = settings.DB.SCHEMA
    seen: list[tuple[str | None, object]] = []
    real_list = list_due_dreams

    async def _wrapped(db: AsyncSession) -> Any:
        from sqlalchemy import text

        from src.sentica.workspace_scope import current_workspace

        setting = await db.scalar(
            text("SELECT current_setting('sentica.workspace', true)")
        )
        seen.append((current_workspace(), setting))
        return await real_list(db)

    monkeypatch.setattr("src.backlog.list_due_dreams", _wrapped)
    monkeypatch.setattr(settings.DERIVER, "SCHEDULER", "api")
    try:
        authorize(client, [name])
        assert client.post("/v3/workspaces", json={"name": name}).status_code in (
            200,
            201,
        )
        assert client.post(
            f"/v3/workspaces/{name}/peers", json={"name": "p1"}
        ).status_code in (200, 201)
        assert client.post(
            f"/v3/workspaces/{name}/peers", json={"name": "p2"}
        ).status_code in (200, 201)
        assert client.post(
            f"/v3/workspaces/{name}/sessions", json={"id": "s1"}
        ).status_code in (200, 201)
        created = datetime.now(UTC) - timedelta(days=2)
        recent = datetime.now(UTC)
        with psycopg.connect(_app_dsn(worker_id, db_engine)) as conn:
            _exec(conn, "BEGIN")
            _exec(conn, "SELECT set_config('sentica.workspace', %s, true)", (name,))
            for observer in ("p1", "p2"):
                _exec(
                    conn,
                    "INSERT INTO "
                    + schema
                    + ".collections (id, observer, observed, workspace_name) "
                    + "VALUES (%s, %s, %s, %s)",
                    ("k" + uuid.uuid4().hex[:20], observer, observer, name),
                )
            for observer, stamp in (("p1", created), ("p2", recent)):
                _exec(
                    conn,
                    "INSERT INTO "
                    + schema
                    + ".documents (id, content, observer, observed, "
                    + "workspace_name, session_name, level, created_at) "
                    + "SELECT 'd' || substr(md5(%s || g::text), 1, 20), "
                    + "'dream note', %s, %s, %s, 's1', 'explicit', %s "
                    + "FROM generate_series(1, %s) AS g",
                    (observer + name, observer, observer, name, stamp, 2),
                )
            _exec(conn, "COMMIT")
        from sqlalchemy import text

        from src.dependencies import tracked_db

        async with tracked_db("dream.check", read_only=True) as db:
            present = await db.scalar(
                text("SELECT to_regprocedure(:name)"),
                {"name": schema + ".sentica_pending_counts()"},
            )
            due_rows = (
                await db.execute(
                    text(
                        "SELECT workspace_name, dreams_due FROM "
                        + schema
                        + ".sentica_pending_counts()"
                    )
                )
            ).all()
        assert present is not None, due_rows
        matched = [row for row in due_rows if row[0] == name]
        assert matched and int(matched[0][1]) == 2, due_rows
        monkeypatch.setattr(settings.DREAM, "ENABLED", True)
        monkeypatch.setattr(settings.DREAM, "DOCUMENT_THRESHOLD", 1)
        monkeypatch.setattr(settings.DREAM, "ENABLED_TYPES", ["omni"])
        poller = DeriverMetricsPoller()
        await poller.refresh()
        assert any(item[0] == name for item in seen), seen
        assert poller.snapshot.dreams_due >= 1
        with psycopg.connect(_app_dsn(worker_id, db_engine)) as conn:
            _exec(conn, "BEGIN")
            _exec(conn, "SELECT set_config('sentica.workspace', %s, true)", (name,))
            rows = _exec(
                conn,
                "SELECT payload::text FROM "
                + schema
                + ".queue WHERE workspace_name = %s AND task_type = 'dream'",
                (name,),
            ).fetchall()
            _exec(conn, "COMMIT")
        assert len(rows) == 1
        payload = rows[0][0]
        assert "api_poll" in payload
        assert '"observed": "p1"' in payload or '"observed":"p1"' in payload
    finally:
        if installed:
            _drop_counts(worker_id, db_engine)


def _two_workspaces(client: Any) -> tuple[str, str]:
    from tests.sentica.rls_job import authorize

    left = "x" + uuid.uuid4().hex[:8]
    right = "y" + uuid.uuid4().hex[:8]
    for name in (left, right):
        authorize(client, [left, right], primary=name)
        assert client.post("/v3/workspaces", json={"name": name}).status_code in (
            200,
            201,
        )
        assert client.post(
            f"/v3/workspaces/{name}/peers", json={"name": "p1"}
        ).status_code in (200, 201)
        assert client.post(
            f"/v3/workspaces/{name}/sessions", json={"id": "s1"}
        ).status_code in (200, 201)
    return left, right


def _workspace_sql(
    worker_id: str,
    db_engine: AsyncEngine,
    name: str,
    statement: str,
    params: tuple[Any, ...],
) -> None:
    from src.config import settings

    del settings
    with psycopg.connect(_app_dsn(worker_id, db_engine)) as conn:
        _exec(conn, "BEGIN")
        _exec(conn, "SELECT set_config('sentica.workspace', %s, true)", (name,))
        _exec(conn, statement, params)
        _exec(conn, "COMMIT")


async def test_later_test_has_no_workspace_guard(
    db_engine: object,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Removing the per-test listeners leaves the next transaction unguarded."""
    from sqlalchemy import text

    import src.sentica.workspace_scope as scope

    assert isinstance(db_engine, AsyncEngine)
    if rls_job():
        before = list(scope._listener_targets)  # pyright: ignore[reportPrivateUsage]
        scope.remove_workspace_scope()
        after = list(scope._listener_targets)  # pyright: ignore[reportPrivateUsage]
        assert after == before
        return
    _bind_unit_db(monkeypatch, db_engine, request)
    read = _READ_ENGINES[id(db_engine)]
    scope.remove_workspace_scope(db_engine, read)
    token = scope.set_workspace("left")
    try:
        async with db_engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
            scope.reset_workspace(token)
            token = scope.set_workspace("right")
            await conn.execute(text("SELECT 1"))
    finally:
        scope.reset_workspace(token)


async def test_sync_vectors_leaves_the_other_workspace(
    client: object,
    worker_id: str,
    db_engine: object,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    from starlette.testclient import TestClient

    from src.config import settings
    from src.reconciler.sync_vectors import run_vector_reconciliation_cycle
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    assert isinstance(client, TestClient)
    assert isinstance(db_engine, AsyncEngine)
    _bind_unit_db(monkeypatch, db_engine, request)
    left, right = _two_workspaces(client)
    schema = settings.DB.SCHEMA
    width = settings.EMBEDDING.VECTOR_DIMENSIONS

    async def _vectors(
        texts: list[str], *_args: object, **_kwargs: object
    ) -> list[list[float]]:
        return [[0.1] * width for _text in texts]

    monkeypatch.setattr(
        "src.reconciler.sync_vectors.embedding_client.simple_batch_embed", _vectors
    )
    for name in (left, right):
        public_id = "m" + uuid.uuid4().hex[:20]
        _workspace_sql(
            worker_id,
            db_engine,
            name,
            "INSERT INTO "
            + schema
            + ".messages (public_id, session_name, content, token_count, "
            + "seq_in_session, peer_name, workspace_name) "
            + "VALUES (%s, 's1', %s, 1, 1, 'p1', %s)",
            (public_id, "embed " + name, name),
        )
        _workspace_sql(
            worker_id,
            db_engine,
            name,
            "INSERT INTO "
            + schema
            + ".message_embeddings (content, message_id, workspace_name, "
            + "session_name, peer_name, sync_state) "
            + "VALUES (%s, %s, %s, 's1', 'p1', 'pending')",
            ("embed " + name, public_id, name),
        )
    token = set_workspace(left)
    try:
        await run_vector_reconciliation_cycle()
    finally:
        reset_workspace(token)
    with psycopg.connect(_app_dsn(worker_id, db_engine)) as conn:
        states: dict[str, str] = {}
        for name in (left, right):
            _exec(conn, "BEGIN")
            _exec(conn, "SELECT set_config('sentica.workspace', %s, true)", (name,))
            found = _exec(
                conn,
                "SELECT sync_state FROM "
                + schema
                + ".message_embeddings WHERE workspace_name = %s",
                (name,),
            ).fetchone()
            _exec(conn, "COMMIT")
            assert found is not None
            states[name] = str(found[0])
    assert states[left] == "synced"
    assert states[right] == "pending"


async def test_cleanup_leaves_the_other_workspace(
    client: object,
    worker_id: str,
    db_engine: object,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    from datetime import UTC, datetime, timedelta

    from starlette.testclient import TestClient

    from src.config import settings
    from src.reconciler.sync_vectors import run_document_cleanup
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    assert isinstance(client, TestClient)
    assert isinstance(db_engine, AsyncEngine)
    _bind_unit_db(monkeypatch, db_engine, request)
    left, right = _two_workspaces(client)
    schema = settings.DB.SCHEMA
    deleted_at = datetime.now(UTC) - timedelta(minutes=10)
    ids: dict[str, str] = {}
    for name in (left, right):
        doc_id = "d" + uuid.uuid4().hex[:20]
        ids[name] = doc_id
        _workspace_sql(
            worker_id,
            db_engine,
            name,
            "INSERT INTO "
            + schema
            + ".collections (id, observer, observed, workspace_name) "
            + "VALUES (%s, 'p1', 'p1', %s)",
            ("k" + uuid.uuid4().hex[:20], name),
        )
        _workspace_sql(
            worker_id,
            db_engine,
            name,
            "INSERT INTO "
            + schema
            + ".documents (id, content, observer, observed, workspace_name, "
            + "session_name, level, deleted_at) "
            + "VALUES (%s, %s, 'p1', 'p1', %s, 's1', 'explicit', %s)",
            (doc_id, "old " + name, name, deleted_at),
        )
    token = set_workspace(left)
    try:
        cleaned = await run_document_cleanup()
    finally:
        reset_workspace(token)
    assert cleaned == 1
    with psycopg.connect(_app_dsn(worker_id, db_engine)) as conn:
        for name, expected in ((left, None), (right, ids[right])):
            _exec(conn, "BEGIN")
            _exec(conn, "SELECT set_config('sentica.workspace', %s, true)", (name,))
            found = _exec(
                conn,
                "SELECT id FROM " + schema + ".documents WHERE id = %s",
                (ids[name],),
            ).fetchone()
            _exec(conn, "COMMIT")
            if expected is None:
                assert found is None
            else:
                assert found == (expected,)


async def test_backfill_leaves_the_other_workspace(
    client: object,
    worker_id: str,
    db_engine: object,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    from starlette.testclient import TestClient

    from src.config import settings
    from src.reconciler.backfill import BACKFILLS, run_backfill_cycle
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    assert isinstance(client, TestClient)
    assert isinstance(db_engine, AsyncEngine)
    _bind_unit_db(monkeypatch, db_engine, request)
    left, right = _two_workspaces(client)
    schema = settings.DB.SCHEMA
    source = "a" * 21
    ids: dict[str, str] = {}
    for name in (left, right):
        doc_id = "d" + uuid.uuid4().hex[:20]
        ids[name] = doc_id
        _workspace_sql(
            worker_id,
            db_engine,
            name,
            "INSERT INTO "
            + schema
            + ".collections (id, observer, observed, workspace_name) "
            + "VALUES (%s, 'p1', 'p1', %s)",
            ("k" + uuid.uuid4().hex[:20], name),
        )
        _workspace_sql(
            worker_id,
            db_engine,
            name,
            "INSERT INTO "
            + schema
            + ".documents (id, content, observer, observed, workspace_name, "
            + "session_name, level, source_ids) "
            + "VALUES (%s, %s, 'p1', 'p1', %s, 's1', 'explicit', %s::jsonb)",
            (doc_id, "link " + name, name, '["' + source + '"]'),
        )
    token = set_workspace(left)
    try:
        await run_backfill_cycle(BACKFILLS["document_sources"])
    finally:
        reset_workspace(token)
    with psycopg.connect(_app_dsn(worker_id, db_engine)) as conn:
        for name in (left, right):
            _exec(conn, "BEGIN")
            _exec(conn, "SELECT set_config('sentica.workspace', %s, true)", (name,))
            doc = _exec(
                conn,
                "SELECT source_ids IS NULL FROM " + schema + ".documents WHERE id = %s",
                (ids[name],),
            ).fetchone()
            edge = _exec(
                conn,
                "SELECT source_id FROM "
                + schema
                + ".document_sources WHERE derived_id = %s",
                (ids[name],),
            ).fetchone()
            _exec(conn, "COMMIT")
            assert doc is not None
            if name == left:
                assert doc[0] is True
                assert edge == (source,)
            else:
                assert doc[0] is False
                assert edge is None
