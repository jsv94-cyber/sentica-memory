"""Workspace dreams: daily digests, weekly insights, and the off switch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src import crud, models, schemas
from src.config import settings
from src.llm.types import HonchoLLMCallResponse
from src.models import Peer, Workspace
from src.sentica.dreams import reset_tokens, run_dreams, tokens_used
from src.sentica.safety import drops, reset_drops


def _vector(slot: int) -> list[float]:
    values = [0.0] * settings.EMBEDDING.VECTOR_DIMENSIONS
    values[slot] = 1.0
    return values


def _document(content: str) -> schemas.DocumentCreate:
    return schemas.DocumentCreate(
        content=content,
        session_name="s1",
        level="explicit",
        metadata=schemas.DocumentMetadata(
            message_ids=[1],
            message_created_at="2026-01-02T00:00:00Z",
        ),
        embedding=_vector(0),
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


def _fake(prompts: list[str], content_for: Any):
    async def fake(**kwargs: object) -> HonchoLLMCallResponse[str]:
        prompt = str(kwargs["prompt"])
        prompts.append(prompt)
        return HonchoLLMCallResponse(
            content=content_for(prompt),
            output_tokens=4,
            input_tokens=6,
            finish_reasons=["stop"],
        )

    return fake


async def _dreams(
    db_session: AsyncSession, workspace_name: str
) -> list[dict[str, Any]]:
    rows = (
        await db_session.scalars(
            select(models.DreamDigest)
            .where(models.DreamDigest.workspace_name == workspace_name)
            .order_by(models.DreamDigest.period.asc(), models.DreamDigest.id.asc())
        )
    ).all()
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "period": row.period,
            "content": row.content,
            "skipped": row.skipped,
            "workspace_name": row.workspace_name,
        }
        for row in rows
    ]


async def test_weekly_insight_cites_daily_digests(
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
    monkeypatch: Any,
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await crud.create_documents(
        db_session,
        [_document("alpha likes the window seat")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    prompts: list[str] = []

    def content_for(prompt: str) -> str:
        if "daily digests" in prompt:
            return "weekly insight from the digest"
        return "daily digest text"

    monkeypatch.setattr("src.llm.api.honcho_llm_call", _fake(prompts, content_for))
    reset_tokens()
    created = await run_dreams(workspace.name, now=datetime(2026, 10, 12, tzinfo=UTC))
    kinds = [item["kind"] for item in created]
    assert kinds == ["daily", "weekly"]
    daily = created[0]
    weekly = created[1]
    assert daily["source_ids"]
    assert weekly["source_ids"] == [daily["id"]]
    assert weekly["workspace_name"] == workspace.name
    weekly_prompt = prompts[1]
    assert "daily digests" in weekly_prompt
    assert str(daily["id"]) in weekly_prompt
    assert "alpha likes the window seat" not in weekly_prompt
    assert tokens_used() == 20


async def test_dream_does_not_cross_workspaces(
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
    monkeypatch: Any,
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    other = models.Workspace(name="ws-dream-other")
    db_session.add(other)
    await db_session.flush()
    other_peer = models.Peer(name=peer.name, workspace_name=other.name)
    db_session.add(other_peer)
    await db_session.flush()
    db_session.add(models.Session(name="s1", workspace_name=other.name))
    db_session.add(
        models.Collection(
            workspace_name=other.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db_session.flush()
    await crud.create_documents(
        db_session,
        [_document("alpha likes the window seat")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    await crud.create_documents(
        db_session,
        [_document("beta-only-secret door code")],
        other.name,
        observer=peer.name,
        observed=peer.name,
    )
    prompts: list[str] = []

    def content_for(_prompt: str) -> str:
        return "digest"

    monkeypatch.setattr(
        "src.llm.api.honcho_llm_call",
        _fake(prompts, content_for),
    )
    created = await run_dreams(workspace.name, now=datetime(2026, 10, 12, tzinfo=UTC))
    assert created
    assert all(item["workspace_name"] == workspace.name for item in created)
    assert all("beta-only-secret" not in prompt for prompt in prompts)
    assert await _dreams(db_session, other.name) == []


async def test_banned_inference_is_not_stored(
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
    monkeypatch: Any,
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await crud.create_documents(
        db_session,
        [_document("asked about meals")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )

    async def banned(**_kwargs: object) -> HonchoLLMCallResponse[str]:
        return HonchoLLMCallResponse(
            content="low-sugar meals and insulin pumps, so likely diabetic",
            output_tokens=4,
            input_tokens=6,
            finish_reasons=["stop"],
        )

    monkeypatch.setattr("src.llm.api.honcho_llm_call", banned)
    reset_drops()
    created = await run_dreams(workspace.name, now=datetime(2026, 10, 12, tzinfo=UTC))
    assert created
    assert all(item["content"] == "" and item["skipped"] for item in created)
    stored = await _dreams(db_session, workspace.name)
    assert stored
    assert all(item["content"] == "" for item in stored)
    assert all("diabetic" not in item["content"] for item in stored)
    assert drops() == 1


async def test_synthesis_off_keeps_earlier_dreams(
    client: TestClient,
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
    monkeypatch: Any,
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await crud.create_documents(
        db_session,
        [_document("alpha likes the window seat")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )

    def content_for(_prompt: str) -> str:
        return "kept dream"

    monkeypatch.setattr(
        "src.llm.api.honcho_llm_call",
        _fake([], content_for),
    )
    first = await run_dreams(workspace.name, now=datetime(2026, 10, 12, tzinfo=UTC))
    assert len(first) == 2
    turned_off = client.put(
        "/v3/workspaces/" + workspace.name + "/synthesis",
        json={"enabled": False},
    )
    assert turned_off.status_code == 200
    assert turned_off.json()["enabled"] is False
    later = await run_dreams(
        workspace.name,
        now=datetime(2026, 10, 12, tzinfo=UTC) + timedelta(days=8),
    )
    assert later == []
    stored = await _dreams(db_session, workspace.name)
    assert len(stored) == 2
    assert stored[0]["content"] == "kept dream"
