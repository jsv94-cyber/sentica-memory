"""Hybrid retrieval and the bounded call brief."""

from __future__ import annotations

import math
import warnings
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src import crud, models, schemas
from src.config import settings
from src.crud.peer_card import set_peer_card
from src.models import Peer, Workspace
from src.sentica.retrieval import prepare_brief, search_conclusions
from src.sentica.workspace_scope import reset_workspace, set_workspace
from src.utils.tokens import estimate_tokens


def _vector(slot: int) -> list[float]:
    values = [0.0] * settings.EMBEDDING.VECTOR_DIMENSIONS
    values[slot] = 1.0
    return values


def _document(
    content: str, *, subject_ref: str | None = None, slot: int = 0
) -> schemas.DocumentCreate:
    return schemas.DocumentCreate(
        content=content,
        session_name="s1",
        subject_ref=subject_ref,
        level="explicit",
        metadata=schemas.DocumentMetadata(
            message_ids=[1],
            message_created_at="2026-01-02T00:00:00Z",
        ),
        embedding=_vector(slot),
    )


async def _ready(
    db_session: AsyncSession, workspace: Workspace, peer: Peer, session_name: str = "s1"
) -> None:
    db_session.add(models.Session(name=session_name, workspace_name=workspace.name))
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db_session.flush()


def _contents(payload: object) -> list[str]:
    rows = cast(list[dict[str, Any]], payload)
    return [str(row["content"]) for row in rows]


async def test_synonym_query_finds_the_stored_fact(
    client: TestClient,
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
    monkeypatch: Any,
) -> None:
    workspace, peer = sample_data

    async def embed(query: str) -> list[float]:
        if "window" in query or "glass" in query:
            return _vector(0)
        return _vector(1)

    async def batch(texts: list[str], **_kwargs: object) -> list[list[float]]:
        return [await embed(text) for text in texts]

    monkeypatch.setattr("src.embedding_client.embedding_client.embed", embed)
    monkeypatch.setattr(
        "src.embedding_client.embedding_client.simple_batch_embed", batch
    )
    created = client.post(
        "/v3/workspaces/" + workspace.name + "/conclusions",
        json={
            "conclusions": [
                {
                    "content": "the customer prefers the window seat",
                    "observer_id": peer.name,
                    "observed_id": peer.name,
                },
                {
                    "content": "the truck uses diesel fuel",
                    "observer_id": peer.name,
                    "observed_id": peer.name,
                },
            ]
        },
    )
    assert created.status_code == 201
    brief = client.post(
        "/v3/workspaces/" + workspace.name + "/peers/" + peer.name + "/brief",
        json={
            "token_budget": 400,
            "query": "likes sitting beside the glass",
            "top_k": 5,
        },
    )
    assert brief.status_code == 200
    body = cast(dict[str, Any], brief.json())
    contents = _contents(body["conclusions"])
    assert contents[0] == "the customer prefers the window seat"
    assert "the truck uses diesel fuel" not in contents[:1]
    stored = (
        await db_session.scalars(
            select(models.Document).where(
                models.Document.content == "the customer prefers the window seat"
            )
        )
    ).one()
    await db_session.refresh(stored)
    assert stored.retrieved_count == 1


async def test_filters_honor_subject_peer_recency_and_supersede(
    db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    other = models.Peer(name="other-peer", workspace_name=workspace.name)
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=other.name,
            observed=other.name,
        )
    )
    await db_session.flush()
    await crud.create_documents(
        db_session,
        [
            _document("prefers the window seat", subject_ref="customer:123", slot=0),
            _document("the job needs a window", subject_ref="job:J-2291", slot=0),
        ],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    await crud.create_documents(
        db_session,
        [_document("other peer likes the window", slot=0)],
        workspace.name,
        observer=other.name,
        observed=other.name,
    )
    old = await crud.create_documents(
        db_session,
        [_document("old calendar entry", slot=1)],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert old.created_documents
    stored_old = (
        await db_session.scalars(
            select(models.Document).where(
                models.Document.content == "old calendar entry"
            )
        )
    ).one()
    stored_old.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    await db_session.commit()
    await crud.create_documents(
        db_session,
        [_document("current calendar entry", slot=1)],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    replaced = await crud.create_documents(
        db_session,
        [_document("kept the window", subject_ref="customer:9", slot=2)],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert replaced.created_documents
    await crud.create_documents(
        db_session,
        [_document("moved to the aisle", subject_ref="customer:9", slot=2)],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )

    by_subject = await search_conclusions(
        db_session,
        workspace.name,
        "window",
        observer=peer.name,
        observed=peer.name,
        subject_ref="customer:123",
        embedding=_vector(0),
        top_k=5,
    )
    assert [doc.content for doc in by_subject] == ["prefers the window seat"]

    by_peer = await search_conclusions(
        db_session,
        workspace.name,
        "window",
        observer=peer.name,
        observed=peer.name,
        embedding=_vector(0),
        top_k=10,
    )
    assert "other peer likes the window" not in [doc.content for doc in by_peer]

    by_time = await search_conclusions(
        db_session,
        workspace.name,
        "calendar",
        observer=peer.name,
        observed=peer.name,
        since=datetime(2024, 1, 1, tzinfo=UTC),
        embedding=_vector(1),
        top_k=5,
    )
    timed = [doc.content for doc in by_time]
    assert timed == ["current calendar entry"]

    current = await search_conclusions(
        db_session,
        workspace.name,
        "window",
        observer=peer.name,
        observed=peer.name,
        subject_ref="customer:9",
        embedding=_vector(2),
        top_k=5,
    )
    assert [doc.content for doc in current] == ["moved to the aisle"]


async def test_workspace_set_stays_inside_each_workspace(
    client: TestClient,
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
) -> None:
    workspace, peer = sample_data
    other = models.Workspace(name="ws-other-brief")
    db_session.add(other)
    await db_session.flush()
    db_session.add(models.Peer(name=peer.name, workspace_name=other.name))
    await db_session.commit()
    for name, content in (
        (workspace.name, "alpha keeps a window seat"),
        (other.name, "beta keeps a door key"),
    ):
        created = client.post(
            "/v3/workspaces/" + name + "/conclusions",
            json={
                "conclusions": [
                    {
                        "content": content,
                        "observer_id": peer.name,
                        "observed_id": peer.name,
                    }
                ]
            },
        )
        assert created.status_code == 201
    response = client.post(
        "/v3/workspaces/" + workspace.name + "/peers/" + peer.name + "/brief",
        json={
            "token_budget": 400,
            "query": "keeps",
            "workspaces": [other.name],
        },
    )
    assert response.status_code == 200
    payload = cast(list[dict[str, Any]], response.json())
    assert [row["workspace_id"] for row in payload] == [workspace.name, other.name]
    by_workspace = {
        str(row["workspace_id"]): _contents(row["conclusions"]) for row in payload
    }
    assert by_workspace[workspace.name] == ["alpha keeps a window seat"]
    assert by_workspace[other.name] == ["beta keeps a door key"]


async def test_brief_fits_the_token_budget(
    client: TestClient,
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    session = (
        await db_session.scalars(
            select(models.Session).where(models.Session.name == "s1")
        )
    ).one()
    session.internal_metadata = {
        "summaries": {
            "short": {
                "content": "summary " + ("detail " * 40),
                "created_at": "2026-10-01T00:00:00Z",
                "message_id": 1,
                "token_count": 40,
            }
        }
    }
    await db_session.execute(
        models.session_peers_table.insert().values(
            workspace_name=workspace.name,
            session_name="s1",
            peer_name=peer.name,
            joined_at=datetime.now(UTC),
        )
    )
    await set_peer_card(
        db_session,
        workspace.name,
        ["greet the caller by name"],
        observer=peer.name,
        observed=peer.name,
    )
    await crud.create_documents(
        db_session,
        [
            _document("prefers the window seat", slot=0),
            _document("drives a blue truck on tuesdays", slot=1),
        ],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    stale = await crud.create_documents(
        db_session,
        [_document("the old address is out of date", slot=3)],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert stale.created_documents
    stored = (
        await db_session.scalars(
            select(models.Document).where(
                models.Document.content == "the old address is out of date"
            )
        )
    ).one()
    stored.stale = True
    await db_session.commit()

    card_tokens = estimate_tokens("greet the caller by name")
    fact_tokens = estimate_tokens("prefers the window seat")
    tight = client.post(
        "/v3/workspaces/" + workspace.name + "/peers/" + peer.name + "/brief",
        json={
            "token_budget": card_tokens + fact_tokens,
            "query": "window seat",
            "top_k": 5,
        },
    )
    assert tight.status_code == 200
    tight_body = cast(dict[str, Any], tight.json())
    assert tight_body["card"] == ["greet the caller by name"]
    assert _contents(tight_body["conclusions"]) == ["prefers the window seat"]
    assert tight_body["summary"] is None
    assert tight_body["stale"] == []
    assert tight_body["tokens_used"] <= card_tokens + fact_tokens

    wide = client.post(
        "/v3/workspaces/" + workspace.name + "/peers/" + peer.name + "/brief",
        json={"token_budget": 800, "query": "window seat", "top_k": 5},
    )
    assert wide.status_code == 200
    wide_body = cast(dict[str, Any], wide.json())
    assert wide_body["summary"] is not None
    assert str(wide_body["summary"]).startswith("summary ")
    stale_rows = cast(list[dict[str, Any]], wide_body["stale"])
    assert [row["content"] for row in stale_rows] == ["the old address is out of date"]
    assert wide_body["tokens_used"] <= 800


async def test_context_latency_on_ten_thousand_conclusions(
    db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db_session.flush()
    dimensions = settings.EMBEDDING.VECTOR_DIMENSIONS
    near = "[" + ",".join(["0"] * (dimensions - 1) + ["1"]) + "]"
    far = "[" + ",".join(["1"] + ["0"] * (dimensions - 1)) + "]"
    await db_session.execute(
        text(
            "INSERT INTO documents (id, content, embedding, observer, observed, workspace_name) "
            + "SELECT 'm' || lpad(g::text, 20, '0'), "
            + "CASE WHEN g = 1 THEN 'the customer prefers the window seat' "
            + "ELSE 'parking note ' || g::text END, "
            + "CASE WHEN g = 1 THEN CAST(:near AS vector) ELSE CAST(:far AS vector) END, "
            + ":peer, :peer, :workspace FROM generate_series(1, 10000) AS g"
        ),
        {
            "near": near,
            "far": far,
            "peer": peer.name,
            "workspace": workspace.name,
        },
    )
    await db_session.commit()
    token = set_workspace(workspace.name)
    try:
        started = perf_counter()
        first = await prepare_brief(
            workspace.name,
            peer.name,
            token_budget=400,
            query="window seat",
            embedding=_vector(dimensions - 1),
        )
        cold = perf_counter() - started
        warm: list[float] = []
        for _ in range(20):
            started = perf_counter()
            await prepare_brief(
                workspace.name,
                peer.name,
                token_budget=400,
                query="window seat",
                embedding=_vector(dimensions - 1),
            )
            warm.append(perf_counter() - started)
    finally:
        reset_workspace(token)
    ordered = sorted(warm)
    index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    warm_p95 = ordered[index]
    warnings.warn(
        f"context cold {cold * 1000:.1f} ms, warm p95 {warm_p95 * 1000:.1f} ms",
        UserWarning,
        stacklevel=1,
    )
    assert [item.content for item in first.conclusions] == [
        "the customer prefers the window seat"
    ]
    assert warm_p95 < 0.3


async def test_search_memory_returns_the_keyword_hit_with_its_workspace(
    client: TestClient,
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
    monkeypatch: Any,
) -> None:
    """The broker route is hybrid search, and each hit names its workspace."""
    workspace, peer = sample_data

    async def embed(query: str) -> list[float]:
        del query
        return _vector(0)

    async def batch(texts: list[str], **_kwargs: object) -> list[list[float]]:
        return [await embed(text) for text in texts]

    monkeypatch.setattr("src.embedding_client.embedding_client.embed", embed)
    monkeypatch.setattr(
        "src.embedding_client.embedding_client.simple_batch_embed", batch
    )
    created = client.post(
        "/v3/workspaces/" + workspace.name + "/conclusions",
        json={
            "conclusions": [
                {
                    "content": "the customer prefers the window seat",
                    "observer_id": peer.name,
                    "observed_id": peer.name,
                    "subject_ref": "customer:123",
                    "source_trust": "external",
                }
            ]
        },
    )
    assert created.status_code == 201, created.text
    created_rows = cast(list[dict[str, Any]], created.json())
    assert created_rows[0]["workspace_id"] == workspace.name
    found = client.post(
        "/v3/workspaces/" + workspace.name + "/peers/" + peer.name + "/search_memory",
        json={"query": "window seat", "subject_ref": "customer:123", "top_k": 5},
    )
    assert found.status_code == 200, found.text
    rows = cast(list[dict[str, Any]], found.json())
    assert rows[0]["content"] == "the customer prefers the window seat"
    assert rows[0]["workspace_id"] == workspace.name
    assert rows[0]["subject_ref"] == "customer:123"
    assert rows[0]["source_trust"] == "external"
    assert rows[0]["observer_id"] == peer.name
    blank = client.post(
        "/v3/workspaces/" + workspace.name + "/peers/" + peer.name + "/search_memory",
        json={"query": "   "},
    )
    assert blank.status_code == 422
    stored = (
        await db_session.scalars(
            select(models.Document).where(
                models.Document.content == "the customer prefers the window seat"
            )
        )
    ).one()
    await db_session.refresh(stored)
    assert stored.retrieved_count == 1


async def test_search_memory_refuses_a_workspace_before_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.exceptions import AuthenticationException
    from src.routers.peers import search_peer_memory
    from src.security import JWTParams

    monkeypatch.setattr("src.sentica.runtime.production_enabled", lambda: True)
    calls: list[str] = []

    async def embed(query: str) -> list[float]:
        calls.append(query)
        return _vector(0)

    monkeypatch.setattr("src.routers.peers.embedding_client.embed", embed)
    with pytest.raises(AuthenticationException, match="outside the token"):
        await search_peer_memory(
            workspace_id="general",
            peer_id="p1",
            body=schemas.MemorySearchRequest(
                query="window seat",
                workspaces=["other"],
            ),
            jwt_params=JWTParams(w="general", workspaces=["general"]),
        )
    assert calls == []


async def test_brief_refuses_a_workspace_before_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.exceptions import AuthenticationException
    from src.routers.peers import get_peer_brief
    from src.security import JWTParams

    monkeypatch.setattr("src.sentica.runtime.production_enabled", lambda: True)
    calls: list[str] = []

    async def embed(query: str) -> list[float]:
        calls.append(query)
        return _vector(0)

    monkeypatch.setattr("src.routers.peers.embedding_client.embed", embed)
    with pytest.raises(AuthenticationException, match="outside the token"):
        await get_peer_brief(
            workspace_id="general",
            peer_id="p1",
            body=schemas.BriefRequest(
                token_budget=400,
                query="window seat",
                workspaces=["other"],
            ),
            jwt_params=JWTParams(w="general", workspaces=["general"]),
        )
    assert calls == []
