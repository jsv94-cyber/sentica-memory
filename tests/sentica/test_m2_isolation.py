"""HTTP isolation red-team for the runtime role.

These tests run only in the test-m2-rls job. They use signed broker tokens.
Removing the workspace predicate or the sentica_workspace policy fails them.
The negative control still shows the canary when both are removed, so the
harness itself is not weakened.
"""

from __future__ import annotations

import uuid

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


def _workspace_scoped(expression: str) -> bool:
    """The expression names the workspace setting and is not a tautology."""
    if _allows_every_row(expression):
        return False
    folded = expression.lower().replace(" ", "")
    return "workspace_name=current_setting('sentica.workspace'" in folded


def test_workspace_filter_and_rls_policy_are_present() -> None:
    """The retrieval SQL and the documents policy both name the workspace."""
    tautology = "true OR (workspace_name = current_setting('sentica.workspace', true))"
    assert _allows_every_row(tautology)
    assert not _workspace_scoped(tautology)
    statement = _scoped("left", "p1", "p1", subject_ref=None, since=None)
    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    where = compiled.lower().split("where", 1)[1]
    assert "workspace_name" in where
    import psycopg

    from tests.sentica.rls_job import runtime_dsn

    schema = settings.DB.SCHEMA
    with psycopg.connect(runtime_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, "
            + "polrelid) FROM pg_policy WHERE polname = 'sentica_workspace' "
            + "AND polrelid = (%s || '.documents')::regclass",
            (schema,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is not None and _workspace_scoped(row[0])
    assert row[1] is not None and _workspace_scoped(row[1])


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
