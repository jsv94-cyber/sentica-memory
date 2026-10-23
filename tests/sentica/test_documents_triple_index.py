"""Composite index for conclusion reads on (workspace, observer, observed)."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import CompoundSelect, Select

from src import models, schemas
from src.config import settings
from src.crud import document as document_crud
from src.crud.peer_card import replace_peer_card_from_conclusions
from src.sentica.runtime import SenticaStartupError
from src.sentica.schema_revision import assert_revision, head_revision
from src.sentica.taint import collection_run_trust

ROOT = Path(__file__).resolve().parents[2]
REVISION = "e7c2a91b4d08"
PREVIOUS = "d4a8c1e27b90"
INDEX = "ix_documents_workspace_observer_observed"
_TARGET = ("wa", "o00", "d00")
_ROWS = 10_000


def _migration_source() -> str:
    name = "e7c2a91b4d08_documents_workspace_observer_observed.py"
    return (ROOT / "migrations" / "versions" / name).read_text(encoding="utf-8")


def _admin_url() -> Any:
    raw = os.environ.get("DB_CONNECTION_URI") or settings.DB.CONNECTION_URI
    if not raw:
        raise RuntimeError("DB_CONNECTION_URI is required")
    return make_url(raw)


def _sync(url: Any) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _index_row(conn: Any, schema: str) -> tuple[Any, ...] | None:
    row = conn.execute(
        """
        SELECT pg_get_indexdef(i.indexrelid), i.indisvalid
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = %s AND n.nspname = %s
        """,
        (INDEX, schema),
    ).fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def _version(conn: Any, schema: str) -> str | None:
    row = conn.execute(
        sql.SQL("SELECT version_num FROM {}.alembic_version").format(
            sql.Identifier(schema)
        )
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def _check_revision(url: str, schema: str) -> None:
    """The check docker/entrypoint.sh runs for the API and the deriver."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert_revision(conn, schema)
    finally:
        engine.dispose()


def test_api_and_deriver_share_the_revision_gate() -> None:
    """Both processes run the schema check before they serve."""
    entry = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    check_at = entry.index("assert_schema_at_head")
    assert check_at < entry.index('exec "$@"')
    assert check_at < entry.index("fastapi run")
    assert "Refusing to start: schema revision is behind" in entry
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["/app/docker/entrypoint.sh"]' in dockerfile
    compose = (ROOT / "docker-compose.sentica.yml").read_text(encoding="utf-8")
    assert 'entrypoint: ["/app/docker/entrypoint.sh"]' in compose
    assert 'command: ["/app/.venv/bin/python", "-m", "src.deriver"]' in compose
    integration = (ROOT / "docs" / "INTEGRATION.md").read_text(encoding="utf-8")
    assert "Start the API and the deriver. Both refuse to start when" in integration
    assert "is not this image's head" in integration
    source = _migration_source()
    assert f'revision: str = "{REVISION}"' in source
    assert f'down_revision: str | None = "{PREVIOUS}"' in source
    assert "autocommit_block()" in source
    assert "postgresql_concurrently=True" in source
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(cfg).get_heads() == [REVISION]
    assert head_revision() == REVISION


def test_ddl_role_migrates_the_index_up_and_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrade and downgrade as the ddl role. The previous head is refused."""
    admin = _admin_url()
    token = uuid.uuid4().hex[:8]
    runtime_role = "r6idx" + token
    ddl_role = runtime_role + "_ddl"
    database = "r6idx" + token
    password = admin.password or "r6idx-secret"
    schema = settings.DB.SCHEMA
    maintenance = _sync(admin.set(database="postgres"))
    ddl_sync = _sync(admin.set(username=ddl_role, password=password, database=database))
    ddl_url = ddl_sync.replace("postgresql://", "postgresql+psycopg://", 1)
    monkeypatch.setattr(settings.DB, "CONNECTION_URI", ddl_url)
    monkeypatch.setenv("DB_CONNECTION_URI", ddl_url)
    engine_logger = logging.getLogger("sqlalchemy.engine")
    previous_level = engine_logger.level
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", ddl_url)
    try:
        with psycopg.connect(maintenance, autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN NOBYPASSRLS").format(
                    sql.Identifier(runtime_role)
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
        with psycopg.connect(
            _sync(admin.set(database=database)), autocommit=True
        ) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        command.upgrade(cfg, PREVIOUS)
        with psycopg.connect(ddl_sync, autocommit=True) as conn:
            assert _version(conn, schema) == PREVIOUS
            assert _index_row(conn, schema) is None
        with pytest.raises(SenticaStartupError, match="schema revision is behind"):
            _check_revision(ddl_url, schema)
        command.upgrade(cfg, REVISION)
        with psycopg.connect(ddl_sync, autocommit=True) as conn:
            assert _version(conn, schema) == REVISION
            created = _index_row(conn, schema)
            assert created is not None
            definition, valid = created
            assert valid is True
            assert "(workspace_name, observer, observed)" in str(definition)
        _check_revision(ddl_url, schema)
        command.downgrade(cfg, PREVIOUS)
        with psycopg.connect(ddl_sync, autocommit=True) as conn:
            assert _version(conn, schema) == PREVIOUS
            assert _index_row(conn, schema) is None
        with pytest.raises(SenticaStartupError, match="schema revision is behind"):
            _check_revision(ddl_url, schema)
        command.upgrade(cfg, REVISION)
        with psycopg.connect(ddl_sync, autocommit=True) as conn:
            restored = _index_row(conn, schema)
            assert restored is not None and restored[1] is True
            assert _version(conn, schema) == REVISION
        _check_revision(ddl_url, schema)
    finally:
        engine_logger.setLevel(previous_level)
        logging.getLogger("sqlalchemy.engine.Engine").disabled = True
        with psycopg.connect(maintenance, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                + "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database))
            )
            conn.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(ddl_role))
            )
            conn.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime_role))
            )


def _cast_distance(explained_sql: str) -> str:
    """Bind every cosine argument as vector. float[] has no <=> operator."""
    needle = " <=> %("
    if needle not in explained_sql:
        return explained_sql
    parts: list[str] = []
    rest = explained_sql
    while needle in rest:
        start = rest.find(needle)
        parts.append(rest[:start])
        rest = rest[start + len(needle) :]
        end = rest.find(")s")
        if end == -1:
            return explained_sql
        parts.append(" <=> CAST(%(")
        parts.append(rest[: end + 2])
        parts.append(" AS vector)")
        rest = rest[end + 2 :]
    parts.append(rest)
    return "".join(parts)


def _plan_sync(sync_session: Any, statement: Any) -> str:
    connection = sync_session.connection()
    compiled = statement.compile(dialect=connection.dialect)
    explained = connection.exec_driver_sql(
        "EXPLAIN " + _cast_distance(str(compiled.string)),
        compiled.params,
    )
    return "\n".join(str(row[0]) for row in explained.all())


def _is_triple_read(statement: Any) -> bool:
    if not isinstance(statement, Select | Update | CompoundSelect):
        return False
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    lowered = compiled.lower()
    return "documents" in lowered and "observer" in lowered and "observed" in lowered


async def _plans_of(
    db: AsyncSession, call: Callable[[], Awaitable[object]]
) -> list[str]:
    plans: list[str] = []
    sync_session = db.sync_session
    original: Any = sync_session._execute_internal  # pyright: ignore[reportPrivateUsage]
    busy = False

    def _watching(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal busy
        if not busy and _is_triple_read(statement):
            busy = True
            try:
                plans.append(_plan_sync(sync_session, statement))
            finally:
                busy = False
        return original(statement, *args, **kwargs)

    sync_session._execute_internal = _watching  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    try:
        await call()
    finally:
        sync_session._execute_internal = original  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    return plans


def _require(plans: list[str]) -> None:
    assert plans, "the triple read did not run"
    missing = [plan for plan in plans if INDEX not in plan]
    assert not missing, missing


async def _seed(db: AsyncSession) -> None:
    """10,000 conclusions, one row for each observer/observed pair."""
    await db.execute(
        text(
            "INSERT INTO workspaces (id, name) VALUES "
            + "(substr(md5('ws-wa'), 1, 21), 'wa')"
        )
    )
    await db.execute(
        text(
            "INSERT INTO peers (id, name, workspace_name) SELECT "
            + "substr(md5('peer-o' || i::text), 1, 21), "
            + "'o' || lpad(i::text, 2, '0'), 'wa' "
            + "FROM generate_series(0, 99) AS i"
        )
    )
    await db.execute(
        text(
            "INSERT INTO peers (id, name, workspace_name) SELECT "
            + "substr(md5('peer-d' || i::text), 1, 21), "
            + "'d' || lpad(i::text, 2, '0'), 'wa' "
            + "FROM generate_series(0, 99) AS i"
        )
    )
    await db.execute(
        text(
            "INSERT INTO collections (id, observer, observed, workspace_name) "
            + "SELECT substr(md5('col' || g::text), 1, 21), "
            + "'o' || lpad(((g - 1) % 100)::text, 2, '0'), "
            + "'d' || lpad((((g - 1) / 100) % 100)::text, 2, '0'), "
            + "'wa' FROM generate_series(1, 10000) AS g"
        )
    )
    await db.execute(
        text(
            "INSERT INTO documents (id, content, observer, observed, "
            + "workspace_name, level, internal_metadata) SELECT "
            + "substr(md5('doc' || g::text), 1, 21), 'c', "
            + "'o' || lpad(((g - 1) % 100)::text, 2, '0'), "
            + "'d' || lpad((((g - 1) / 100) % 100)::text, 2, '0'), "
            + "'wa', 'deductive', "
            + """'{"source_trust":"owner"}'::jsonb """
            + "FROM generate_series(1, 10000) AS g"
        )
    )
    await db.commit()
    await db.execute(text("ANALYZE documents"))
    await db.commit()


async def test_ten_thousand_conclusions_use_the_triple_index(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXPLAIN on 10,000 conclusions uses the composite index."""
    await _seed(db_session)
    stored = (
        await db_session.execute(text("SELECT count(*) FROM documents"))
    ).scalar_one()
    assert stored == _ROWS
    target_count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM documents WHERE "
                + "workspace_name = :workspace AND observer = :observer "
                + "AND observed = :observed"
            ),
            {"workspace": _TARGET[0], "observer": _TARGET[1], "observed": _TARGET[2]},
        )
    ).scalar_one()
    assert target_count == 1

    workspace, observer, observed = _TARGET
    listed = await _plan_async(
        db_session,
        document_crud.get_all_documents(
            workspace, observer=observer, observed=observed
        ),
    )
    assert INDEX in listed, listed

    _require(
        await _plans_of(
            db_session,
            lambda: document_crud.query_documents_recent(
                db_session, workspace, observer=observer, observed=observed
            ),
        )
    )
    _require(
        await _plans_of(
            db_session,
            lambda: replace_peer_card_from_conclusions(
                db_session, workspace, observer=observer, observed=observed
            ),
        )
    )

    @asynccontextmanager
    async def _same_session(_operation: str | None = None, *, read_only: bool = False):
        del _operation, read_only
        yield db_session

    monkeypatch.setattr("src.dependencies.tracked_db", _same_session)
    _require(
        await _plans_of(
            db_session,
            lambda: collection_run_trust(
                workspace, observer=observer, observed=observed
            ),
        )
    )

    dimensions = settings.EMBEDDING.VECTOR_DIMENSIONS
    created = schemas.DocumentCreate(
        content="c",
        level="deductive",
        metadata=schemas.DocumentMetadata(
            message_ids=[1],
            message_created_at="2026-01-01T00:00:00Z",
        ),
        embedding=[1.0] + [0.0] * (dimensions - 1),
    )
    _require(
        await _plans_of(
            db_session,
            lambda: document_crud._pgvector_dup_candidates(  # pyright: ignore[reportPrivateUsage]
                db_session,
                [created],
                workspace,
                observer=observer,
                observed=observed,
            ),
        )
    )

    target_id = await db_session.scalar(
        select(models.Document.id)
        .where(models.Document.workspace_name == workspace)
        .where(models.Document.observer == observer)
        .where(models.Document.observed == observed)
        .limit(1)
    )
    assert isinstance(target_id, str)
    removed = await _plans_of(
        db_session,
        lambda: document_crud.delete_document(
            db_session,
            workspace,
            target_id,
            observer=observer,
            observed=observed,
        ),
    )
    assert removed, removed
    triple_only = update(models.Document).where(
        models.Document.workspace_name == workspace,
        models.Document.observer == observer,
        models.Document.observed == observed,
        models.Document.deleted_at.is_(None),
    )
    update_plan = await _plan_async(db_session, triple_only)
    assert INDEX in update_plan, update_plan
    if INDEX not in removed[0]:
        assert "pkey" in removed[0] or "pk_documents" in removed[0]


async def _plan_async(db: AsyncSession, statement: Any) -> str:
    connection = await db.connection()
    compiled = statement.compile(dialect=connection.dialect)
    explained = await connection.exec_driver_sql(
        "EXPLAIN " + _cast_distance(str(compiled.string)),
        compiled.params,
    )
    return "\n".join(str(row[0]) for row in explained.all())
