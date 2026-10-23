"""search_memory, brief, and list carry the four read-time dedupe fields."""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from src import models, schemas
from src.config import settings
from src.models import Peer, Workspace

DEDUPE_FIELDS = ("workspace_id", "subject_ref", "session_id", "content")
_REQUIRED_TEXT = ("workspace_id", "content")
_NULLABLE_TEXT = ("subject_ref", "session_id")


class DedupeFieldError(Exception):
    """A read row is missing a dedupe field, or the field is empty."""


def require_dedupe_fields(row: object) -> None:
    """Refuse a row that cannot be deduped on the four read fields.

    A missing key, an empty string, a non-string, or a non-object is
    refused. ``subject_ref`` and ``session_id`` may be null. They may
    not be omitted.
    """
    if not isinstance(row, dict):
        raise DedupeFieldError("not an object")
    payload = cast(dict[object, object], row)
    for name in DEDUPE_FIELDS:
        if name not in payload:
            raise DedupeFieldError("unset")
        value = payload[name]
        if name in _REQUIRED_TEXT:
            if not isinstance(value, str) or value.strip() == "":
                raise DedupeFieldError("empty")
        elif name in _NULLABLE_TEXT:
            if value is not None and (
                not isinstance(value, str) or value.strip() == ""
            ):
                raise DedupeFieldError("empty")
        else:
            raise DedupeFieldError("unknown")


def require_stale_without_session(row: object) -> None:
    """Refuse a stale item that carries ``session_id`` or drops its text."""
    if not isinstance(row, dict):
        raise DedupeFieldError("not an object")
    payload = cast(dict[object, object], row)
    if "session_id" in payload:
        raise DedupeFieldError("session_id")
    for name in ("id", "subject_ref", "content"):
        if name not in payload:
            raise DedupeFieldError("unset")
    content = payload["content"]
    subject_ref = payload["subject_ref"]
    item_id = payload["id"]
    if not isinstance(content, str) or content.strip() == "":
        raise DedupeFieldError("empty")
    if not isinstance(item_id, str) or item_id.strip() == "":
        raise DedupeFieldError("empty")
    if subject_ref is not None and (
        not isinstance(subject_ref, str) or subject_ref.strip() == ""
    ):
        raise DedupeFieldError("empty")


def _vector(slot: int) -> list[float]:
    values = [0.0] * settings.EMBEDDING.VECTOR_DIMENSIONS
    values[slot] = 1.0
    return values


def _good_row() -> dict[str, object]:
    return {
        "workspace_id": "general",
        "subject_ref": "customer:123",
        "session_id": "call-1",
        "content": "the customer prefers the window seat",
    }


def test_dedupe_field_check_rejects_a_mutated_row() -> None:
    """The intact row passes. Dropping the guard accepts a stripped row."""
    require_dedupe_fields(_good_row())
    for name in DEDUPE_FIELDS:
        mutated = _good_row()
        del mutated[name]
        with pytest.raises(DedupeFieldError):
            require_dedupe_fields(mutated)
        blank = _good_row()
        blank[name] = ""
        with pytest.raises(DedupeFieldError):
            require_dedupe_fields(blank)
        blank[name] = "   "
        with pytest.raises(DedupeFieldError):
            require_dedupe_fields(blank)
    with pytest.raises(DedupeFieldError):
        require_dedupe_fields({"other": "value"})
    with pytest.raises(DedupeFieldError):
        require_dedupe_fields(None)
    with pytest.raises(DedupeFieldError):
        require_dedupe_fields([])
    nullable = _good_row()
    nullable["session_id"] = None
    nullable["subject_ref"] = None
    require_dedupe_fields(nullable)
    wrong_type = _good_row()
    wrong_type["workspace_id"] = 1
    with pytest.raises(DedupeFieldError):
        require_dedupe_fields(wrong_type)

    stale: dict[str, object] = {
        "id": "conclusion-1",
        "subject_ref": "customer:123",
        "content": "the customer prefers the window seat",
    }
    require_stale_without_session(stale)
    with_session = dict(stale)
    with_session["session_id"] = "call-1"
    with pytest.raises(DedupeFieldError):
        require_stale_without_session(with_session)
    with_null_session = dict(stale)
    with_null_session["session_id"] = None
    with pytest.raises(DedupeFieldError):
        require_stale_without_session(with_null_session)
    dropped_content = dict(stale)
    del dropped_content["content"]
    with pytest.raises(DedupeFieldError):
        require_stale_without_session(dropped_content)
    empty_content = dict(stale)
    empty_content["content"] = ""
    with pytest.raises(DedupeFieldError):
        require_stale_without_session(empty_content)


def test_stale_flag_schema_has_no_session_id() -> None:
    """Adding session_id to the stale flag fails this test."""
    assert set(schemas.StaleFlag.model_fields) == {"id", "subject_ref", "content"}


async def test_search_memory_brief_and_list_carry_dedupe_fields(
    client: TestClient,
    db_session: AsyncSession,
    sample_data: tuple[Workspace, Peer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing a dedupe field from those responses fails this test."""
    workspace, peer = sample_data
    session_name = "call-1"
    content = "the customer prefers the window seat"
    subject_ref = "customer:123"
    db_session.add(models.Session(name=session_name, workspace_name=workspace.name))
    await db_session.commit()

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
                    "content": content,
                    "observer_id": peer.name,
                    "observed_id": peer.name,
                    "session_id": session_name,
                    "subject_ref": subject_ref,
                    "source_trust": "external",
                }
            ]
        },
    )
    assert created.status_code == 201, created.status_code
    created_rows = cast(list[dict[str, Any]], created.json())
    assert len(created_rows) == 1

    found = client.post(
        "/v3/workspaces/" + workspace.name + "/peers/" + peer.name + "/search_memory",
        json={"query": "window seat", "subject_ref": subject_ref, "top_k": 5},
    )
    assert found.status_code == 200, found.status_code
    search_rows = cast(list[dict[str, Any]], found.json())
    assert len(search_rows) == 1
    _assert_row(search_rows[0], workspace.name, subject_ref, session_name, content)

    brief = client.post(
        "/v3/workspaces/" + workspace.name + "/peers/" + peer.name + "/brief",
        json={"token_budget": 400, "subject_ref": subject_ref},
    )
    assert brief.status_code == 200, brief.status_code
    brief_body = cast(dict[str, Any], brief.json())
    conclusions = cast(list[dict[str, Any]], brief_body["conclusions"])
    assert len(conclusions) == 1
    _assert_row(conclusions[0], workspace.name, subject_ref, session_name, content)

    listed = client.post(
        "/v3/workspaces/" + workspace.name + "/conclusions/list",
        json={"filters": {"session_id": session_name}},
    )
    assert listed.status_code == 200, listed.status_code
    page = cast(dict[str, Any], listed.json())
    items = cast(list[dict[str, Any]], page["items"])
    assert len(items) == 1
    _assert_row(items[0], workspace.name, subject_ref, session_name, content)

    changed = client.post(
        "/v3/workspaces/" + workspace.name + "/subjects/" + subject_ref + "/changed"
    )
    assert changed.status_code == 200, changed.status_code
    stale_brief = client.post(
        "/v3/workspaces/" + workspace.name + "/peers/" + peer.name + "/brief",
        json={"token_budget": 400, "subject_ref": subject_ref},
    )
    assert stale_brief.status_code == 200, stale_brief.status_code
    stale_body = cast(dict[str, Any], stale_brief.json())
    flags = cast(list[dict[str, Any]], stale_body["stale"])
    assert len(flags) == 1
    require_stale_without_session(flags[0])
    assert flags[0]["subject_ref"] == subject_ref
    assert flags[0]["content"] == content


def _assert_row(
    row: dict[str, Any],
    workspace_id: str,
    subject_ref: str,
    session_id: str,
    content: str,
) -> None:
    require_dedupe_fields(row)
    assert row["workspace_id"] == workspace_id
    assert row["subject_ref"] == subject_ref
    assert row["session_id"] == session_id
    assert row["content"] == content
