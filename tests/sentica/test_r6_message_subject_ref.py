"""subject_ref in message metadata reaches deletion and subject-changed."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src import crud, models, schemas
from src.config import settings
from src.models import Peer, Workspace

SUBJECT = "customer:123"
OTHER = "customer:456"


def _vector(slot: int) -> list[float]:
    values = [0.0] * settings.EMBEDDING.VECTOR_DIMENSIONS
    values[slot] = 1.0
    return values


def _ingest(
    client: TestClient,
    workspace_name: str,
    peer_name: str,
    content: str,
    metadata: dict[str, Any] | None,
) -> Any:
    message: dict[str, Any] = {"content": content, "peer_id": peer_name}
    if metadata is not None:
        message["metadata"] = metadata
    return client.post(
        f"/v3/workspaces/{workspace_name}/sessions/s1/messages",
        json={"messages": [message]},
    )


async def _message_contents(db: AsyncSession, workspace_name: str) -> list[str]:
    rows = await db.scalars(
        select(models.Message.content)
        .where(models.Message.workspace_name == workspace_name)
        .order_by(models.Message.id)
    )
    return list(rows.all())


async def _message(
    db: AsyncSession, workspace_name: str, content: str
) -> models.Message:
    return (
        await db.scalars(
            select(models.Message).where(
                models.Message.workspace_name == workspace_name,
                models.Message.content == content,
            )
        )
    ).one()


async def _conclusion(
    db: AsyncSession,
    workspace_name: str,
    peer_name: str,
    message: models.Message,
    content: str,
    slot: int,
) -> models.Document:
    """A conclusion whose provenance cites the message, as the deriver writes it."""
    await crud.create_documents(
        db,
        [
            schemas.DocumentCreate(
                content=content,
                session_name=message.session_name,
                level="explicit",
                metadata=schemas.DocumentMetadata(
                    message_ids=[message.id],
                    message_public_ids=[message.public_id],
                    message_created_at="2026-01-02T00:00:00Z",
                ),
                embedding=_vector(slot),
            )
        ],
        workspace_name,
        observer=peer_name,
        observed=peer_name,
    )
    return (
        await db.scalars(
            select(models.Document).where(
                models.Document.workspace_name == workspace_name,
                models.Document.content == content,
            )
        )
    ).one()


async def _seed(
    client: TestClient, db: AsyncSession, workspace_name: str, peer_name: str
) -> dict[str, models.Document]:
    """One message per case, each with a conclusion citing it."""
    cases: dict[str, tuple[dict[str, Any] | None, int]] = {
        "subject": ({"subject_ref": SUBJECT, "channel": "sms"}, 0),
        "other": ({"subject_ref": OTHER}, 1),
        "plain": (None, 2),
    }
    documents: dict[str, models.Document] = {}
    db.add(
        models.Collection(
            workspace_name=workspace_name, observer=peer_name, observed=peer_name
        )
    )
    await db.commit()
    for name, (metadata, slot) in cases.items():
        response = _ingest(
            client, workspace_name, peer_name, f"note about {name}", metadata
        )
        assert response.status_code == 201, response.text
        message = await _message(db, workspace_name, f"note about {name}")
        documents[name] = await _conclusion(
            db, workspace_name, peer_name, message, f"conclusion about {name}", slot
        )
    return documents


async def _add_child(
    db: AsyncSession, workspace_name: str, peer_name: str, parent: models.Document
) -> None:
    """Something derived from a conclusion, linked by provenance only."""
    db.add(
        models.Document(
            workspace_name=workspace_name,
            observer=peer_name,
            observed=peer_name,
            content="deduced from the subject conclusion",
            level="deductive",
            internal_metadata={"source_ids": [parent.id]},
            session_name="s1",
            embedding=_vector(3),
        )
    )
    await db.commit()


async def _document_contents(db: AsyncSession, workspace_name: str) -> set[str]:
    rows = await db.scalars(
        select(models.Document.content).where(
            models.Document.workspace_name == workspace_name
        )
    )
    return set(rows.all())


async def test_subject_ref_is_stored_for_lookup(
    client: TestClient, db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    workspace_name, peer_name = workspace.name, peer.name
    with_ref = _ingest(
        client, workspace_name, peer_name, "has ref", {"subject_ref": SUBJECT}
    )
    without = _ingest(client, workspace_name, peer_name, "no ref", None)
    assert with_ref.status_code == 201, with_ref.text
    assert without.status_code == 201, without.text
    stored = await _message(db_session, workspace_name, "has ref")
    assert stored.internal_metadata == {"subject_ref": SUBJECT}
    assert stored.h_metadata["subject_ref"] == SUBJECT
    plain = await _message(db_session, workspace_name, "no ref")
    assert plain.internal_metadata == {}
    assert plain.h_metadata == {}


async def test_deletion_by_subject_ref_removes_message_and_derived(
    client: TestClient, db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    workspace_name, peer_name = workspace.name, peer.name
    documents = await _seed(client, db_session, workspace_name, peer_name)
    await _add_child(db_session, workspace_name, peer_name, documents["subject"])

    response = client.post(
        f"/v3/workspaces/{workspace_name}/deletions", json={"subject_ref": SUBJECT}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["subject_ref"] == SUBJECT
    assert body["document_count"] == 2
    assert set(body) == {
        "id",
        "peer_name",
        "subject_ref",
        "document_count",
        "embedding_count",
        "summary_count",
    }

    db_session.expire_all()
    assert await _message_contents(db_session, workspace_name) == [
        "note about other",
        "note about plain",
    ]
    assert await _document_contents(db_session, workspace_name) == {
        "conclusion about other",
        "conclusion about plain",
    }
    stored = (
        await db_session.scalars(
            select(models.DeletionRecord).where(
                models.DeletionRecord.workspace_name == workspace_name
            )
        )
    ).one()
    assert stored.document_count == 2
    assert stored.subject_hash is not None
    assert SUBJECT not in stored.subject_hash


async def test_deletion_reaches_a_message_with_no_conclusion_yet(
    client: TestClient, db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    workspace_name, peer_name = workspace.name, peer.name
    first = _ingest(
        client, workspace_name, peer_name, "unprocessed", {"subject_ref": SUBJECT}
    )
    second = _ingest(client, workspace_name, peer_name, "kept", {"subject_ref": OTHER})
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    response = client.post(
        f"/v3/workspaces/{workspace_name}/deletions", json={"subject_ref": SUBJECT}
    )
    assert response.status_code == 201, response.text
    assert response.json()["document_count"] == 0
    db_session.expire_all()
    assert await _message_contents(db_session, workspace_name) == ["kept"]


async def test_subject_changed_marks_derived_conclusions_stale(
    client: TestClient, db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    workspace_name, peer_name = workspace.name, peer.name
    documents = await _seed(client, db_session, workspace_name, peer_name)
    await _add_child(db_session, workspace_name, peer_name, documents["subject"])

    response = client.post(
        f"/v3/workspaces/{workspace_name}/subjects/{SUBJECT}/changed"
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"subject_ref": SUBJECT, "stale_count": 2}
    db_session.expire_all()
    rows = (
        await db_session.scalars(
            select(models.Document).where(
                models.Document.workspace_name == workspace_name
            )
        )
    ).all()
    assert {row.content: row.stale for row in rows} == {
        "conclusion about subject": True,
        "deduced from the subject conclusion": True,
        "conclusion about other": False,
        "conclusion about plain": False,
    }
    # A change flags conclusions. It removes no message.
    assert len(await _message_contents(db_session, workspace_name)) == 3


@pytest.mark.parametrize(
    "bad", ["", "   ", 123, None, ["customer:1"], {"id": "customer:1"}, True]
)
async def test_malformed_subject_ref_is_refused(
    client: TestClient,
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
    bad: Any,
) -> None:
    workspace, peer = sample_data
    workspace_name, peer_name = workspace.name, peer.name
    response = _ingest(
        client, workspace_name, peer_name, "refused", {"subject_ref": bad}
    )
    assert response.status_code == 422, response.text
    assert await _message_contents(db_session, workspace_name) == []
    # Positive control: the same body with a valid ref is accepted.
    ok = _ingest(
        client, workspace_name, peer_name, "accepted", {"subject_ref": SUBJECT}
    )
    assert ok.status_code == 201, ok.text
    assert await _message_contents(db_session, workspace_name) == ["accepted"]


async def test_malformed_subject_ref_in_a_batch_refuses_the_whole_batch(
    client: TestClient, db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    workspace_name, peer_name = workspace.name, peer.name
    good = {"content": "good", "peer_id": peer_name}
    good["metadata"] = {"subject_ref": SUBJECT}  # pyright: ignore[reportArgumentType]
    bad = {"content": "bad", "peer_id": peer_name}
    bad["metadata"] = {"subject_ref": " "}  # pyright: ignore[reportArgumentType]
    response = client.post(
        f"/v3/workspaces/{workspace_name}/sessions/s1/messages",
        json={"messages": [good, bad]},
    )
    assert response.status_code == 422, response.text
    assert await _message_contents(db_session, workspace_name) == []


async def test_messages_without_subject_ref_behave_as_before(
    client: TestClient, db_session: AsyncSession, sample_data: tuple[Workspace, Peer]
) -> None:
    workspace, peer = sample_data
    workspace_name, peer_name = workspace.name, peer.name
    documents = await _seed(client, db_session, workspace_name, peer_name)
    # A subject nobody wrote touches nothing, plain notes included.
    response = client.post(
        f"/v3/workspaces/{workspace_name}/deletions",
        json={"subject_ref": "customer:none"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["document_count"] == 0
    changed = client.post(
        f"/v3/workspaces/{workspace_name}/subjects/customer:none/changed"
    )
    assert changed.json()["stale_count"] == 0
    db_session.expire_all()
    rows = (
        await db_session.scalars(
            select(models.Document).where(
                models.Document.workspace_name == workspace_name
            )
        )
    ).all()
    assert len(rows) == len(documents)
    assert not any(row.stale for row in rows)
    assert len(await _message_contents(db_session, workspace_name)) == 3
    # Peer deletion still works on plain notes.
    peer_response = client.post(
        f"/v3/workspaces/{workspace_name}/deletions", json={"peer_id": peer_name}
    )
    assert peer_response.status_code == 201, peer_response.text
    assert peer_response.json()["document_count"] == 3
