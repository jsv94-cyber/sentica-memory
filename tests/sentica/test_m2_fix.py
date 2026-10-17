"""Regression tests for the M2 review fixes."""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
    from tests.sentica.rls_job import sync_dsn

    return sync_dsn(worker_id)


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
    from src.sentica.workspace_scope import pending_queue_counts_function_sql

    sql = pending_counts_function_sql("memory")
    queue_sql = pending_queue_counts_function_sql("memory")
    assert "SECURITY DEFINER" in sql
    assert "search_path = memory, pg_temp" in sql
    assert "pending_queue" in sql
    assert "pending_embeddings" in sql
    assert "pending_backfills" in sql
    assert "soft_deleted_documents" in sql
    assert "dreams_due" in sql
    assert "last_dream_document_count" in sql
    assert "split_part" not in sql
    assert "work_unit_key" not in sql
    assert "deleted_at IS NOT NULL" in sql
    assert "sentica_pending_counts()" in sql
    assert "content" not in sql
    assert "SECURITY DEFINER" in queue_sql
    assert "search_path = memory, pg_temp" in queue_sql
    assert "sentica_pending_queue_counts()" in queue_sql
    assert "pending_embeddings" not in queue_sql
    assert "content" not in queue_sql
    migration = (
        ROOT
        / "migrations"
        / "versions"
        / "c4e8b1a2d6f0_runtime_role_and_pending_counts.py"
    ).read_text(encoding="utf-8")
    assert "REVOKE ALL ON FUNCTION" in migration
    assert "sentica_pending_queue_counts" in migration
    assert "sentica_pending_counts" in migration
    assert "must end in _ddl" in migration
    assert "ix_documents_soft_deleted_cleanup" in migration


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
    assert "build-default-image" in text_value
    assert "test-gvisor-smoke" in text_value
    assert "gvisor.tar.bz2" in text_value
    assert "sha512sum -c gvisor.tar.bz2.sha512" in text_value
    assert "needs.build-default-image.result" in text_value
    assert "needs.test-gvisor-smoke.result" in text_value


def test_data_migration_updates_each_workspace(worker_id: str) -> None:
    from sqlalchemy import create_engine

    left = "a" + uuid.uuid4().hex[:20]
    right = "b" + uuid.uuid4().hex[:20]
    sync = _sync_dsn(worker_id)
    url = sync.replace("postgresql://", "postgresql+psycopg://", 1)
    with psycopg.connect(sync) as conn:
        for name, public_id in (
            (left, "l" + uuid.uuid4().hex[:20]),
            (right, "r" + uuid.uuid4().hex[:20]),
        ):
            conn.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, %s)",
                (public_id, name),
            )
            conn.execute(
                "SELECT set_config('sentica.workspace', %s, true)",
                (name,),
            )
            conn.execute(
                "INSERT INTO peers (id, name, workspace_name) VALUES (%s, %s, %s)",
                ("p" + uuid.uuid4().hex[:20], "peer", name),
            )
            conn.commit()
        schema = conn.execute("SELECT current_schema()").fetchone()
        assert schema is not None
    engine = create_engine(url)
    with engine.connect() as sql_conn:
        apply_per_workspace(
            sql_conn,
            schema[0],
            "UPDATE peers SET internal_metadata = "
            + "jsonb_build_object('seen', current_setting('sentica.workspace', true)) "
            + "WHERE name = 'peer' AND workspace_name = "
            + "current_setting('sentica.workspace', true)",
        )
    with psycopg.connect(sync) as conn:
        rows: list[Any] = []
        for name in (left, right):
            conn.execute(
                "SELECT set_config('sentica.workspace', %s, true)",
                (name,),
            )
            row = conn.execute(
                "SELECT workspace_name, internal_metadata->>'seen' FROM peers "
                + "WHERE workspace_name = %s",
                (name,),
            ).fetchone()
            rows.append(row)
            conn.commit()
        assert rows == [(left, left), (right, right)]
        for name in (left, right):
            conn.execute(
                "SELECT set_config('sentica.workspace', %s, true)",
                (name,),
            )
            conn.execute("DELETE FROM peers WHERE workspace_name = %s", (name,))
            conn.commit()
        conn.execute("DELETE FROM workspaces WHERE name IN (%s, %s)", (left, right))
        conn.commit()


def test_queue_workspace_name_is_required(worker_id: str) -> None:
    """A queue row without a workspace is rejected."""
    from tests.sentica.rls_job import ddl_dsn, rls_job

    with (
        psycopg.connect(_sync_dsn(worker_id)) as conn,
        pytest.raises(psycopg.Error) as exc,
    ):
        conn.execute(
            "INSERT INTO queue (work_unit_key, task_type, payload) "
            + "VALUES ('missing-workspace', 'representation', '{}')"
        )
    # create_all has no policy, so NOT NULL reports 23502. FORCE RLS on the
    # migrated schema rejects the row in WITH CHECK and reports 42501.
    if rls_job():
        assert exc.value.sqlstate == "42501"
        with psycopg.connect(ddl_dsn(), autocommit=True) as owner:
            left = _exec(
                owner,
                "SELECT count(*) FROM memory.queue "
                + "WHERE work_unit_key = 'missing-workspace'",
            ).fetchone()
        assert left == (0,)
        # The superuser bypasses RLS, so the same insert is the NOT NULL error.
        import os

        admin_dsn = os.environ["SENTICA_RLS_ADMIN_DSN"].replace(
            "postgresql+psycopg://", "postgresql://", 1
        )
        with (
            psycopg.connect(admin_dsn) as admin,
            pytest.raises(psycopg.Error) as not_null,
        ):
            _exec(
                admin,
                "INSERT INTO memory.queue (work_unit_key, task_type, payload) "
                + "VALUES ('missing-workspace-admin', 'representation', '{}')",
            )
        assert not_null.value.sqlstate == "23502"
    else:
        assert exc.value.sqlstate == "23502"


def _revision_check_on_migrated_schema() -> None:
    """The runtime role can read the revision. The ddl role writes it."""
    from sqlalchemy import create_engine

    from src.sentica.schema_revision import assert_revision, head_revision
    from tests.sentica.rls_job import ddl_dsn, runtime_sqlalchemy_url

    head = head_revision()
    url = runtime_sqlalchemy_url()
    with psycopg.connect(ddl_dsn(), autocommit=True) as owner:
        owner.execute("UPDATE memory.alembic_version SET version_num = 'not-the-head'")
    engine = create_engine(url)
    try:
        with (
            engine.connect() as conn,
            pytest.raises(SenticaStartupError, match="schema revision is behind"),
        ):
            assert_revision(conn, "memory")
    finally:
        engine.dispose()
        with psycopg.connect(ddl_dsn(), autocommit=True) as owner:
            owner.execute(
                "UPDATE memory.alembic_version SET version_num = %s",
                (head,),
            )
    engine = create_engine(url)
    with engine.connect() as conn:
        assert_revision(conn, "memory")
    engine.dispose()


async def test_workspace_setting_is_not_inherited(
    worker_id: str, db_session: object
) -> None:
    """After a transaction bound to A commits, the next one sees no rows."""
    from tests.sentica.rls_job import rls_job

    if rls_job():
        await _setting_not_inherited_as_runtime()
        return
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
            raw = await conn.get_raw_connection()
            driver = raw.driver_connection
            assert driver is not None
            cursor = driver.cursor()
            await cursor.execute("SELECT current_setting('sentica.workspace', true)")
            raw_setting = await cursor.fetchone()
            await cursor.close()
            await driver.rollback()
            assert raw_setting is not None
            assert raw_setting[0] in ("", None)
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
            with pytest.raises(RuntimeError, match="open transaction"):
                switched = set_workspace(right)
                try:
                    await conn.execute(text("SELECT 1"))
                finally:
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
    from tests.sentica.rls_job import rls_job

    if rls_job():
        _revision_check_on_migrated_schema()
        return
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


async def test_deriver_metrics_are_limited_to_the_token_workspaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import schemas
    from src.backlog import (
        DeriverMetricsPoller,
        DeriverMetricsSnapshot,
        active_work_seconds,
    )
    from src.config import settings
    from src.routers import deriver_metrics
    from src.security import JWTParams

    monkeypatch.setattr(settings.AUTH, "USE_AUTH", True)
    seen: list[list[str]] = []

    async def _scoped(names: list[str]) -> schemas.DeriverMetrics:
        seen.append(list(names))
        return schemas.DeriverMetrics(
            eligible_work_units=0,
            claimed_work_units=0,
            pending_items=1,
            oldest_pending_age_seconds=0.0,
            embeddings_pending=0,
            embeddings_pending_due=0,
        )

    monkeypatch.setattr("src.crud.metrics_for_workspaces", _scoped)

    async def _counts(_db: object) -> list[object]:
        from src.sentica.workspace_scope import PendingCount

        return [PendingCount("left", 0, 0, 0, 0, 9)]

    async def _due(_db: object) -> list[object]:
        from src.dreamer.dream_due import DueDream
        from src.schemas import DreamType
        from src.sentica.workspace_scope import current_workspace

        del _db
        if current_workspace() != "left":
            return []
        return [DueDream("left", "p1", "p1", DreamType.OMNI, "s1")]

    monkeypatch.setattr("src.sentica.workspace_scope.fetch_pending_counts", _counts)
    monkeypatch.setattr("src.dreamer.dream_due.list_due_dreams", _due)
    poller = DeriverMetricsPoller()
    poller._snapshot = DeriverMetricsSnapshot(  # pyright: ignore[reportPrivateUsage]
        signal_seconds=9.0,
        dreams_due=7,
        stats=schemas.DeriverMetrics(
            eligible_work_units=0,
            claimed_work_units=0,
            pending_items=99,
            oldest_pending_age_seconds=0.0,
            embeddings_pending=0,
            embeddings_pending_due=0,
        ),
        measured_at=1.0,
    )
    deriver_metrics.set_deriver_metrics_poller(poller)
    try:
        body = await deriver_metrics.get_deriver_metrics_response(
            JWTParams(t="t", w="left", workspaces=["left"])
        )
    finally:
        deriver_metrics.set_deriver_metrics_poller(None)
    assert seen == [["left"]]
    assert body["pending_items"] == 1
    assert body["dreams_due"] == 1
    assert body["outstanding_work_seconds"] == active_work_seconds()


async def test_execute_dream_and_delayed_dream_bind_the_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.dreamer.dream_scheduler import DreamScheduler
    from src.schemas import DreamType
    from src.sentica.workspace_scope import current_workspace

    seen: list[str | None] = []

    async def _bound(
        self: DreamScheduler,
        workspace_name: str,
        dream_type: DreamType,
        **_kwargs: object,
    ) -> None:
        del self, workspace_name, dream_type
        seen.append(current_workspace())

    async def _sleep(_seconds: float) -> None:
        seen.append(current_workspace())

    monkeypatch.setattr(DreamScheduler, "_execute_dream_bound", _bound)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    scheduler = DreamScheduler()
    await scheduler.execute_dream("left-ws", DreamType.OMNI, observer="a", observed="b")
    await scheduler._delayed_dream(  # pyright: ignore[reportPrivateUsage]
        "key", "right-ws", 1, DreamType.OMNI, observer="a", observed="b"
    )
    assert seen[0] == "left-ws"
    assert "right-ws" in seen


class _Peers:
    """Two peers and an engine whose listener installs sentica.workspace."""

    def __init__(
        self,
        engine: AsyncEngine,
        left: str,
        right: str,
        role: str | None,
    ) -> None:
        self.engine: AsyncEngine = engine
        self.left: str = left
        self.right: str = right
        self.role: str | None = role


@asynccontextmanager
async def _isolated_peers(worker_id: str) -> AsyncGenerator[_Peers]:
    """Peers in two workspaces, visible only through the workspace setting."""
    import src.sentica.workspace_scope as scope
    from src.sentica.workspace_scope import workspace_policy_sql
    from tests.sentica.rls_job import rls_job, runtime_dsn

    left = "a" + uuid.uuid4().hex[:20]
    right = "b" + uuid.uuid4().hex[:20]
    if rls_job():
        from src.db import engine as app_engine

        with psycopg.connect(runtime_dsn()) as conn:
            for name in (left, right):
                conn.execute(
                    "INSERT INTO workspaces (id, name) VALUES (%s, %s)",
                    ("w" + uuid.uuid4().hex[:20], name),
                )
                conn.execute(
                    "SELECT set_config('sentica.workspace', %s, true)",
                    (name,),
                )
                conn.execute(
                    "INSERT INTO peers (id, name, workspace_name) "
                    + "VALUES (%s, %s, %s)",
                    ("p" + uuid.uuid4().hex[:20], name, name),
                )
                conn.commit()
        try:
            yield _Peers(app_engine, left, right, None)
        finally:
            with psycopg.connect(runtime_dsn()) as conn:
                for name in (left, right):
                    conn.execute(
                        "SELECT set_config('sentica.workspace', %s, true)",
                        (name,),
                    )
                    conn.execute(
                        "DELETE FROM peers WHERE workspace_name = %s",
                        (name,),
                    )
                    conn.commit()
                conn.execute(
                    "DELETE FROM workspaces WHERE name IN (%s, %s)",
                    (left, right),
                )
                conn.commit()
        return

    runtime = "inh" + uuid.uuid4().hex[:8]
    sync = _sync_dsn(worker_id)
    schema = "public"
    with psycopg.connect(sync, autocommit=True) as conn:
        _exec(conn, "CREATE ROLE " + runtime + " NOLOGIN NOBYPASSRLS")
        current = conn.execute("SELECT current_schema()").fetchone()
        assert current is not None
        schema = str(current[0])
        for name in (left, right):
            conn.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, %s)",
                ("w" + uuid.uuid4().hex[:20], name),
            )
            conn.execute(
                "INSERT INTO peers (id, name, workspace_name) VALUES (%s, %s, %s)",
                ("p" + uuid.uuid4().hex[:20], name, name),
            )
        _exec(conn, workspace_policy_sql(schema, "peers"))
        _exec(conn, 'GRANT SELECT ON "' + schema + '"."peers" TO ' + runtime)
    url = sync.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(url)
    scope._installed = False  # pyright: ignore[reportPrivateUsage]
    scope.install_workspace_scope(engine)
    try:
        yield _Peers(engine, left, right, runtime)
    finally:
        await engine.dispose()
        with psycopg.connect(sync, autocommit=True) as conn:
            _exec(
                conn,
                'DROP POLICY IF EXISTS sentica_workspace ON "' + schema + '"."peers"',
            )
            _exec(
                conn, 'ALTER TABLE "' + schema + '"."peers" DISABLE ROW LEVEL SECURITY'
            )
            conn.execute(
                "DELETE FROM peers WHERE workspace_name IN (%s, %s)",
                (left, right),
            )
            conn.execute(
                "DELETE FROM workspaces WHERE name IN (%s, %s)",
                (left, right),
            )
            _exec(conn, "DROP OWNED BY " + runtime)
            _exec(conn, "DROP ROLE " + runtime)


async def _use_runtime(conn: Any, role: str | None) -> None:
    if role is not None:
        await conn.execute(text("SET ROLE " + role))


async def _setting_not_inherited_as_runtime() -> None:
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    async with _isolated_peers("master") as peers, peers.engine.connect() as conn:
        held = set_workspace(peers.left)
        await _use_runtime(conn, peers.role)
        count = await conn.scalar(text("SELECT count(*) FROM peers"))
        await conn.commit()
        reset_workspace(held)
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection
        assert driver is not None
        cursor = driver.cursor()
        await cursor.execute("SELECT current_setting('sentica.workspace', true)")
        raw_setting = await cursor.fetchone()
        await cursor.close()
        await driver.rollback()
        assert raw_setting is not None
        assert raw_setting[0] in ("", None)
        later = await conn.scalar(text("SELECT count(*) FROM peers"))
        await conn.commit()
        assert count == 1
        assert later == 0
        held = set_workspace(peers.left)
        await conn.execute(text("SELECT 1"))
        reset_workspace(held)
        with pytest.raises(RuntimeError, match="open transaction"):
            changed = set_workspace(peers.right)
            try:
                await conn.execute(text("SELECT 1"))
            finally:
                reset_workspace(changed)
        await conn.rollback()
        again = set_workspace(peers.left)
        await conn.execute(text("SELECT 1"))
        nested = await conn.begin_nested()
        await nested.rollback()
        reset_workspace(again)
        with pytest.raises(RuntimeError, match="open transaction"):
            switched = set_workspace(peers.right)
            try:
                await conn.execute(text("SELECT 1"))
            finally:
                reset_workspace(switched)
        await conn.rollback()


async def test_workspace_change_inside_an_open_transaction_is_refused(
    worker_id: str,
) -> None:
    """Item 6: a second workspace cannot be installed mid-transaction."""
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    async with _isolated_peers(worker_id) as peers:
        held = set_workspace(peers.left)
        try:
            async with peers.engine.connect() as conn:
                await _use_runtime(conn, peers.role)
                await conn.execute(text("SELECT 1"))
                reset_workspace(held)
                held = None
                with pytest.raises(RuntimeError, match="open transaction"):
                    changed = set_workspace(peers.right)
                    try:
                        await conn.execute(text("SELECT 1"))
                    finally:
                        reset_workspace(changed)
                await conn.rollback()
        finally:
            if held is not None:
                reset_workspace(held)


async def test_savepoint_rollback_refuses_a_different_workspace(
    worker_id: str,
) -> None:
    """A savepoint rollback keeps the pinned workspace and refuses a switch."""
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    async with _isolated_peers(worker_id) as peers:
        held = set_workspace(peers.left)
        try:
            async with peers.engine.connect() as conn:
                await _use_runtime(conn, peers.role)
                count = await conn.scalar(text("SELECT count(*) FROM peers"))
                nested = await conn.begin_nested()
                await nested.rollback()
                again = await conn.scalar(text("SELECT count(*) FROM peers"))
                assert count == 1
                assert again == 1
                reset_workspace(held)
                held = None
                with pytest.raises(RuntimeError, match="open transaction"):
                    switched = set_workspace(peers.right)
                    try:
                        await conn.execute(text("SELECT 1"))
                    finally:
                        reset_workspace(switched)
                await conn.rollback()
        finally:
            if held is not None:
                reset_workspace(held)


async def test_workspace_setting_is_cleared_on_rollback(worker_id: str) -> None:
    """Item 19: rollback drops the cached workspace, not only commit."""
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    async with _isolated_peers(worker_id) as peers:
        held = set_workspace(peers.left)
        try:
            async with peers.engine.connect() as conn:
                await _use_runtime(conn, peers.role)
                count = await conn.scalar(text("SELECT count(*) FROM peers"))
                assert count == 1
                await conn.rollback()
                await _use_runtime(conn, peers.role)
                again = await conn.scalar(text("SELECT count(*) FROM peers"))
                assert again == 1
        finally:
            reset_workspace(held)


def test_null_queue_rows_are_deleted_when_force_rls_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-superuser ddl role can delete NULL queue rows and reach head."""
    import os

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.engine.url import make_url

    from src.config import settings
    from tests.sentica.rls_job import rls_job

    admin = (
        os.environ["SENTICA_RLS_ADMIN_DSN"]
        if rls_job()
        else os.environ["DB_CONNECTION_URI"]
    )
    admin_url = make_url(admin).set(drivername="postgresql")
    suffix = uuid.uuid4().hex[:10]
    database = "m2mig" + suffix
    runtime = "m2" + suffix
    role = runtime + "_ddl"
    password = "m2mig-secret"
    maintenance = admin_url.set(database="postgres").render_as_string(
        hide_password=False
    )
    # The job's admin URL names the tenant database, which can create another.
    if rls_job():
        maintenance = admin_url.render_as_string(hide_password=False)
    schema = settings.DB.SCHEMA
    role_url = admin_url.set(
        username=role, password=password, database=database
    ).render_as_string(hide_password=False)
    monkeypatch.setattr(
        settings.DB,
        "CONNECTION_URI",
        role_url.replace("postgresql://", "postgresql+psycopg://", 1),
    )
    with psycopg.connect(maintenance, autocommit=True) as conn:
        _exec(
            conn,
            "CREATE ROLE " + runtime + " NOLOGIN NOSUPERUSER NOBYPASSRLS",
        )
        _exec(
            conn,
            "CREATE ROLE "
            + role
            + " LOGIN PASSWORD 'm2mig-secret' NOSUPERUSER NOBYPASSRLS",
        )
        _exec(conn, "CREATE DATABASE " + database + " OWNER " + role)
    try:
        owner = admin_url.set(database=database).render_as_string(hide_password=False)
        with psycopg.connect(owner, autocommit=True) as conn:
            _exec(conn, "CREATE EXTENSION IF NOT EXISTS vector")
            _exec(conn, "CREATE EXTENSION IF NOT EXISTS pg_trgm")
        cfg = Config(str(ROOT / "alembic.ini"))
        command.upgrade(cfg, "3e7a1c9d5b20")
        with psycopg.connect(role_url, autocommit=True) as conn:
            _exec(
                conn,
                "INSERT INTO "
                + schema
                + ".queue (work_unit_key, task_type, payload, workspace_name) "
                + "VALUES (%s, 'representation', %s::jsonb, NULL)",
                ("null-workspace", "{}"),
            )
        command.upgrade(cfg, "head")
        with psycopg.connect(role_url, autocommit=True) as conn:
            nullable = _exec(
                conn,
                """
                SELECT is_nullable FROM information_schema.columns
                WHERE table_schema = %s AND table_name = 'queue'
                    AND column_name = 'workspace_name'
                """,
                (schema,),
            ).fetchone()
            left = _exec(
                conn,
                "SELECT count(*) FROM "
                + schema
                + ".queue WHERE workspace_name IS NULL",
            ).fetchone()
        assert nullable == ("NO",)
        assert left == (0,)
        with psycopg.connect(owner, autocommit=True) as conn:
            superuser_left = _exec(
                conn,
                "SELECT count(*) FROM "
                + schema
                + ".queue WHERE workspace_name IS NULL",
            ).fetchone()
        assert superuser_left == (0,)
    finally:
        with psycopg.connect(maintenance, autocommit=True) as conn:
            _exec(
                conn,
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                + "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            _exec(conn, "DROP DATABASE IF EXISTS " + database)
            _exec(conn, "DROP ROLE IF EXISTS " + role)
            _exec(conn, "DROP ROLE IF EXISTS " + runtime)


async def test_claim_loop_uses_the_queue_counts_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.deriver.queue_manager import QueueManager
    from src.sentica.workspace_scope import PendingQueueCount

    called = {"queue": 0, "full": 0}

    async def _queue(_db: object) -> list[PendingQueueCount]:
        called["queue"] += 1
        return [PendingQueueCount(workspace_name="left", pending_queue=0)]

    async def _full(_db: object) -> list[object]:
        called["full"] += 1
        raise AssertionError("the claim loop must not read the reconciler counts")

    monkeypatch.setattr(
        "src.sentica.workspace_scope.fetch_pending_queue_counts", _queue
    )
    monkeypatch.setattr("src.sentica.workspace_scope.fetch_pending_counts", _full)
    claimed = await QueueManager().get_and_claim_work_units()
    assert called == {"queue": 1, "full": 0}
    assert claimed == {}


def test_migration_refuses_a_role_that_does_not_end_in_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.engine.url import make_url

    from src.config import settings
    from tests.sentica.rls_job import rls_job

    admin = (
        os.environ["SENTICA_RLS_ADMIN_DSN"]
        if rls_job()
        else os.environ["DB_CONNECTION_URI"]
    )
    admin_url = make_url(admin).set(drivername="postgresql")
    suffix = uuid.uuid4().hex[:10]
    database = "m2bad" + suffix
    role = "m2plain" + suffix
    maintenance = admin_url.set(database="postgres").render_as_string(
        hide_password=False
    )
    if rls_job():
        maintenance = admin_url.render_as_string(hide_password=False)
    role_url = admin_url.set(
        username=role, password="m2mig-secret", database=database
    ).render_as_string(hide_password=False)
    monkeypatch.setattr(
        settings.DB,
        "CONNECTION_URI",
        role_url.replace("postgresql://", "postgresql+psycopg://", 1),
    )
    with psycopg.connect(maintenance, autocommit=True) as conn:
        _exec(
            conn,
            "CREATE ROLE "
            + role
            + " LOGIN PASSWORD 'm2mig-secret' NOSUPERUSER NOBYPASSRLS",
        )
        _exec(conn, "CREATE DATABASE " + database + " OWNER " + role)
    try:
        owner = admin_url.set(database=database).render_as_string(hide_password=False)
        with psycopg.connect(owner, autocommit=True) as conn:
            _exec(conn, "CREATE EXTENSION IF NOT EXISTS vector")
            _exec(conn, "CREATE EXTENSION IF NOT EXISTS pg_trgm")
        cfg = Config(str(ROOT / "alembic.ini"))
        with pytest.raises(Exception, match="must end in _ddl"):
            command.upgrade(cfg, "head")
    finally:
        with psycopg.connect(maintenance, autocommit=True) as conn:
            _exec(
                conn,
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                + "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            _exec(conn, "DROP DATABASE IF EXISTS " + database)
            _exec(conn, "DROP ROLE IF EXISTS " + role)
