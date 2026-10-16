"""M4: banned inferences are dropped, and conclusions keep their sources."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src import crud, models, schemas
from src.config import settings
from src.crud.peer_card import get_peer_card, replace_peer_card_from_conclusions
from src.deriver.prompts import minimal_deriver_prompt
from src.dreamer.specialists import (
    DeductionSpecialist,
    InductionSpecialist,
)
from src.llm.types import HonchoLLMCallResponse
from src.sentica.safety import (
    PROMPT_RULES,
    banned_category,
    drops,
    keep_conclusion,
    reset_drops,
)
from src.utils.summarizer import long_summary_prompt, short_summary_prompt

_INSULIN = "asked about low-sugar meals plus insulin pumps, so likely diabetic"
_BANNED = {
    "health": _INSULIN,
    "sexual": "is gay",
    "religion": "is a Christian",
    "politics": "votes Democrat",
    "immigration": "is undocumented",
    "criminal": "has a felony conviction",
    "finances": "salary is 90000",
    "leave": "was out sick with the flu",
}


def _embedding() -> list[float]:
    return [0.0] * settings.EMBEDDING.VECTOR_DIMENSIONS


def _document(
    content: str,
    message_ids: list[int] | None = None,
    session_name: str = "s1",
) -> schemas.DocumentCreate:
    return schemas.DocumentCreate(
        content=content,
        session_name=session_name,
        level="explicit",
        metadata=schemas.DocumentMetadata(
            message_ids=message_ids or [],
            message_created_at="2026-01-02T00:00:00Z",
        ),
        embedding=_embedding(),
    )


@pytest.mark.parametrize(("category", "text"), list(_BANNED.items()))
def test_each_banned_category_is_rejected(category: str, text: str) -> None:
    assert banned_category(text) == category


def test_dates_only_leave_and_ordinary_facts_are_kept() -> None:
    assert banned_category("absent from 2026-01-02 to 2026-01-04") is None
    assert banned_category("likes tea") is None
    assert banned_category("asked about low-sugar meals") is None


def test_prompts_forbid_the_banned_categories() -> None:
    deriver = minimal_deriver_prompt("peer", "hello")
    short = short_summary_prompt("hello", 20, "")
    long = long_summary_prompt("hello", 20, "")
    deduction = DeductionSpecialist().build_system_prompt("peer")
    induction = InductionSpecialist().build_system_prompt("peer")
    from src.dialectic.prompts import (
        agent_system_prompt,
        workspace_agent_system_prompt,
    )

    pair = agent_system_prompt("observer", "observed", None, None)
    workspace_prompt = workspace_agent_system_prompt()
    for prompt in (deriver, short, long, deduction, induction, pair, workspace_prompt):
        assert PROMPT_RULES in prompt


def test_unknown_source_trust_is_rejected() -> None:
    with pytest.raises(ValidationError):
        schemas.MessageCreate.model_validate(
            {"content": "hello", "peer_id": "p1", "source_trust": "stranger"}
        )


async def test_model_check_drops_without_switching_to_a_stored_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_drops()
    monkeypatch.setenv("SENTICA_SAFETY_MODEL_CHECK", "true")

    async def _yes(**_kwargs: object) -> HonchoLLMCallResponse[str]:
        return HonchoLLMCallResponse(
            content="yes", output_tokens=1, finish_reasons=["stop"]
        )

    monkeypatch.setattr("src.llm.api.honcho_llm_call", _yes)
    assert await keep_conclusion("likes tea", workspace_name="left") is False
    assert drops() == 1

    async def _down(**_kwargs: object) -> HonchoLLMCallResponse[str]:
        raise RuntimeError("gateway down")

    monkeypatch.setattr("src.llm.api.honcho_llm_call", _down)
    assert await keep_conclusion("likes tea", workspace_name="left") is False
    assert drops() == 2
    monkeypatch.delenv("SENTICA_SAFETY_MODEL_CHECK")
    assert await keep_conclusion("likes tea", workspace_name="left") is True


async def test_banned_conclusions_are_not_stored(
    db_session: object, sample_data: tuple[models.Workspace, models.Peer]
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(db_session, AsyncSession)
    workspace, peer = sample_data
    reset_drops()
    db_session.add(models.Session(name="s1", workspace_name=workspace.name))
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db_session.flush()
    texts = list(_BANNED.values()) + ["likes tea"]
    result = await crud.create_documents(
        db_session,
        [_document(text) for text in texts],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert [doc.content for doc in result.created_documents] == ["likes tea"]
    assert drops() == len(_BANNED)
    stored = (
        await db_session.scalars(
            select(models.Document.content).where(
                models.Document.workspace_name == workspace.name
            )
        )
    ).all()
    assert stored == ["likes tea"]


async def test_external_conclusions_keep_message_ids_and_leave_the_card(
    db_session: object, sample_data: tuple[models.Workspace, models.Peer]
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(db_session, AsyncSession)
    workspace, peer = sample_data
    session = models.Session(name="s1", workspace_name=workspace.name)
    message = models.Message(
        session_name=session.name,
        peer_name=peer.name,
        content="window",
        workspace_name=workspace.name,
        token_count=1,
        seq_in_session=1,
        source_trust="external",
    )
    db_session.add(session)
    db_session.add(message)
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db_session.flush()
    fact = "likes the window seat"
    await crud.create_documents(
        db_session,
        [_document(fact, [message.id])],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    stored = (
        await db_session.scalars(
            select(models.Document).where(models.Document.content == fact)
        )
    ).one()
    conclusion = schemas.Conclusion.model_validate(stored)
    assert conclusion.message_ids == [message.public_id]
    assert conclusion.source_trust == "external"
    await crud.create_documents(
        db_session,
        [_document("likes tea")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    card = await replace_peer_card_from_conclusions(
        db_session,
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    stored_card = await get_peer_card(
        db_session,
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert card == ["likes tea"]
    assert stored_card == ["likes tea"]
    assert fact not in card
