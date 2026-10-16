"""Stale flags, superseding, decay, and deletion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src import crud, models, schemas
from src.config import settings
from src.models import Peer, Workspace
from src.sentica.freshness import (
    apply_decay,
    forget,
    note_used,
    purge_expired,
)


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


async def _ready(db_session: AsyncSession, workspace: Workspace, peer: Peer) -> None:
    db_session.add(models.Session(name="s1", workspace_name=workspace.name))
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db_session.flush()


def test_changed_subject_flags_its_conclusions(
    client: TestClient, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    created = client.post(
        "/v3/workspaces/" + workspace.name + "/conclusions",
        json={
            "conclusions": [
                {
                    "content": "likes the window seat",
                    "observer_id": peer.name,
                    "observed_id": peer.name,
                    "subject_ref": "customer:123",
                },
                {
                    "content": "the job is scheduled for Monday",
                    "observer_id": peer.name,
                    "observed_id": peer.name,
                    "subject_ref": "job:J-2291",
                },
            ]
        },
    )
    assert created.status_code == 201
    payload: Any = created.json()
    assert isinstance(payload, list)
    rows = cast(list[dict[str, Any]], payload)
    customer = next(row for row in rows if row["subject_ref"] == "customer:123")
    changed = client.post(
        "/v3/workspaces/" + workspace.name + "/subjects/customer:123/changed"
    )
    assert changed.status_code == 200
    assert changed.json()["stale_count"] == 1
    flagged = client.get(
        "/v3/workspaces/" + workspace.name + "/conclusions/" + str(customer["id"])
    )
    assert flagged.status_code == 200
    assert flagged.json()["stale"] is True
    job = next(row for row in rows if row["subject_ref"] == "job:J-2291")
    other = client.get(
        "/v3/workspaces/" + workspace.name + "/conclusions/" + str(job["id"])
    )
    assert other.json()["stale"] is False


async def test_update_supersedes_and_retrieval_skips_it(
    db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    old = await crud.create_documents(
        db_session,
        [_document("prefers the window seat", subject_ref="customer:123")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert len(old.created_documents) == 1
    new = await crud.create_documents(
        db_session,
        [_document("prefers the aisle seat", subject_ref="customer:123")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    stored_old = (
        await db_session.scalars(
            select(models.Document).where(
                models.Document.content == "prefers the window seat"
            )
        )
    ).one()
    stored_new = (
        await db_session.scalars(
            select(models.Document).where(
                models.Document.content == "prefers the aisle seat"
            )
        )
    ).one()
    assert stored_old.superseded_by == stored_new.id
    assert stored_new.internal_metadata.get("relation") == "update"
    assert len(new.created_documents) == 1
    found = await crud.query_documents(
        db_session,
        workspace.name,
        "seat",
        observer=peer.name,
        observed=peer.name,
        embedding=_vector(0),
        top_k=5,
    )
    assert [doc.content for doc in found] == ["prefers the aisle seat"]


async def test_contradiction_supersedes_too(
    db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await crud.create_documents(
        db_session,
        [_document("prefers the window seat", subject_ref="customer:123")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    await crud.create_documents(
        db_session,
        [_document("no longer prefers the window seat", subject_ref="customer:123")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    stored_new = (
        await db_session.scalars(
            select(models.Document).where(
                models.Document.content == "no longer prefers the window seat"
            )
        )
    ).one()
    assert stored_new.internal_metadata.get("relation") == "contradiction"


async def test_stale_ranks_after_a_fresh_conclusion(
    db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await crud.create_documents(
        db_session,
        [
            _document("fresh fact"),
            _document("old fact"),
        ],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    old = (
        await db_session.scalars(
            select(models.Document).where(models.Document.content == "old fact")
        )
    ).one()
    old.stale = True
    await db_session.commit()
    found = await crud.query_documents(
        db_session,
        workspace.name,
        "fact",
        observer=peer.name,
        observed=peer.name,
        embedding=_vector(0),
        top_k=1,
    )
    assert [doc.content for doc in found] == ["fresh fact"]


async def test_decay_and_purge_follow_the_schedule(
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
    monkeypatch: object,
) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    monkeypatch.setenv("SENTICA_DECAY_FLOOR", "0.6")
    monkeypatch.setenv("SENTICA_DECAY_RETENTION_DAYS", "30")
    await crud.create_documents(
        db_session,
        [_document("unused fact"), _document("used fact", slot=1)],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    used = (
        await db_session.scalars(
            select(models.Document).where(models.Document.content == "used fact")
        )
    ).one()
    await note_used(db_session, [used.id], workspace_name=workspace.name)
    await db_session.refresh(used)
    now = datetime.now(UTC) + timedelta(days=30)
    used.last_used_at = now
    used.decayed_at = now
    await db_session.commit()
    changed = await apply_decay(db_session, workspace_name=workspace.name, now=now)
    assert changed == 1
    unused = (
        await db_session.scalars(
            select(models.Document).where(models.Document.content == "unused fact")
        )
    ).one()
    used = (
        await db_session.scalars(
            select(models.Document).where(models.Document.content == "used fact")
        )
    ).one()
    assert unused.weight == 0.5
    assert unused.deleted_at is not None
    assert used.deleted_at is None
    assert used.used_count == 1
    unused.deleted_at = now - timedelta(days=31)
    await db_session.commit()
    purged = await purge_expired(db_session, workspace_name=workspace.name, now=now)
    assert purged == 1
    remaining = (
        await db_session.scalars(
            select(models.Document.content).where(
                models.Document.workspace_name == workspace.name
            )
        )
    ).all()
    assert remaining == ["used fact"]


async def test_deletion_leaves_nothing_searchable(
    db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    message = models.Message(
        session_name="s1",
        peer_name=peer.name,
        content="secret window",
        workspace_name=workspace.name,
        token_count=2,
        seq_in_session=1,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(
        models.MessageEmbedding(
            content="secret window",
            embedding=_vector(0),
            message_id=message.public_id,
            workspace_name=workspace.name,
            session_name="s1",
            peer_name=peer.name,
        )
    )
    session = (
        await db_session.scalars(
            select(models.Session).where(models.Session.name == "s1")
        )
    ).one()
    session.internal_metadata = {
        "summaries": {"short": {"message_id": message.id, "content": "secret summary"}}
    }
    await db_session.commit()
    await crud.create_documents(
        db_session,
        [
            schemas.DocumentCreate(
                content="likes the secret window",
                session_name="s1",
                subject_ref="customer:123",
                level="explicit",
                metadata=schemas.DocumentMetadata(
                    message_ids=[message.id],
                    message_public_ids=[message.public_id],
                    message_created_at="2026-01-02T00:00:00Z",
                ),
                embedding=_vector(0),
            )
        ],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    record = await forget(db_session, workspace.name, subject_ref="customer:123")
    assert record.document_count == 1
    assert record.embedding_count == 1
    assert record.summary_count == 1
    from src.sentica.freshness import identifier_hash

    saved = (
        await db_session.scalars(
            select(models.Workspace).where(models.Workspace.name == workspace.name)
        )
    ).one()
    assert "deletion_records" not in (saved.internal_metadata or {})
    stored = (
        await db_session.scalars(
            select(models.DeletionRecord).where(
                models.DeletionRecord.workspace_name == workspace.name
            )
        )
    ).one()
    assert stored.id == record.id
    assert stored.subject_hash == identifier_hash("customer:123")
    assert stored.peer_hash is None
    assert "customer:123" not in (stored.subject_hash or "")
    documents = (
        await db_session.scalars(
            select(models.Document).where(
                models.Document.workspace_name == workspace.name
            )
        )
    ).all()
    embeddings = (
        await db_session.scalars(
            select(models.MessageEmbedding).where(
                models.MessageEmbedding.workspace_name == workspace.name
            )
        )
    ).all()
    messages = (
        await db_session.scalars(
            select(models.Message).where(
                models.Message.workspace_name == workspace.name
            )
        )
    ).all()
    assert documents == []
    assert embeddings == []
    assert messages == []
    stored_session = (
        await db_session.scalars(
            select(models.Session).where(models.Session.name == "s1")
        )
    ).one()
    summaries = stored_session.internal_metadata.get("summaries")
    assert summaries == {}
    found = await crud.query_documents(
        db_session,
        workspace.name,
        "window",
        observer=peer.name,
        observed=peer.name,
        embedding=_vector(0),
        top_k=5,
    )
    assert found == []
