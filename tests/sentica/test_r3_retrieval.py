"""HNSW order and the concurrent-index guard for new migrations."""

from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql import Select

from src import models
from src.config import settings
from src.sentica.vector_query import (
    CANDIDATE_FACTOR,
    execute_neighbors,
    neighbor_statement,
)

# Migrations that were already applied. New files are the ones outside this set.
_FROZEN_MIGRATIONS = {
    "05486ce795d5_make_session_name_required_on_messages.py",
    "066e87ca5b07_align_schema_with_declarative_models.py",
    "08894082221a_replace_collection_name_with_observer_.py",
    "110bdf470272_rename_deriver_disabled_to_deriver_.py",
    "119a52b73c60_support_external_embeddings.py",
    "20f89a421aff_rename_metamessage_type_to_label.py",
    "29ade7350c19_remove_document_level_valid_constraint.py",
    "3e7a1c9d5b20_index_messages_content_trgm.py",
    "556a16564f50_add_user_id_and_app_id_to_tables.py",
    "564ba40505c5_add_session_name_column_to_documents.py",
    "66e63cf2cf77_add_indexes_to_documents_table.py",
    "76ffba56fe8c_add_error_field_to_queueitem.py",
    "7c0d9a4e3b1f_add_unique_index_for_pending_dreams.py",
    "88b0fb10906f_add_webhooks_table.py",
    "917195d9b5e9_add_messageembedding_table.py",
    "a1b2c3d4e5f6_initial_schema.py",
    "a7c3e9f1b2d4_add_document_sources_table.py",
    "a7d4e2c91b08_conclusion_freshness.py",
    "a9c1e3f5b7d2_workspace_row_level_security.py",
    "b765d82110bd_change_metamessages_to_user_level_with_.py",
    "b8183c5ffb48_codify_document_level_and_times_derived.py",
    "b8d2f4a6c9e1_index_session_peers_by_peer.py",
    "b8e3c1a94d20_conclusion_keyword_index.py",
    "baa22cad81e2_standardize_constraint_names.py",
    "bb6fb3a7a643_add_message_seq_in_session_column.py",
    "c3828084f472_add_indexes_for_messages_and_.py",
    "c4e8b1a2d6f0_runtime_role_and_pending_counts.py",
    "c9d4e2a71b58_digests_and_deletion_records.py",
    "d429de0e5338_adopt_peer_paradigm.py",
    "e4eba9cfaa6f_make_document_session_name_nullable.py",
    "e6b2d8f04a17_dreams_due_superset.py",
    "e9b705f9adf9_add_server_defaults_to_timestamp_.py",
    "ec8f94139b02_codify_workspace_name_and_message_id_in_.py",
    "f1a2b3c4d5e6_add_reasoning_tree_columns.py",
    "f3c8a1d94e27_message_source_trust.py",
}


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return None


def _owns_transaction(test: ast.AST) -> bool:
    return (
        isinstance(test, ast.Call)
        and _call_name(test.func) == "_caller_owns_transaction"
    )


def _constant_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _every_call_passes_true(tree: ast.AST, func_name: str, param: str) -> bool:
    """True when each call of ``func_name`` passes ``param=True``."""
    seen = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != func_name:
            continue
        seen = True
        passed = False
        for keyword in node.keywords:
            if keyword.arg != param:
                continue
            if isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                passed = True
        if not passed:
            return False
    return seen


def _index_call_is_concurrent(
    node: ast.Call,
    funcs: list[ast.FunctionDef | ast.AsyncFunctionDef],
    tree: ast.AST,
) -> bool:
    for keyword in node.keywords:
        if keyword.arg != "postgresql_concurrently":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and value.value is True:
            return True
        name = _call_name(value)
        if name is None or not funcs:
            return False
        params = [arg.arg for arg in (*funcs[-1].args.args, *funcs[-1].args.kwonlyargs)]
        if name not in params:
            return False
        return _every_call_passes_true(tree, funcs[-1].name, name)
    return False


def _nonconcurrent_indexes(source: str) -> list[str]:
    """Index builds outside the caller-transaction fallback that are not concurrent.

    A plain ``op.create_index`` inside ``if _caller_owns_transaction()`` is
    the autocommit fallback and is allowed. Every other ``op.create_index``
    must pass ``postgresql_concurrently=True``, or a parameter that every
    call site sets to True. A SQL string that starts a ``CREATE INDEX`` must
    include ``CONCURRENTLY``. Docstrings that only mention the words are left
    alone because they do not start with the statement.
    """
    tree = ast.parse(source)
    problems: list[str] = []

    class _Checker(ast.NodeVisitor):
        def __init__(self) -> None:
            self.funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._push(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._push(node)

        def _push(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.funcs.append(node)
            self.generic_visit(node)
            self.funcs.pop()

        def visit_If(self, node: ast.If) -> None:
            if _owns_transaction(node.test):
                for stmt in node.orelse:
                    self.visit(stmt)
                return
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "create_index"
                and _call_name(func.value) == "op"
                and not _index_call_is_concurrent(node, self.funcs, tree)
            ):
                label = _constant_text(node.args[0]) if node.args else None
                problems.append(label or "op.create_index line " + str(node.lineno))
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str):
                stripped = " ".join(node.value.split()).upper()
                starts_index = stripped.startswith(
                    ("CREATE INDEX", "CREATE UNIQUE INDEX")
                )
                if starts_index and "CONCURRENTLY" not in stripped:
                    problems.append(stripped.split(" ON ", 1)[0])
            self.generic_visit(node)

    _Checker().visit(tree)
    return problems


def test_new_index_migrations_use_create_index_concurrently() -> None:
    """Applied migrations stay frozen. Each later index build is concurrent."""
    root = Path("migrations/versions")
    seen_new = False
    for path in sorted(root.glob("*.py")):
        if path.name in _FROZEN_MIGRATIONS:
            continue
        seen_new = True
        source = path.read_text(encoding="utf-8")
        found = _nonconcurrent_indexes(source)
        assert not found, path.name + ": " + ", ".join(found)
    assert seen_new


def test_index_guard_rejects_a_plain_index_beside_a_concurrent_one() -> None:
    """One concurrent build must not hide a second plain build."""
    source = (
        "def upgrade():\n"
        "    op.create_index(\n"
        "        'ix_ok', 'documents', ['embedding'],\n"
        "        postgresql_concurrently=True,\n"
        "    )\n"
        "    op.create_index('ix_plain', 'documents', ['embedding'])\n"
        "    statement = 'CREATE INDEX ix_sql ON documents (embedding)'\n"
    )
    found = _nonconcurrent_indexes(source)
    assert "ix_plain" in found
    assert "CREATE INDEX IX_SQL" in found
    assert "ix_ok" not in found


def test_index_guard_allows_the_caller_transaction_fallback() -> None:
    """The plain build inside the owned-transaction branch stays allowed."""
    source = (
        "def _caller_owns_transaction():\n"
        "    return True\n"
        "\n"
        "def _create_index(*, concurrently):\n"
        "    op.create_index(\n"
        "        'ix_current', 'documents', ['embedding'],\n"
        "        postgresql_concurrently=concurrently,\n"
        "    )\n"
        "\n"
        "def upgrade():\n"
        '    """A plain CREATE INDEX is the fallback."""\n'
        "    if _caller_owns_transaction():\n"
        "        op.create_index('ix_current', 'documents', ['embedding'])\n"
        "        return\n"
        "    _create_index(concurrently=True)\n"
    )
    assert _nonconcurrent_indexes(source) == []


async def _plans_during(
    db_session: AsyncSession,
    call: Callable[[], Awaitable[list[models.Document]]],
) -> tuple[list[models.Document], str]:
    """Explain the neighbor query while ``execute_neighbors`` holds its settings."""
    plans: list[str] = []
    original = db_session.execute

    async def _watching(statement: Any, *args: Any, **kwargs: Any) -> Any:
        query = statement
        if isinstance(query, Select) and not plans:
            plans.append(
                await _plan(db_session, cast(Select[tuple[models.Document]], query))
            )
        return await original(statement, *args, **kwargs)

    db_session.execute = _watching  # type: ignore[method-assign]
    try:
        rows = await call()
    finally:
        db_session.execute = original  # type: ignore[method-assign]
    assert plans, "the neighbor query did not run"
    return rows, plans[0]


async def _plan(db_session: AsyncSession, stmt: Select[tuple[models.Document]]) -> str:
    """EXPLAIN a neighbor statement, casting the distance bind to vector."""
    connection = await db_session.connection()
    compiled = stmt.compile(dialect=connection.dialect)
    # exec_driver_sql sends the embedding as float[]. pgvector only defines
    # <=> for the vector type, so the distance bind has to be cast.
    explained_sql = compiled.string.replace(
        " <=> %(",
        " <=> CAST(%(",
        1,
    ).replace(
        ")s \n",
        ")s AS vector)\n",
        1,
    )
    explained = await connection.exec_driver_sql(
        "EXPLAIN " + explained_sql,
        compiled.params,
    )
    return "\n".join(str(row[0]) for row in explained.all())


def _biased_embedding_sql(dimensions: int) -> str:
    """Vector text for row ``g``.

    Rows with ``g % 12 <> 0`` put about 0.8 on the first component, so they
    sit next to the query ``[1, 0, ...]``. Target rows stay near the origin.
    A post-filtered HNSW scan that stops at ``ef_search`` never reaches them.
    """
    zeros = ",".join(["0"] * (dimensions - 16))
    return (
        "'[' || (SELECT string_agg("
        + "CASE WHEN i = 1 AND g % 12 <> 0 THEN "
        + "(get_byte(decode(md5(g::text || ':' || i::text), 'hex'), 0)::float8 "
        + "/ 255.0) * 0.2 + 0.8 ELSE "
        + "(get_byte(decode(md5(g::text || ':' || i::text), 'hex'), 0)::float8 "
        + "/ 255.0) * 0.2 END::text, ',' ORDER BY i) "
        + "FROM generate_series(1, 16) AS i) || ',"
        + zeros
        + "]'"
    )


async def test_vector_query_uses_hnsw_at_10k_rows(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
) -> None:
    """EXPLAIN at 10k current rows is an HNSW index scan."""
    workspace, peer = sample_data
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db_session.commit()
    dimensions = settings.EMBEDDING.VECTOR_DIMENSIONS
    vector = "[" + ",".join(["0"] * (dimensions - 1) + ["1"]) + "]"
    await db_session.execute(
        text(
            "INSERT INTO documents (id, content, observer, observed, "
            + "workspace_name, embedding, stale, level, internal_metadata) "
            + "SELECT substr(md5(g::text || :salt), 1, 21), 'row ' || g, "
            + ":observer, :observed, :workspace, CAST(:vector AS vector), "
            + "false, 'explicit', '{}'::jsonb FROM generate_series(1, 10000) AS g"
        ),
        {
            "salt": workspace.name,
            "observer": peer.name,
            "observed": peer.name,
            "workspace": workspace.name,
            "vector": vector,
        },
    )
    await db_session.execute(text("ANALYZE documents"))
    await db_session.commit()
    embedding = [0.0] * (dimensions - 1) + [1.0]
    stmt = neighbor_statement(
        select(models.Document).where(models.Document.workspace_name == workspace.name),
        embedding,
        10,
    )
    plan = await _plan(db_session, stmt)
    folded = plan.lower()
    assert "index scan" in folded or "bitmap index scan" in folded
    assert "hnsw" in folded


async def test_filtered_hnsw_returns_a_full_page_at_10k_rows(
    db_session: AsyncSession,
) -> None:
    """LIMIT 40 on a selective filter returns 40 and still uses HNSW.

    Ten thousand rows span four workspaces and two peers. About 830 of them
    match the queried workspace and peer, and those matches are farther from
    the query than the other rows. Without iterative scan the index returns
    a handful of candidates and the workspace filter throws them all away.
    """
    from nanoid import generate as generate_nanoid

    dimensions = settings.EMBEDDING.VECTOR_DIMENSIONS
    assert dimensions >= 16
    workspaces = [models.Workspace(name=str(generate_nanoid())) for _ in range(4)]
    db_session.add_all(workspaces)
    for workspace in workspaces:
        for peer_name in ("p0", "p1"):
            db_session.add(models.Peer(name=peer_name, workspace_name=workspace.name))
    await db_session.commit()
    for workspace in workspaces:
        for peer_name in ("p0", "p1"):
            db_session.add(
                models.Collection(
                    workspace_name=workspace.name,
                    observer=peer_name,
                    observed=peer_name,
                )
            )
    await db_session.commit()
    target = workspaces[0]
    await db_session.execute(
        text(
            "INSERT INTO documents (id, content, observer, observed, "
            + "workspace_name, embedding, stale, level, internal_metadata) "
            + "SELECT substr(md5(g::text || :salt), 1, 21), 'row ' || g, "
            + "CASE WHEN g % 2 = 0 THEN :p0 ELSE :p1 END, "
            + "CASE WHEN g % 2 = 0 THEN :p0 ELSE :p1 END, "
            + "CASE WHEN g % 12 = 0 THEN :w0 WHEN g % 3 = 0 THEN :w1 "
            + "WHEN g % 3 = 1 THEN :w2 ELSE :w3 END, "
            + "CAST("
            + _biased_embedding_sql(dimensions)
            + " AS vector), false, 'explicit', '{}'::jsonb "
            + "FROM generate_series(1, 10000) AS g"
        ),
        {
            "salt": target.name,
            "p0": "p0",
            "p1": "p1",
            "w0": target.name,
            "w1": workspaces[1].name,
            "w2": workspaces[2].name,
            "w3": workspaces[3].name,
        },
    )
    await db_session.execute(text("ANALYZE documents"))
    await db_session.commit()
    matched = await db_session.scalar(
        select(func.count())
        .select_from(models.Document)
        .where(models.Document.workspace_name == target.name)
        .where(models.Document.observer == "p0")
        .where(models.Document.observed == "p0")
    )
    assert matched is not None and matched >= 40
    page = 40
    assert page % CANDIDATE_FACTOR == 0
    limit = page // CANDIDATE_FACTOR
    embedding = [1.0] + [0.0] * (dimensions - 1)
    scoped = select(models.Document).where(
        models.Document.workspace_name == target.name,
        models.Document.observer == "p0",
        models.Document.observed == "p0",
    )
    await db_session.rollback()
    plans = await _plans_during(
        db_session,
        lambda: execute_neighbors(db_session, scoped, embedding, limit),
    )
    rows, plan = plans
    folded = plan.lower()
    assert "index scan" in folded or "bitmap index scan" in folded
    assert "hnsw" in folded
    assert "seq scan" not in folded
    assert len(rows) == page
    iterative = await db_session.scalar(
        text("SELECT current_setting('hnsw.iterative_scan')")
    )
    seqscan = await db_session.scalar(text("SELECT current_setting('enable_seqscan')"))
    sort = await db_session.scalar(text("SELECT current_setting('enable_sort')"))
    assert iterative == "off"
    assert seqscan == "on"
    assert sort == "on"


async def test_neighbor_scan_after_a_large_delete_returns_a_full_page(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """Live rows stay visible after a large delete leaves dead HNSW entries.

    The cleanup does not vacuum. A short index page has to fall back to an
    exact scan, or the next insert is missing from the page.
    """
    from tests.conftest import _clear_all_tables  # pyright: ignore[reportPrivateUsage]

    dimensions = settings.EMBEDDING.VECTOR_DIMENSIONS
    assert dimensions >= 16
    workspace = models.Workspace(name="hnswdead000000000000")
    db_session.add(workspace)
    await db_session.commit()
    db_session.add(models.Peer(name="p0", workspace_name=workspace.name))
    await db_session.commit()
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer="p0",
            observed="p0",
        )
    )
    await db_session.commit()
    close = "[" + ",".join(["0.9"] + ["0"] * (dimensions - 1)) + "]"
    await db_session.execute(
        text(
            "INSERT INTO documents (id, content, observer, observed, "
            + "workspace_name, embedding, stale, level, internal_metadata) "
            + "SELECT substr(md5('dead' || g::text), 1, 21), 'dead ' || g, "
            + ":observer, :observed, :workspace, CAST(:vector AS vector), "
            + "false, 'explicit', '{}'::jsonb FROM generate_series(1, 1500) AS g"
        ),
        {
            "observer": "p0",
            "observed": "p0",
            "workspace": workspace.name,
            "vector": close,
        },
    )
    await db_session.commit()
    await db_session.rollback()
    await _clear_all_tables(db_engine, vacuum_hnsw=False)

    target = models.Workspace(name="hnswlive000000000000")
    db_session.add(target)
    await db_session.commit()
    db_session.add(models.Peer(name="p0", workspace_name=target.name))
    await db_session.commit()
    db_session.add(
        models.Collection(
            workspace_name=target.name,
            observer="p0",
            observed="p0",
        )
    )
    await db_session.commit()
    far = "[" + ",".join(["0.05"] + ["0.2"] * (dimensions - 1)) + "]"
    await db_session.execute(
        text(
            "INSERT INTO documents (id, content, observer, observed, "
            + "workspace_name, embedding, stale, level, internal_metadata) "
            + "SELECT substr(md5('live' || g::text), 1, 21), 'live ' || g, "
            + ":observer, :observed, :workspace, CAST(:vector AS vector), "
            + "false, 'explicit', '{}'::jsonb FROM generate_series(1, 40) AS g"
        ),
        {
            "observer": "p0",
            "observed": "p0",
            "workspace": target.name,
            "vector": far,
        },
    )
    await db_session.commit()
    page = 40
    limit = page // CANDIDATE_FACTOR
    embedding = [1.0] + [0.0] * (dimensions - 1)
    scoped = select(models.Document).where(
        models.Document.workspace_name == target.name,
        models.Document.observer == "p0",
        models.Document.observed == "p0",
    )
    rows = await execute_neighbors(db_session, scoped, embedding, limit)
    assert len(rows) == page


async def test_top_k_returns_the_fresh_row_ahead_of_a_closer_stale_row(
    db_session: AsyncSession,
) -> None:
    """top_k returns the fresh row when a stale row is closer.

    Fresh neighbors are concatenated ahead of stale ones. Ranking by
    distance alone would cut to the closer stale row.
    """
    from src.crud.document import query_documents

    dimensions = settings.EMBEDDING.VECTOR_DIMENSIONS
    assert dimensions >= 2
    workspace = models.Workspace(name="hnswrank000000000000")
    db_session.add(workspace)
    await db_session.commit()
    db_session.add(models.Peer(name="p0", workspace_name=workspace.name))
    await db_session.commit()
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer="p0",
            observed="p0",
        )
    )
    await db_session.commit()
    query = [1.0] + [0.0] * (dimensions - 1)
    close = [0.9] + [0.0] * (dimensions - 1)
    far = [0.0, 1.0] + [0.0] * (dimensions - 2)
    db_session.add_all(
        [
            models.Document(
                content="far fresh",
                observer="p0",
                observed="p0",
                workspace_name=workspace.name,
                embedding=far,
                stale=False,
                level="explicit",
                internal_metadata={},
            ),
            models.Document(
                content="close stale",
                observer="p0",
                observed="p0",
                workspace_name=workspace.name,
                embedding=close,
                stale=True,
                level="explicit",
                internal_metadata={},
            ),
        ]
    )
    await db_session.commit()
    found = await query_documents(
        db_session,
        workspace.name,
        "rank",
        observer="p0",
        observed="p0",
        embedding=query,
        top_k=1,
    )
    assert [doc.content for doc in found] == ["far fresh"]


async def test_aborted_neighbor_scan_keeps_its_original_error(
    db_session: AsyncSession,
) -> None:
    """A failed scan reports that failure, not the restore that follows it.

    A statement error aborts the transaction. Restoring HNSW settings in
    ``finally`` then fails too, and that second error used to replace the
    division-by-zero that aborted the scan.
    """
    dimensions = settings.EMBEDDING.VECTOR_DIMENSIONS
    embedding = [1.0] + [0.0] * (dimensions - 1)
    stmt = select(models.Document).where(text("1 = (SELECT 1/0)"))
    with pytest.raises(Exception, match="division by zero") as caught:
        await execute_neighbors(db_session, stmt, embedding, 1)
    assert "current transaction is aborted" not in str(caught.value).lower()
