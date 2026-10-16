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
    unparseable_drops,
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
    tea = _document("likes tea")
    tea.metadata.source_trust = "owner"
    await crud.create_documents(
        db_session,
        [tea],
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


def test_peer_card_writes_are_the_rebuild_or_the_admin_route() -> None:
    """set_peer_card is the rebuild, the admin route, or the dormant tool handler."""
    import ast
    from pathlib import Path
    from typing import final

    from src.utils.agent_tools import (
        CARD_REFRESH_SPECIALIST_TOOLS,
        DEDUCTION_SPECIALIST_TOOLS,
        INDUCTION_SPECIALIST_TOOLS,
    )

    root = Path(__file__).resolve().parents[2]
    allowed = {
        ("src/crud/peer_card.py", "replace_peer_card_from_conclusions"),
        ("src/routers/peers.py", "set_peer_card"),
        ("src/utils/agent_tools.py", "_handle_update_peer_card"),
    }

    @final
    class _Visitor(ast.NodeVisitor):
        def __init__(self, relative: str) -> None:
            self.relative = relative
            self.stack: list[str] = []
            self.calls: list[tuple[str, str]] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._enter(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._enter(node)

        def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name == "set_peer_card" and self.stack:
                self.calls.append((self.relative, self.stack[-1]))
            self.generic_visit(node)

    found: list[tuple[str, str]] = []
    for path in (root / "src").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        visitor = _Visitor(relative)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found.extend(visitor.calls)
    assert set(found) == allowed
    tools_source = (root / "src" / "utils" / "agent_tools.py").read_text(
        encoding="utf-8"
    )
    for name in (
        "DEDUCTION_SPECIALIST_TOOLS",
        "INDUCTION_SPECIALIST_TOOLS",
        "CARD_REFRESH_SPECIALIST_TOOLS",
    ):
        start = tools_source.index(name + ": list")
        body = tools_source[start : tools_source.index("\n]\n", start)]
        assert "update_peer_card" not in body
    for tools in (
        DEDUCTION_SPECIALIST_TOOLS,
        INDUCTION_SPECIALIST_TOOLS,
        CARD_REFRESH_SPECIALIST_TOOLS,
    ):
        assert "update_peer_card" not in {str(tool["name"]) for tool in tools}


async def test_unparseable_safety_reply_uses_its_own_counter(
    client: object,
    db_session: object,
    sample_data: tuple[models.Workspace, models.Peer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /conclusions. Only a yes answer uses the ban counter."""
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(client, TestClient)
    assert isinstance(db_session, AsyncSession)
    workspace, peer = sample_data
    db_session.add(models.Session(name="s1", workspace_name=workspace.name))
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db_session.commit()
    monkeypatch.setenv("SENTICA_SAFETY_MODEL_CHECK", "on")
    reset_drops()
    before = drops()

    async def _ask(prompt: str, *, workspace_name: str) -> str:
        del prompt, workspace_name
        return "maybe"

    monkeypatch.setattr("src.sentica.safety._ask", _ask)
    created = client.post(
        f"/v3/workspaces/{workspace.name}/conclusions",
        json={
            "conclusions": [
                {
                    "content": "likes the window",
                    "observer_id": peer.name,
                    "observed_id": peer.name,
                    "session_id": "s1",
                }
            ]
        },
    )
    assert created.status_code == 201, created.text
    assert created.json() == []
    assert unparseable_drops() == 1
    assert drops() == before
    stored = (
        await db_session.scalars(
            select(models.Document.content).where(
                models.Document.workspace_name == workspace.name,
                models.Document.content == "likes the window",
            )
        )
    ).all()
    assert stored == []


async def test_stored_borderline_text_does_not_drop_a_later_conclusion(
    client: object,
    db_session: object,
    sample_data: tuple[models.Workspace, models.Peer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /conclusions. The check sees the new line and not older conclusions."""
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(client, TestClient)
    assert isinstance(db_session, AsyncSession)
    workspace, peer = sample_data
    db_session.add(models.Session(name="s1", workspace_name=workspace.name))
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db_session.commit()
    monkeypatch.setenv("SENTICA_SAFETY_MODEL_CHECK", "on")

    async def _ask(prompt: str, *, workspace_name: str) -> str:
        del workspace_name
        if "borderline phrase" in prompt and "likes the window" in prompt:
            return "yes"
        return "no"

    monkeypatch.setattr("src.sentica.safety._ask", _ask)

    def _post(text: str) -> object:
        return client.post(
            f"/v3/workspaces/{workspace.name}/conclusions",
            json={
                "conclusions": [
                    {
                        "content": text,
                        "observer_id": peer.name,
                        "observed_id": peer.name,
                        "session_id": "s1",
                    }
                ]
            },
        )

    workspace_name = workspace.name
    first = _post("borderline phrase")
    second = _post("likes the window")
    assert getattr(first, "status_code", None) == 201
    assert getattr(second, "status_code", None) == 201
    db_session.expire_all()
    stored = set(
        await db_session.scalars(
            select(models.Document.content).where(
                models.Document.workspace_name == workspace_name
            )
        )
    )
    assert "borderline phrase" in stored
    assert "likes the window" in stored


async def test_fallback_summary_passes_the_safety_check(
    db_session: object,
    sample_data: tuple[models.Workspace, models.Peer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process_item summary task. A fallback summary is checked before it is saved."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.deriver.consumer import process_item
    from src.sentica.workspace_scope import reset_workspace, set_workspace
    from src.utils.config_helpers import get_configuration

    assert isinstance(db_session, AsyncSession)
    workspace, peer = sample_data
    session = models.Session(name="s1", workspace_name=workspace.name)
    db_session.add(session)
    await db_session.flush()
    message = models.Message(
        session_name="s1",
        peer_name=peer.name,
        content="likes the window",
        workspace_name=workspace.name,
        token_count=3,
        seq_in_session=1,
    )
    db_session.add(message)
    await db_session.flush()
    configuration = get_configuration(None, session, workspace)
    configuration = configuration.model_copy(
        update={
            "summary": configuration.summary.model_copy(
                update={
                    "enabled": True,
                    "messages_per_short_summary": 1,
                    "messages_per_long_summary": 60,
                }
            )
        }
    )
    item = models.QueueItem(
        work_unit_key=f"summary:{workspace.name}:s1",
        task_type="summary",
        payload={
            "task_type": "summary",
            "session_name": "s1",
            "message_seq_in_session": 1,
            "message_public_id": message.public_id,
            "configuration": configuration.model_dump(mode="json"),
        },
        workspace_name=workspace.name,
        message_id=message.id,
        session_id=session.id,
    )
    db_session.add(item)
    await db_session.commit()
    prompts: list[str] = []

    async def _summary(*_args: object, **_kwargs: object) -> HonchoLLMCallResponse[str]:
        raise RuntimeError("summary model down")

    async def _ask(prompt: str, *, workspace_name: str) -> str:
        del workspace_name
        prompts.append(prompt)
        return "no"

    monkeypatch.setenv("SENTICA_SAFETY_MODEL_CHECK", "on")
    monkeypatch.setattr("src.utils.summarizer.create_short_summary", _summary)
    monkeypatch.setattr("src.utils.summarizer.create_long_summary", _summary)
    monkeypatch.setattr("src.sentica.safety._ask", _ask)
    token = set_workspace(workspace.name)
    try:
        await process_item(item)
    finally:
        reset_workspace(token)
    assert any("Conversation with" in prompt for prompt in prompts)
