"""NOT [{}] on a conclusion read matches no row inside one workspace."""

from __future__ import annotations

import uuid
from typing import Any, LiteralString, cast

import psycopg
from nanoid import generate as generate_nanoid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src import models
from src.crud.document import get_documents_with_filters
from src.sentica.workspace_scope import workspace_policy_sql


def _dsn(engine: AsyncEngine) -> str:
    """Sync URL for the test database. The password stays out of errors."""
    rendered = engine.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    if not rendered.startswith("postgresql://"):
        raise RuntimeError("test database url is not postgresql")
    return rendered


def _exec(dsn: str, statement: str) -> None:
    # Identifiers are checked before they are concatenated into statement.
    query = cast(LiteralString, statement)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(query)


def _parts(sql: str) -> list[str]:
    return [part.strip() for part in sql.split(";") if part.strip()]


async def _ids(
    session: AsyncSession,
    workspace_name: str,
    filters: dict[str, Any] | None,
) -> list[str]:
    statement = get_documents_with_filters(workspace_name, filters=filters)
    result = await session.execute(statement)
    return [row.id for row in result.scalars().all()]


async def _plant(
    db_session: AsyncSession,
    workspace: models.Workspace,
    peer: models.Peer,
) -> models.Document:
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    document = models.Document(
        workspace_name=workspace.name,
        observer=peer.name,
        observed=peer.name,
        content="stored-fact",
        level="explicit",
    )
    db_session.add(document)
    await db_session.flush()
    return document


async def test_not_empty_conclusion_filter_returns_no_rows(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    sample_data: tuple[models.Workspace, models.Peer],
) -> None:
    """NOT [{}] returns no conclusions inside the caller's workspace.

    The same non-superuser session sees the planted conclusion when the
    filter is absent, and sees nothing when the workspace setting is empty.
    Replacing the empty-object TRUE result with a dropped condition makes
    NOT [{}] return the planted conclusion.
    """
    workspace, peer = sample_data
    planted = await _plant(db_session, workspace, peer)
    other_workspace = models.Workspace(name=generate_nanoid())
    other_peer = models.Peer(
        name=generate_nanoid(), workspace_name=other_workspace.name
    )
    db_session.add(other_workspace)
    db_session.add(other_peer)
    await db_session.flush()
    other = await _plant(db_session, other_workspace, other_peer)
    await db_session.commit()
    planted_id = planted.id
    other_id = other.id

    schema = models.Document.__table__.schema or "public"
    if not schema.replace("_", "").isalnum():
        raise RuntimeError("unexpected schema")
    role = "r6flt" + uuid.uuid4().hex[:12]
    if not role.isalnum():
        raise RuntimeError("unexpected role")
    dsn = _dsn(db_engine)
    table = '"' + schema + '".documents'
    quoted_schema = '"' + schema + '"'
    created = False
    try:
        _exec(dsn, "CREATE ROLE " + role + " NOLOGIN NOSUPERUSER NOBYPASSRLS")
        created = True
        for statement in _parts(workspace_policy_sql(schema, "documents")):
            _exec(dsn, statement)
        _exec(
            dsn,
            "GRANT SELECT ON ALL TABLES IN SCHEMA " + quoted_schema + " TO " + role,
        )
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as session:
            try:
                await session.execute(text("SET ROLE " + role))
                identity = (
                    await session.execute(
                        text(
                            "SELECT current_user, rolsuper, rolbypassrls "
                            + "FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).one()
                assert identity[0] == role
                assert bool(identity[1]) is False
                assert bool(identity[2]) is False
                await session.execute(
                    text("SELECT set_config('sentica.workspace', '', true)")
                )
                assert await _ids(session, workspace.name, None) == []
                await session.execute(
                    text("SELECT set_config('sentica.workspace', :name, true)"),
                    {"name": workspace.name},
                )
                visible = await _ids(session, workspace.name, None)
                assert visible == [planted_id]
                assert other_id not in visible
                assert await _ids(session, workspace.name, {"level": "explicit"}) == [
                    planted_id
                ]
                assert await _ids(session, workspace.name, {"NOT": [{}]}) == []
            finally:
                await session.rollback()
                await session.execute(text("RESET ROLE"))
                await session.rollback()
    finally:
        _exec(dsn, "ALTER TABLE " + table + " NO FORCE ROW LEVEL SECURITY")
        _exec(dsn, "ALTER TABLE " + table + " DISABLE ROW LEVEL SECURITY")
        _exec(dsn, "DROP POLICY IF EXISTS sentica_workspace ON " + table)
        if created:
            _exec(dsn, "DROP OWNED BY " + role)
            _exec(dsn, "DROP ROLE " + role)
