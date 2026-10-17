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


def test_workspace_filter_and_rls_policy_are_present() -> None:
    """The retrieval SQL and the documents policy both name the workspace."""
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
    assert row[0] is not None and "sentica.workspace" in row[0]
    assert row[1] is not None and "sentica.workspace" in row[1]


async def test_http_red_team_hides_the_other_workspace(
    client: object, db_session: object
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


async def test_removed_filter_and_disabled_rls_shows_the_canary(
    client: object, db_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness still reports a leak when the filter and RLS are removed."""
    from starlette.testclient import TestClient

    assert isinstance(client, TestClient)
    left, _right, canary = await _seed_canary_pair(client, db_session)
    monkeypatch.setattr("src.sentica.retrieval._scoped", _unscoped_documents)
    await _release_locks(db_session)
    with _relax_rls("documents"):
        leaked = client.post(
            f"/v3/workspaces/{left}/peers/p1/brief",
            json={"token_budget": 200},
        )
        await _release_locks(db_session)
    assert leaked.status_code == 200, leaked.text
    assert canary in leaked.text


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
