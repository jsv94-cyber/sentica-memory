"""HTTP isolation red-team for the runtime role.

These tests run only in the test-m2-rls job. They use signed broker tokens.
Removing the workspace predicate or the sentica_workspace policy fails them.
The negative control still shows the canary when both are removed, so the
harness itself is not weakened.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import LiteralString, cast

import pytest
from sqlalchemy.dialects import postgresql

from src.config import settings
from src.sentica.retrieval import (
    _scoped,  # pyright: ignore[reportPrivateUsage]
)
from tests.sentica.rls_job import authorize
from tests.sentica.test_m2_partitions import (
    _relax_rls,  # pyright: ignore[reportPrivateUsage]
    _release_locks,  # pyright: ignore[reportPrivateUsage]
    _seed_canary_pair,  # pyright: ignore[reportPrivateUsage]
    _unscoped_documents,  # pyright: ignore[reportPrivateUsage]
)


def _split_top(expression: str, operator: str) -> list[str]:
    """Split on a top-level AND or OR, leaving parenthesized text intact."""
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    index = 0
    needle = operator.upper()
    folded = expression.upper()
    while index < len(expression):
        char = expression[index]
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif (
            depth == 0
            and folded.startswith(needle, index)
            and (index == 0 or not expression[index - 1].isalnum())
            and (
                index + len(needle) >= len(expression)
                or not expression[index + len(needle)].isalnum()
            )
        ):
            parts.append(expression[start:index].strip())
            index += len(needle)
            start = index
            continue
        index += 1
    parts.append(expression[start:].strip())
    return [part for part in parts if part]


def _unwrap(expression: str) -> str:
    text = expression.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        wraps = True
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if depth == 0 and index < len(text) - 1:
                wraps = False
                break
        if not wraps or depth != 0:
            break
        text = text[1:-1].strip()
    return text


def _allows_every_row(expression: str) -> bool:
    """True when a policy expression does not constrain the row."""
    text = _unwrap(expression)
    if text.upper() == "TRUE":
        return True
    branches = _split_top(text, "OR")
    if len(branches) > 1:
        return any(_allows_every_row(part) for part in branches)
    terms = _split_top(text, "AND")
    if len(terms) > 1:
        return all(_allows_every_row(part) for part in terms)
    return False


_CAST = re.compile(r"::[A-Za-z_][A-Za-z0-9_]*")
_WORKSPACE_EQUALITY = "workspace_name=current_setting('sentica.workspace',true)"


def _has_or(expression: str) -> bool:
    """True when OR appears outside quotes."""
    quote = ""
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if expression[index : index + 2].upper() == "OR" and (
            index == 0 or not expression[index - 1].isalnum()
        ):
            after = index + 2
            if after >= len(expression) or not expression[after].isalnum():
                return True
        index += 1
    return False


def _compact_sql(expression: str) -> str:
    """Drop casts and whitespace outside quotes."""
    text = _CAST.sub("", expression).lower()
    parts: list[str] = []
    quote = ""
    for char in text:
        if quote:
            parts.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            parts.append(char)
            continue
        if char.isspace():
            continue
        parts.append(char)
    return "".join(parts)


def _policy_is_single_equality(expression: str) -> bool:
    """One workspace_name equality to the setting, and no OR branch."""
    if not expression or not expression.strip():
        return False
    if _has_or(expression):
        return False
    return _compact_sql(expression).count(_WORKSPACE_EQUALITY) == 1


def _app_workspace_equality(where: str) -> bool:
    """The app filter equates workspace_name to a value. IS NOT NULL does not."""
    if not where or _has_or(where):
        return False
    compact = _compact_sql(where)
    if "workspace_name=" not in compact:
        return False
    return "workspace_name=null" not in compact


def _workspace_scoped(expression: str) -> bool:
    """The policy expression is one workspace equality and not a tautology."""
    if _allows_every_row(expression):
        return False
    return _policy_is_single_equality(expression)


def test_workspace_filter_and_rls_policy_are_present() -> None:
    """The retrieval SQL and the documents policy both name the workspace."""
    tautology = "true OR (workspace_name = current_setting('sentica.workspace', true))"
    assert _allows_every_row(tautology)
    assert not _workspace_scoped(tautology)
    open_policy = (
        "workspace_name = current_setting('sentica.workspace', true) "
        + "OR current_setting('sentica.workspace', true) IS NULL"
    )
    assert not _policy_is_single_equality(open_policy)
    statement = _scoped("left", "p1", "p1", subject_ref=None, since=None)
    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    where = compiled.lower().split("where", 1)[1]
    assert _app_workspace_equality(where)
    assert not _app_workspace_equality(where.replace("= 'left'", "IS NOT NULL"))
    import psycopg

    from tests.sentica.rls_job import runtime_dsn

    schema = settings.DB.SCHEMA
    with psycopg.connect(runtime_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname, pg_get_expr(p.polqual, p.polrelid), "
            + "pg_get_expr(p.polwithcheck, p.polrelid) "
            + "FROM pg_policy AS p "
            + "JOIN pg_class AS c ON c.oid = p.polrelid "
            + "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
            + "WHERE p.polname = 'sentica_workspace' AND n.nspname = %s "
            + "ORDER BY c.relname",
            (schema,),
        )
        rows = cur.fetchall()
    assert rows
    for name, using, check in rows:
        assert using is not None and _policy_is_single_equality(using), name
        assert check is not None and _policy_is_single_equality(check), name


async def test_http_red_team_hides_the_other_workspace(
    client: object, db_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed token's brief does not contain the other workspace canary."""
    from starlette.testclient import TestClient

    assert isinstance(client, TestClient)
    left, _right, canary = await _seed_canary_pair(client, db_session)
    response = client.post(
        f"/v3/workspaces/{left}/peers/p1/brief",
        json={"token_budget": 200},
    )
    assert response.status_code == 200, response.text
    assert canary not in response.text
    assert "local hello" in response.text
    keyword = client.post(
        f"/v3/workspaces/{left}/peers/p1/brief",
        json={"token_budget": 200, "query": "local hello"},
    )
    assert keyword.status_code == 200, keyword.text
    assert canary not in keyword.text
    assert "local hello" in keyword.text
    other = client.post(
        f"/v3/workspaces/{left}/peers/p1/brief",
        json={"token_budget": 200, "query": canary},
    )
    assert other.status_code == 200, other.text
    assert canary not in other.text
    queried = client.post(
        f"/v3/workspaces/{left}/conclusions/query",
        json={
            "query": "local hello",
            "top_k": 5,
            "filters": {"observer": "p1", "observed": "p1"},
        },
    )
    assert queried.status_code == 200, queried.text
    assert canary not in queried.text
    assert "local hello" in queried.text

    async def _shared_vector(_content: str) -> list[float]:
        return [0.25] * settings.EMBEDDING.VECTOR_DIMENSIONS

    monkeypatch.setattr("src.routers.peers.embedding_client.embed", _shared_vector)
    vector = client.post(
        f"/v3/workspaces/{left}/peers/p1/brief",
        json={"token_budget": 800, "query": "zzvectorprobe"},
    )
    assert vector.status_code == 200, vector.text
    assert canary not in vector.text


async def test_removed_filter_and_disabled_rls_shows_the_canary(
    client: object, db_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness still reports a leak when the filter and RLS are removed."""
    from starlette.testclient import TestClient

    assert isinstance(client, TestClient)
    left, _right, canary = await _seed_canary_pair(client, db_session)
    monkeypatch.setattr("src.sentica.retrieval._scoped", _unscoped_documents)
    await _release_locks(db_session)

    async def _shared_vector(_content: str) -> list[float]:
        return [0.25] * settings.EMBEDDING.VECTOR_DIMENSIONS

    with _relax_rls("documents"):
        leaked = client.post(
            f"/v3/workspaces/{left}/peers/p1/brief",
            json={"token_budget": 200},
        )
        keyword = client.post(
            f"/v3/workspaces/{left}/peers/p1/brief",
            json={"token_budget": 200, "query": canary},
        )
        monkeypatch.setattr("src.routers.peers.embedding_client.embed", _shared_vector)
        vector = client.post(
            f"/v3/workspaces/{left}/peers/p1/brief",
            json={"token_budget": 800, "query": "zzvectorprobe"},
        )
        await _release_locks(db_session)
    assert leaked.status_code == 200, leaked.text
    assert canary in leaked.text
    assert keyword.status_code == 200, keyword.text
    assert canary in keyword.text
    assert vector.status_code == 200, vector.text
    assert canary in vector.text


def test_multi_workspace_token_creates_only_in_the_primary(client: object) -> None:
    """A token creates a workspace, peer, session, or webhook only in w."""
    from starlette.testclient import TestClient

    assert isinstance(client, TestClient)
    primary = "p" + uuid.uuid4().hex[:8]
    other = "o" + uuid.uuid4().hex[:8]
    authorize(client, [primary, other], primary=primary)
    created = client.post("/v3/workspaces", json={"name": primary})
    assert created.status_code in (200, 201), created.text
    peer = client.post(f"/v3/workspaces/{primary}/peers", json={"name": "p1"})
    assert peer.status_code in (200, 201), peer.text
    session = client.post(f"/v3/workspaces/{primary}/sessions", json={"id": "s1"})
    assert session.status_code in (200, 201), session.text
    denied_workspace = client.post("/v3/workspaces", json={"name": other})
    assert denied_workspace.status_code == 401
    denied_peer = client.post(f"/v3/workspaces/{other}/peers", json={"name": "p1"})
    assert denied_peer.status_code == 401
    denied_session = client.post(f"/v3/workspaces/{other}/sessions", json={"id": "s1"})
    assert denied_session.status_code == 401
    denied_webhook = client.post(
        f"/v3/workspaces/{other}/webhooks",
        json={"url": "https://example.com/hook"},
    )
    assert denied_webhook.status_code == 401


def test_workspace_policy_rejects_an_or_and_a_null_filter() -> None:
    """An OR with IS NULL, or IS NOT NULL alone, is not a workspace equality."""
    open_policy = (
        "workspace_name = current_setting('sentica.workspace', true) "
        + "OR current_setting('sentica.workspace', true) IS NULL"
    )
    assert not _policy_is_single_equality(open_policy)
    stored = (
        "((COALESCE(current_setting('sentica.workspace'::text, true), ''::text) "
        + "<> ''::text) AND (workspace_name = current_setting("
        + "'sentica.workspace'::text, true)))"
    )
    assert _policy_is_single_equality(stored)
    assert not _app_workspace_equality("workspace_name IS NOT NULL")
    statement = _scoped("left", "p1", "p1", subject_ref=None, since=None)
    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    where = compiled.lower().split("where", 1)[1]
    assert _app_workspace_equality(where)
    weakened = where.replace("= 'left'", "IS NOT NULL")
    assert "is not null" in weakened.lower()
    assert not _app_workspace_equality(weakened)


def _quote_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise AssertionError("unexpected identifier")
    return '"' + name + '"'


def _query(statement: str) -> LiteralString:
    """Dynamic SQL whose identifiers were checked by ``_quote_ident``."""
    return cast(LiteralString, statement)


def test_unset_workspace_sees_zero_rows() -> None:
    """A connection that never sets sentica.workspace cannot read an RLS table."""
    import psycopg

    from tests.sentica.rls_job import runtime_dsn

    schema = settings.DB.SCHEMA
    quoted_schema = _quote_ident(schema)
    admin_dsn = os.environ["SENTICA_RLS_ADMIN_DSN"].replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    workspace_id = "rlsunsetprobe00000001"
    peer_id = "rlsunsetpeer000000001"
    session_id = "rlsunsetsess000000001"
    collection_id = "rlsunsetcoll000000001"
    document_id = "rlsunsetdoc0000000001"
    source_id = "rlsunsetsour000000001"
    digest_id = "rlsunsetdigest0000001"
    deletion_id = "rlsunsetdelet00000001"
    webhook_id = "rlsunsethook000000001"
    public_id = "rlsunsetmsg0000000001"
    workspace = "unsetprobe"
    peer = "punset"
    session = "sunset"
    seeded = (
        "collections",
        "deletion_records",
        "document_sources",
        "documents",
        "dream_digests",
        "message_embeddings",
        "messages",
        "peers",
        "queue",
        "session_peers",
        "sessions",
        "webhook_endpoints",
    )
    inserts = (
        (
            "INSERT INTO " + quoted_schema + ".workspaces (id, name) VALUES (%s, %s)",
            (workspace_id, workspace),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".peers (id, name, workspace_name) VALUES (%s, %s, %s)",
            (peer_id, peer, workspace),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".sessions (id, name, workspace_name) VALUES (%s, %s, %s)",
            (session_id, session, workspace),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".session_peers (workspace_name, session_name, peer_name) "
            + "VALUES (%s, %s, %s)",
            (workspace, session, peer),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".collections (id, observer, observed, workspace_name) "
            + "VALUES (%s, %s, %s, %s)",
            (collection_id, peer, peer, workspace),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".messages (public_id, session_name, content, seq_in_session, "
            + "peer_name, workspace_name) VALUES (%s, %s, %s, %s, %s, %s)",
            (public_id, session, "probe", 1, peer, workspace),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".message_embeddings (content, message_id, workspace_name, "
            + "session_name, peer_name) VALUES (%s, %s, %s, %s, %s)",
            ("probe", public_id, workspace, session, peer),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".documents (id, content, observer, observed, workspace_name) "
            + "VALUES (%s, %s, %s, %s, %s)",
            (document_id, "probe", peer, peer, workspace),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".document_sources (derived_id, source_id, workspace_name) "
            + "VALUES (%s, %s, %s)",
            (document_id, source_id, workspace),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".queue (work_unit_key, task_type, payload, workspace_name) "
            + "VALUES (%s, %s, %s::jsonb, %s)",
            ("unset-probe", "representation", "{}", workspace),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".webhook_endpoints (id, workspace_name, url) VALUES (%s, %s, %s)",
            (webhook_id, workspace, "https://example.com/unset"),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".dream_digests (id, workspace_name, kind, period, content) "
            + "VALUES (%s, %s, %s, %s, %s)",
            (digest_id, workspace, "daily", "2026-10-17", "probe"),
        ),
        (
            "INSERT INTO "
            + quoted_schema
            + ".deletion_records (id, workspace_name, document_count, "
            + "embedding_count, summary_count) VALUES (%s, %s, %s, %s, %s)",
            (deletion_id, workspace, 0, 0, 0),
        ),
    )
    deletes = (
        "document_sources",
        "documents",
        "message_embeddings",
        "messages",
        "queue",
        "dream_digests",
        "deletion_records",
        "webhook_endpoints",
        "session_peers",
        "collections",
        "sessions",
        "peers",
        "workspaces",
    )
    with psycopg.connect(admin_dsn) as admin:
        for statement, params in inserts:
            admin.execute(_query(statement), params)
        admin.commit()
        try:
            discovered = [
                str(row[0])
                for row in admin.execute(
                    _query(
                        "SELECT c.relname FROM pg_policy AS p "
                        + "JOIN pg_class AS c ON c.oid = p.polrelid "
                        + "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                        + "WHERE p.polname = 'sentica_workspace' AND n.nspname = %s "
                        + "ORDER BY c.relname"
                    ),
                    (schema,),
                ).fetchall()
            ]
            assert tuple(discovered) == seeded
            for table in discovered:
                present = admin.execute(
                    _query(
                        "SELECT count(*) FROM "
                        + quoted_schema
                        + "."
                        + _quote_ident(table)
                        + " WHERE workspace_name = %s"
                    ),
                    (workspace,),
                ).fetchone()
                assert present is not None and int(present[0]) >= 1, table
            with psycopg.connect(runtime_dsn()) as runtime, runtime.cursor() as cur:
                cur.execute("SELECT current_setting('sentica.workspace', true)")
                assert cur.fetchone() == (None,)
                for table in discovered:
                    cur.execute(
                        _query(
                            "SELECT count(*) FROM "
                            + quoted_schema
                            + "."
                            + _quote_ident(table)
                        )
                    )
                    seen = cur.fetchone()
                    assert seen == (0,), table
        finally:
            for table in deletes:
                column = "name" if table == "workspaces" else "workspace_name"
                admin.execute(
                    _query(
                        "DELETE FROM "
                        + quoted_schema
                        + "."
                        + _quote_ident(table)
                        + " WHERE "
                        + _quote_ident(column)
                        + " = %s"
                    ),
                    (workspace,),
                )
            admin.commit()
