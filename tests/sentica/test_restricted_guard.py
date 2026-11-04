"""Derived Restricted text is dropped unless the workspace ceiling can hold it."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from src import crud, models, schemas
from src.config import settings
from src.exceptions import ValidationException
from src.llm.types import HonchoLLMCallResponse
from src.sentica.ops_metrics import FILTER_CATEGORIES, filtered, reset_ops
from src.sentica.restricted import (
    REASON_CODES,
    restricted_drop_reason,
    restricted_reason,
)
from src.sentica.safety import drops, reset_drops
from src.sentica.workspace_metadata import (
    CEILING_KEY,
    CEILING_VALUES,
    ceiling_holds_restricted,
    check_workspace_metadata,
    workspace_ceiling,
)

_CARD = "4111111111111111"
_CARD_BAD = "4111111111111112"
_CARD_SPACED = "4111 1111 1111 1111"
_ROUTING = "routing number 021000021"
_ROUTING_BARE = "021000021"
_ROUTING_BAD = "routing number 021000022"
_SSN = "123-45-6789"
_ACCOUNT = "acct 12345678"
_ACCOUNT_BARE = "12345678"
_SALARY = "hourly rate is 42"
_SALARY_BARE = "hourly rate is high"
_DISCIPLINE = "received a written warning"
_DISCIPLINE_NEAR = "received a written note"
_PLAIN = "likes the window seat"

_HITS: tuple[tuple[str, str], ...] = (
    ("card", _CARD),
    ("routing", _ROUTING),
    ("ssn", _SSN),
    ("account", _ACCOUNT),
    ("salary", _SALARY),
    ("disciplinary", _DISCIPLINE),
)
_MISSES: tuple[str, ...] = (
    _CARD_BAD,
    _ROUTING_BARE,
    "000-45-6789",
    "666-45-6789",
    "901-45-6789",
    "123-00-6789",
    "123-45-0000",
    _ACCOUNT_BARE,
    _SALARY_BARE,
    _DISCIPLINE_NEAR,
    _PLAIN,
)


def _vector() -> list[float]:
    return [0.0] * settings.EMBEDDING.VECTOR_DIMENSIONS


def _document(
    content: str, *, level: Literal["explicit", "deductive"] = "deductive"
) -> schemas.DocumentCreate:
    return schemas.DocumentCreate(
        content=content,
        session_name="s1",
        level=level,
        metadata=schemas.DocumentMetadata(
            message_ids=[],
            message_created_at="2026-01-02T00:00:00Z",
        ),
        embedding=_vector(),
    )


async def _ready(
    db: AsyncSession, workspace: models.Workspace, peer: models.Peer
) -> None:
    db.add(models.Session(name="s1", workspace_name=workspace.name))
    db.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db.commit()


async def _set_ceiling(
    db: AsyncSession, workspace: models.Workspace, value: object
) -> None:
    if value is None:
        workspace.internal_metadata = {}
    else:
        workspace.internal_metadata = {CEILING_KEY: value}
    flag_modified(workspace, "internal_metadata")
    await db.commit()


async def _stored(db: AsyncSession, workspace_name: str) -> list[str]:
    rows = (
        await db.scalars(
            select(models.Document.content)
            .where(models.Document.workspace_name == workspace_name)
            .order_by(models.Document.content.asc())
        )
    ).all()
    return [str(row) for row in rows]


def test_each_pattern_and_its_mutation() -> None:
    """A checksum, range, context, or digit change is not the same hit."""
    assert restricted_reason(_CARD_SPACED) == "card"
    assert restricted_reason(_ROUTING_BAD) == "account"
    for reason, text in _HITS:
        assert restricted_reason(text) == reason
        assert reason in FILTER_CATEGORIES
    for text in _MISSES:
        assert restricted_reason(text) is None
    assert restricted_drop_reason(_SSN, "general", broker_explicit=True) is None
    assert restricted_drop_reason(_SSN, "general", broker_explicit=False) == "ssn"
    assert restricted_drop_reason(_SSN, "internal", broker_explicit=False) == "ssn"
    assert restricted_drop_reason(_SSN, "restricted", broker_explicit=False) is None
    assert restricted_drop_reason(_SSN, "personal", broker_explicit=False) is None
    assert set(REASON_CODES) <= set(FILTER_CATEGORIES)


def test_ceiling_values_fail_closed() -> None:
    assert {"general", "internal", "restricted", "personal"} == CEILING_VALUES
    assert workspace_ceiling(None) == "general"
    assert workspace_ceiling({}) == "general"
    assert workspace_ceiling({"other": "restricted"}) == "general"
    for value in CEILING_VALUES:
        assert workspace_ceiling({CEILING_KEY: value}) == value
        assert ceiling_holds_restricted(value) is (value in {"restricted", "personal"})
    for malformed in ("Restricted", "secret", "", " general", 1, None, ["restricted"]):
        assert workspace_ceiling({CEILING_KEY: malformed}) == "general"
    check_workspace_metadata({CEILING_KEY: "restricted"})
    check_workspace_metadata({CEILING_KEY: "nope"})
    with pytest.raises(ValidationException, match="not allowed"):
        check_workspace_metadata({CEILING_KEY: "general", "dreams": []})


_VISA = "4111 1111 1111 1111"
_AMEX = "3782 822463 10005"
_CARD_SEPARATORS = (
    " ",
    "  ",
    "-",
    " - ",
    ".",
    "/",
    "_",
    "|",
    ",",
    "\n",
    "\t",
    "\u2013",
    "\u2014",
    "\u2010",
)
_VISA_GROUPS = ("4111", "1111", "1111", "1111")
_CARD_CONTEXT: tuple[str, ...] = (
    "qty 3 {card}",
    "2026 {card}",
    "order 98765 {card}",
    "2026-11-04 {card}",
    "call 555-123-4567 {card}",
    "{card} 123",
    "{card} 12/28",
    "{card} 123 12/28",
    "{card} {card}",
    "card: {card}.",
)


@pytest.mark.parametrize("separator", _CARD_SEPARATORS)
def test_card_found_with_each_separator(separator: str) -> None:
    assert restricted_reason(separator.join(_VISA_GROUPS)) == "card"
    assert restricted_reason(_AMEX.replace(" ", separator)) == "card"


@pytest.mark.parametrize("template", _CARD_CONTEXT)
@pytest.mark.parametrize("card", (_VISA, _CARD, "4111-1111-1111-1111", _AMEX))
def test_card_found_beside_other_numbers(template: str, card: str) -> None:
    assert restricted_reason(template.format(card=card)) == "card"


@pytest.mark.parametrize(
    "text",
    (
        "qty 3 4111 1111 1111 1112",
        "2026 4111 1111 1111 1112 123",
        "4111 1111 1111 1112 12/28",
        "order 98765 4111111111111112",
        "call 555-123-4567 and 555-987-6543",
        "2026-11-04 12:30:45 and 2026-11-05",
        "1,234,567,890,123",
        "id 1 2 3 4 5 6 7 8 9 1 2 3 4 5",
        "4111 1111 11",
        "12345678901234567890",
    ),
)
def test_card_negative_controls(text: str) -> None:
    assert restricted_reason(text) is None


def test_card_guard_mutations_go_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each guard in the matcher is load-bearing: without it a case flips."""
    from src.sentica import restricted

    assert restricted_reason("2026 " + _VISA) == "card"
    monkeypatch.setattr(restricted, "_MIN_PEELED_GROUP", 99)
    assert restricted_reason("2026 " + _VISA) is None
    monkeypatch.setattr(restricted, "_MIN_PEELED_GROUP", 3)
    assert restricted_reason("2026 " + _VISA) == "card"

    def length_only(digits: str) -> bool:
        return len(digits) >= 13

    monkeypatch.setattr(restricted, "_luhn_ok", length_only)
    assert restricted_reason(_CARD_BAD) == "card"


@pytest.mark.parametrize(
    "text",
    (
        "Opened an account in 2019",
        "Her account manager is on leave until 2026-11-04",
        "The account review is on 2026-11-04",
        "account 2019",
        "acct 2026-11-04",
        "account review 12345678",
        "routing around the delay since 2019",
        "account at 12:30 on 11/04/2026",
        "the account was closed in 2024",
    ),
)
def test_account_context_needs_the_number_to_follow(text: str) -> None:
    assert restricted_reason(text) is None


@pytest.mark.parametrize(
    ("text", "reason"),
    (
        ("account number 12345678", "account"),
        ("account no. 12345678", "account"),
        ("account #12345678", "account"),
        ("account: 12345678", "account"),
        ("Account is 12345678", "account"),
        ("acct 1234 5678", "account"),
        ("routing number 021000021", "routing"),
        ("routing: 021000021", "routing"),
    ),
)
def test_account_number_after_context_hits(text: str, reason: str) -> None:
    assert restricted_reason(text) == reason


def test_account_window_mutation_goes_red(monkeypatch: pytest.MonkeyPatch) -> None:
    import re

    from src.sentica import restricted

    assert restricted_reason("The account review is on 2026-11-04") is None
    monkeypatch.setattr(
        restricted,
        "_ACCOUNT_NUMBER",
        re.compile(r"(?:account|acct|routing)\D*([0-9]{4,17})", re.IGNORECASE),
    )
    monkeypatch.setattr(restricted, "_YEAR", re.compile(r"x"))
    assert restricted_reason("Opened an account in 2019") == "account"


def test_production_log_keeps_the_reason_and_drops_the_text() -> None:
    from src.sentica.log_policy import ALLOW_LIST, ProductionLogFilter

    template = "Dropped a conclusion category=%s"
    assert ("src.sentica.safety", template) in ALLOW_LIST
    redactor = ProductionLogFilter()
    allowed = logging.LogRecord(
        "src.sentica.safety",
        logging.INFO,
        __file__,
        1,
        template,
        ("ssn",),
        None,
        func="record_drop",
    )
    assert redactor.filter(allowed)
    assert allowed.getMessage() == "Dropped a conclusion category=ssn"
    assert _SSN not in allowed.getMessage()
    leaked = logging.LogRecord(
        "src.sentica.safety",
        logging.INFO,
        __file__,
        1,
        "dropped %s",
        (_SSN,),
        None,
        func="record_drop",
    )
    assert redactor.filter(leaked)
    assert _SSN not in leaked.getMessage()


@pytest.mark.parametrize(("reason", "text"), _HITS)
async def test_pattern_drops_in_general_and_stays_when_restricted(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
    caplog: pytest.LogCaptureFixture,
    reason: str,
    text: str,
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await _set_ceiling(db_session, workspace, "general")
    reset_drops()
    reset_ops()
    caplog.set_level(logging.INFO)
    dropped = await crud.create_documents(
        db_session,
        [_document(text)],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert dropped.created_documents == []
    assert await _stored(db_session, workspace.name) == []
    assert drops() == 1
    assert filtered(reason) == 1
    assert text not in caplog.text
    assert "category=" + reason in caplog.text

    caplog.clear()
    await _set_ceiling(db_session, workspace, "restricted")
    reset_drops()
    kept = await crud.create_documents(
        db_session,
        [_document(text)],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert [doc.content for doc in kept.created_documents] == [text]
    assert await _stored(db_session, workspace.name) == [text]
    assert drops() == 0
    assert text not in caplog.text


async def test_near_miss_is_kept_in_a_general_workspace(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
) -> None:
    """Widening a pattern stores a miss and this test goes red."""
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await _set_ceiling(db_session, workspace, None)
    created = await crud.create_documents(
        db_session,
        [_document(text) for text in _MISSES],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert sorted(doc.content for doc in created.created_documents) == sorted(_MISSES)


async def test_missing_and_malformed_ceilings_are_general(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    for value in (None, "nope", "Restricted", 3, ""):
        await _set_ceiling(db_session, workspace, value)
        reset_drops()
        created = await crud.create_documents(
            db_session,
            [_document(_SSN)],
            workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
        assert created.created_documents == []
        assert drops() == 1
    assert await _stored(db_session, workspace.name) == []


async def test_internal_drops_and_personal_keeps(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await _set_ceiling(db_session, workspace, "internal")
    reset_drops()
    dropped = await crud.create_documents(
        db_session,
        [_document(_SSN)],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert dropped.created_documents == []
    assert drops() == 1
    await _set_ceiling(db_session, workspace, "personal")
    reset_drops()
    kept = await crud.create_documents(
        db_session,
        [_document(_SSN)],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert [doc.content for doc in kept.created_documents] == [_SSN]
    assert drops() == 0


async def test_broker_explicit_conclusion_is_kept(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace, peer = sample_data
    await _set_ceiling(db_session, workspace, None)
    reset_drops()
    caplog.set_level(logging.INFO)
    stored = await crud.create_observations(
        db_session,
        [
            schemas.ConclusionCreate(
                content=_SSN,
                observer_id=peer.name,
                observed_id=peer.name,
                source_trust="owner",
            )
        ],
        workspace.name,
    )
    assert [doc.content for doc in stored] == [_SSN]
    assert drops() == 0
    assert _SSN not in caplog.text


async def test_engine_explicit_conclusion_is_dropped(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await _set_ceiling(db_session, workspace, "general")
    reset_drops()
    created = await crud.create_documents(
        db_session,
        [_document(_SSN, level="explicit")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert created.created_documents == []
    assert await _stored(db_session, workspace.name) == []
    assert drops() == 1


async def test_peer_card_guard_and_its_mutation(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace, peer = sample_data
    await _set_ceiling(db_session, workspace, "general")
    reset_drops()
    caplog.set_level(logging.INFO)
    await crud.set_peer_card(
        db_session,
        workspace.name,
        [_SSN, _PLAIN],
        observer=peer.name,
        observed=peer.name,
    )
    stored = await crud.get_peer_card(
        db_session, workspace.name, observer=peer.name, observed=peer.name
    )
    assert stored == [_PLAIN]
    assert drops() == 1
    assert _SSN not in caplog.text
    assert "category=ssn" in caplog.text

    def _keep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("src.sentica.restricted.restricted_drop_reason", _keep)
    await crud.set_peer_card(
        db_session,
        workspace.name,
        [_SSN, _PLAIN],
        observer=peer.name,
        observed=peer.name,
    )
    mutated = await crud.get_peer_card(
        db_session, workspace.name, observer=peer.name, observed=peer.name
    )
    assert mutated == [_SSN, _PLAIN]

    monkeypatch.undo()
    await _set_ceiling(db_session, workspace, "restricted")
    reset_drops()
    await crud.set_peer_card(
        db_session,
        workspace.name,
        [_SSN, _PLAIN],
        observer=peer.name,
        observed=peer.name,
    )
    kept = await crud.get_peer_card(
        db_session, workspace.name, observer=peer.name, observed=peer.name
    )
    assert kept == [_SSN, _PLAIN]
    assert drops() == 0


async def test_dream_guard_and_its_mutation(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.sentica.dreams import run_dreams

    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await _set_ceiling(db_session, workspace, "general")
    await crud.create_documents(
        db_session,
        [_document(_PLAIN, level="explicit")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )

    async def _reply(**_kwargs: object) -> HonchoLLMCallResponse[str]:
        return HonchoLLMCallResponse(
            content=_SSN,
            output_tokens=4,
            input_tokens=6,
            finish_reasons=["stop"],
        )

    monkeypatch.setattr("src.llm.api.honcho_llm_call", _reply)
    reset_drops()
    caplog.set_level(logging.INFO)
    first = await run_dreams(workspace.name, now=datetime(2026, 10, 12, tzinfo=UTC))
    assert first
    assert all(item["content"] == "" and item["skipped"] for item in first)
    assert drops() >= 1
    assert _SSN not in caplog.text
    assert "category=ssn" in caplog.text

    def _keep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("src.sentica.restricted.restricted_drop_reason", _keep)
    second = await run_dreams(workspace.name, now=datetime(2026, 10, 13, tzinfo=UTC))
    assert any(item["content"] == _SSN and not item["skipped"] for item in second)

    monkeypatch.setattr(
        "src.sentica.restricted.restricted_drop_reason", restricted_drop_reason
    )
    await _set_ceiling(db_session, workspace, "restricted")
    reset_drops()
    third = await run_dreams(workspace.name, now=datetime(2026, 10, 14, tzinfo=UTC))
    assert any(item["content"] == _SSN and not item["skipped"] for item in third)
    assert drops() == 0


def test_normalized_text_and_short_accounts_match() -> None:
    """NFKC and format characters still hit. Other scripts do not. 4 to 6 digit accounts hit."""
    fullwidth = "".join(chr(0xFF10 + int(char)) for char in _CARD)
    assert restricted_reason(fullwidth) == "card"
    assert restricted_reason("4111\u200b1111\u200b1111\u200b1111") == "card"
    assert restricted_reason("123\u200b-45-6789") == "ssn"
    assert restricted_reason("hourly rate is \uff14\uff12") == "salary"
    assert restricted_reason("acct 1234") == "account"
    assert restricted_reason("acct 12345") == "account"
    assert restricted_reason("acct 123456") == "account"
    assert restricted_reason("acct 12\u200b34") == "account"
    assert restricted_reason("acct 123") is None
    assert (
        restricted_reason("acct \u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668")
        is None
    )
    assert restricted_reason("1234") is None


async def test_obfuscated_number_is_dropped_at_the_save_point(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
) -> None:
    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    await _set_ceiling(db_session, workspace, None)
    reset_drops()
    await crud.create_documents(
        db_session,
        [_document("4111\u200b1111\u200b1111\u200b1111")],
        workspace.name,
        observer=peer.name,
        observed=peer.name,
    )
    assert await _stored(db_session, workspace.name) == []
    assert drops() == 1


async def test_summary_drops_restricted_text_when_the_ceiling_is_general(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm.types import HonchoLLMCallResponse as Response
    from src.schemas.configuration import (
        ResolvedConfiguration,
        ResolvedDreamConfiguration,
        ResolvedPeerCardConfiguration,
        ResolvedReasoningConfiguration,
        ResolvedSummaryConfiguration,
    )
    from src.utils.summarizer import (
        SummaryType,
        _create_and_save_summary,  # pyright: ignore[reportPrivateUsage]
    )

    workspace, peer = sample_data
    await _ready(db_session, workspace, peer)
    db_session.add(
        models.Message(
            session_name="s1",
            peer_name=peer.name,
            content="desk note",
            workspace_name=workspace.name,
            token_count=2,
            seq_in_session=1,
        )
    )
    await db_session.commit()
    message = await db_session.scalar(
        select(models.Message).where(models.Message.workspace_name == workspace.name)
    )
    assert message is not None
    configuration = ResolvedConfiguration(
        reasoning=ResolvedReasoningConfiguration(enabled=True),
        peer_card=ResolvedPeerCardConfiguration(use=False, create=False),
        summary=ResolvedSummaryConfiguration(
            enabled=True,
            messages_per_short_summary=20,
            messages_per_long_summary=60,
        ),
        dream=ResolvedDreamConfiguration(enabled=False),
    )

    async def _summary(*_args: object, **_kwargs: object) -> Response[str]:
        return Response(
            content=_SSN,
            output_tokens=4,
            input_tokens=6,
            finish_reasons=["stop"],
        )

    monkeypatch.setattr("src.utils.summarizer.create_short_summary", _summary)
    await _set_ceiling(db_session, workspace, "general")
    reset_drops()
    caplog.set_level(logging.INFO)
    await _create_and_save_summary(
        workspace.name,
        "s1",
        message_id=message.id,
        message_seq_in_session=message.seq_in_session,
        message_public_id=message.public_id,
        summary_type=SummaryType.SHORT,
        configuration=configuration,
    )
    stored = await db_session.scalar(
        select(models.Session.internal_metadata)
        .where(
            models.Session.workspace_name == workspace.name,
            models.Session.name == "s1",
        )
        .execution_options(populate_existing=True)
    )
    assert isinstance(stored, dict)
    assert "summaries" not in stored or stored["summaries"] == {}
    assert drops() == 1
    assert _SSN not in caplog.text
    assert "category=ssn" in caplog.text

    await _set_ceiling(db_session, workspace, "restricted")
    reset_drops()
    caplog.clear()
    await _create_and_save_summary(
        workspace.name,
        "s1",
        message_id=message.id,
        message_seq_in_session=message.seq_in_session,
        message_public_id=message.public_id,
        summary_type=SummaryType.SHORT,
        configuration=configuration,
    )
    kept = await db_session.scalar(
        select(models.Session.internal_metadata)
        .where(
            models.Session.workspace_name == workspace.name,
            models.Session.name == "s1",
        )
        .execution_options(populate_existing=True)
    )
    assert isinstance(kept, dict)
    summary = kept["summaries"]["honcho_chat_summary_short"]
    assert summary["content"] == _SSN
    assert drops() == 0
    assert _SSN not in caplog.text


async def test_only_a_broker_audience_can_set_the_ceiling(
    client: object,
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from src.exceptions import AuthenticationException
    from src.main import app
    from src.routers.workspaces import put_workspace_ceiling
    from src.schemas.api import WorkspaceCeilingUpdate
    from src.security import JWTParams, create_jwt, verify_jwt
    from src.sentica.tokens import require_broker_audience, verify_sentica_token
    from tests.sentica.test_m1_tokens import (
        _instance,  # pyright: ignore[reportPrivateUsage]
        _keys,  # pyright: ignore[reportPrivateUsage]
        _token,  # pyright: ignore[reportPrivateUsage]
    )

    assert isinstance(client, TestClient)
    workspace, _peer = sample_data
    await _set_ceiling(db_session, workspace, "general")

    refused = client.put(
        f"/v3/workspaces/{workspace.name}/ceiling",
        json={"ceiling": "restricted"},
    )
    assert refused.status_code == 401
    assert refused.json()["detail"] == "broker audience required"
    metadata_update = client.put(
        f"/v3/workspaces/{workspace.name}",
        json={"metadata": {"ceiling": "personal"}},
    )
    assert metadata_update.status_code == 200
    internal = await db_session.scalar(
        select(models.Workspace.internal_metadata)
        .where(models.Workspace.name == workspace.name)
        .execution_options(populate_existing=True)
    )
    public = await db_session.scalar(
        select(models.Workspace.h_metadata)
        .where(models.Workspace.name == workspace.name)
        .execution_options(populate_existing=True)
    )
    assert workspace_ceiling(internal) == "general"
    assert isinstance(public, dict)
    assert public.get("ceiling") == "personal"

    ordinary = JWTParams(t="1", ad=True, w=workspace.name)
    with pytest.raises(AuthenticationException, match="broker audience required"):
        require_broker_audience(ordinary)
    with pytest.raises(AuthenticationException, match="broker audience required"):
        await put_workspace_ceiling(
            workspace.name,
            WorkspaceCeilingUpdate(ceiling="restricted"),
            ordinary,
            db_session,
        )
    monkeypatch.setattr("src.config.settings.AUTH.JWT_SECRET", "test-secret")
    minted = create_jwt(JWTParams(t="1", w=workspace.name, audience="broker"))
    decoded = verify_jwt(minted)
    assert decoded.audience is None
    with pytest.raises(AuthenticationException, match="broker audience required"):
        require_broker_audience(decoded)

    private, public_pem = _keys()
    instance = _instance(tmp_path, public_pem)
    conversation = verify_sentica_token(
        _token(private, extra={"audience": "general", "w": workspace.name}),
        instance,
    )
    with pytest.raises(AuthenticationException, match="broker audience required"):
        require_broker_audience(conversation)
    missing = verify_sentica_token(
        _token(private, extra={"w": workspace.name}), instance
    )
    assert missing.audience is None
    broker = verify_sentica_token(
        _token(private, extra={"audience": "broker", "w": workspace.name}),
        instance,
    )
    assert broker.audience == "broker"
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", "") == "/v3/workspaces/{workspace_id}/ceiling"
    )
    auth_dependency = route.dependant.dependencies[0].call  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]
    app.dependency_overrides[auth_dependency] = lambda: broker
    try:
        url = f"/v3/workspaces/{workspace.name}/ceiling"
        stored = client.put(url, json={"ceiling": "restricted"})
        assert stored.status_code == 200
        assert stored.json() == {"ceiling": "restricted"}
        raised = await db_session.scalar(
            select(models.Workspace.internal_metadata)
            .where(models.Workspace.name == workspace.name)
            .execution_options(populate_existing=True)
        )
        assert workspace_ceiling(raised) == "restricted"

        invalid = client.put(url, json={"ceiling": "Restricted"})
        assert invalid.status_code == 422
        assert invalid.json() == {"detail": "ceiling is not an allowed value"}
        after_bad = await db_session.scalar(
            select(models.Workspace.internal_metadata)
            .where(models.Workspace.name == workspace.name)
            .execution_options(populate_existing=True)
        )
        assert workspace_ceiling(after_bad) == "restricted"

        missing_workspace = client.put(
            "/v3/workspaces/missing-workspace/ceiling", json={"ceiling": "general"}
        )
        assert missing_workspace.status_code == 404

        app.dependency_overrides[auth_dependency] = lambda: ordinary
        refused_again = client.put(url, json={"ceiling": "internal"})
        assert refused_again.status_code == 401
        assert refused_again.json()["detail"] == "broker audience required"
        final = await db_session.scalar(
            select(models.Workspace.internal_metadata)
            .where(models.Workspace.name == workspace.name)
            .execution_options(populate_existing=True)
        )
        assert workspace_ceiling(final) == "restricted"
    finally:
        app.dependency_overrides.pop(auth_dependency, None)  # pyright: ignore[reportUnknownArgumentType]
