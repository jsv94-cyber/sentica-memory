"""Retrieval eval and the isolation red-team.

Scores are deterministic. CI fails when a category drops more than 2 points
from eval/BASELINE.md, or when any workspace or tenant probe leaks.
"""

from __future__ import annotations

import math
import re
import warnings
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, cast, final

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src import crud, models, schemas
from src.config import settings
from src.exceptions import AuthenticationException
from src.sentica.eval_harness import (
    FACTS,
    QUESTIONS,
    Fact,
    baseline_leak_count,
    category_means,
    parse_baseline,
    question_scores,
    regressions,
    text_embedding,
)
from src.sentica.queue import SenticaTenantRefused, pop_and_check_tenant
from src.sentica.retrieval import prepare_brief, search_conclusions
from src.sentica.safety import banned_category
from src.sentica.workspace_scope import enforce_targets

ROOT = Path(__file__).resolve().parents[2]
_BASELINE = ROOT / "eval" / "BASELINE.md"
_PEER = "clerk"
_SESSIONS = ("s-main", "s-mon", "s-tue")
_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "of",
        "on",
        "or",
        "should",
        "that",
        "the",
        "this",
        "to",
        "when",
        "where",
        "with",
    }
)


def _tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", text.lower()) if token not in _STOP
    }


def _by_key() -> dict[str, Fact]:
    return {fact.key: fact for fact in FACTS}


def test_embeddings_are_not_one_hot_copies() -> None:
    """A query vector is not the gold fact, and it is not a single axis."""
    dimensions = settings.EMBEDDING.VECTOR_DIMENSIONS
    compared = 0
    for question in QUESTIONS:
        if not question.gold:
            continue
        compared += 1
        query = text_embedding(question.query, dimensions)
        fact = next(item for item in FACTS if item.key == question.gold[0])
        gold = text_embedding(fact.content, dimensions)
        assert query != gold, question.name
        nonzero = sum(1 for value in query if value != 0.0)
        assert nonzero > 1, question.name
    assert compared >= 2


def test_vector_only_recall_is_not_perfect() -> None:
    """Nearest n-gram neighbor is not the gold fact for every question."""
    dimensions = settings.EMBEDDING.VECTOR_DIMENSIONS
    misses = 0
    scored = 0
    for question in QUESTIONS:
        if question.abstain or not question.gold:
            continue
        scored += 1
        query = text_embedding(question.query, dimensions)
        pool = [fact for fact in FACTS if fact.business == question.business]
        nearest = max(
            pool,
            key=lambda fact: sum(
                left * right
                for left, right in zip(
                    query,
                    text_embedding(fact.content, dimensions),
                    strict=True,
                )
            ),
        )
        if nearest.key not in question.gold:
            misses += 1
    assert scored >= 2
    assert misses >= 1


def test_seed_facts_are_not_banned() -> None:
    for fact in FACTS:
        assert banned_category(fact.content) is None, fact.key


def test_questions_use_the_channel_their_category_needs() -> None:
    facts = _by_key()
    assert len(facts) == len(FACTS)
    all_tokens: set[str] = set()
    for fact in FACTS:
        all_tokens.update(_tokens(fact.content))
    phase1_slots = [fact.slot for fact in FACTS if fact.phase == 1]
    assert len(phase1_slots) == len(set(phase1_slots))
    for question in QUESTIONS:
        assert question.category
        for key in question.gold + question.forbidden:
            assert key in facts
        query_tokens = _tokens(question.query)
        if question.category in {
            "information_extraction",
            "knowledge_updates",
            "abstention",
        }:
            assert query_tokens.isdisjoint(all_tokens), question.name
        if question.category == "multi_session_reasoning":
            sessions = {facts[key].session_name for key in question.gold}
            assert len(question.gold) == 2
            assert len(sessions) == 2
            for key in question.gold:
                assert query_tokens.intersection(_tokens(facts[key].content))
        if question.category == "temporal_reasoning":
            assert question.since is not None
            for key in question.gold:
                created = facts[key].created_at
                assert created is not None and created >= question.since
            for key in question.forbidden:
                created = facts[key].created_at
                assert created is not None and created < question.since
        if question.category == "knowledge_updates":
            assert len(question.gold) == 1
            assert len(question.forbidden) == 1
            new = facts[question.gold[0]]
            old = facts[question.forbidden[0]]
            assert new.phase == 2 and old.phase == 1
            assert new.slot == old.slot == question.slot
            assert new.subject_ref == old.subject_ref
            assert new.subject_ref
    counts = {name: 0 for name in ("information_extraction", "abstention")}
    for question in QUESTIONS:
        if question.category in counts:
            counts[question.category] += 1
    assert all(count >= 2 for count in counts.values())


def _document(fact: Fact) -> schemas.DocumentCreate:
    return schemas.DocumentCreate(
        content=fact.content,
        session_name=fact.session_name,
        subject_ref=fact.subject_ref,
        level="explicit",
        metadata=schemas.DocumentMetadata(
            message_ids=[1],
            message_created_at="2026-01-02T00:00:00Z",
        ),
        embedding=text_embedding(fact.content, settings.EMBEDDING.VECTOR_DIMENSIONS),
    )


async def _seed(
    db: AsyncSession,
) -> tuple[dict[str, models.Workspace], dict[str, str]]:
    from nanoid import generate as generate_nanoid

    businesses = {
        "harbor": models.Workspace(name=str(generate_nanoid())),
        "northline": models.Workspace(name=str(generate_nanoid())),
    }
    for workspace in businesses.values():
        db.add(workspace)
    await db.flush()
    for workspace in businesses.values():
        db.add(models.Peer(name=_PEER, workspace_name=workspace.name))
    await db.flush()
    for workspace in businesses.values():
        for session_name in _SESSIONS:
            db.add(models.Session(name=session_name, workspace_name=workspace.name))
        db.add(
            models.Collection(
                workspace_name=workspace.name,
                observer=_PEER,
                observed=_PEER,
            )
        )
    await db.flush()

    async def insert(phase: int) -> None:
        grouped: dict[str, list[schemas.DocumentCreate]] = {
            name: [] for name in businesses
        }
        for fact in FACTS:
            if fact.phase == phase:
                grouped[fact.business].append(_document(fact))
        for business, documents in grouped.items():
            if not documents:
                continue
            await crud.create_documents(
                db,
                documents,
                businesses[business].name,
                observer=_PEER,
                observed=_PEER,
            )

    await insert(1)
    for fact in FACTS:
        if fact.created_at is None:
            continue
        await db.execute(
            update(models.Document)
            .where(models.Document.content == fact.content)
            .values(created_at=fact.created_at)
        )
    await db.commit()
    await insert(2)
    found: dict[str, str] = {}
    rows = (await db.execute(select(models.Document.id, models.Document.content))).all()
    for row in rows:
        found[str(row[1])] = str(row[0])
    ids = {fact.key: found[fact.content] for fact in FACTS}
    return businesses, ids


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


async def _search(
    db: AsyncSession,
    workspace_name: str,
    query: str,
    _slot: int,
    *,
    since: datetime | None = None,
) -> list[models.Document]:
    return await search_conclusions(
        db,
        workspace_name,
        query,
        observer=_PEER,
        observed=_PEER,
        since=since,
        top_k=10,
        embedding=text_embedding(query, settings.EMBEDDING.VECTOR_DIMENSIONS),
    )


async def _knowledge_update_details(
    db: AsyncSession,
    businesses: dict[str, models.Workspace],
    ids: dict[str, str],
) -> list[str]:
    """Why a knowledge-update question scored zero. Read on failure only."""
    keys = [
        key
        for question in QUESTIONS
        if question.category == "knowledge_updates"
        for key in question.gold + question.forbidden
    ]
    stored = (
        await db.execute(
            select(
                models.Document.id,
                models.Document.superseded_by,
                models.Document.subject_ref,
                models.Document.embedding.is_(None),
                models.Document.internal_metadata,
            ).where(models.Document.id.in_([ids[key] for key in keys]))
        )
    ).all()
    by_id = {str(row[0]): row for row in stored}
    details: list[str] = []
    for question in QUESTIONS:
        if question.category != "knowledge_updates":
            continue
        found = await _search(
            db,
            businesses[question.business].name,
            question.query,
            question.slot,
            since=question.since,
        )
        retrieved = {item.id for item in found}
        for key in question.gold + question.forbidden:
            row = by_id.get(ids[key])
            raw_meta = row[4] if row is not None else None
            meta = cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
            trust = cast(object, meta.get("source_trust"))
            relation = cast(object, meta.get("relation"))
            details.append(
                key
                + " hit="
                + str(ids[key] in retrieved)
                + " superseded="
                + str(None if row is None else row[1])
                + " subject="
                + str(None if row is None else row[2])
                + " embedding_null="
                + str(None if row is None else row[3])
                + " trust="
                + str(trust)
                + " relation="
                + str(relation)
                + " n="
                + str(len(retrieved))
            )
    return details


async def test_retrieval_eval_matches_baseline(db_session: AsyncSession) -> None:
    businesses, ids = await _seed(db_session)
    rows: list[tuple[str, float, float]] = []
    for question in QUESTIONS:
        found = await _search(
            db_session,
            businesses[question.business].name,
            question.query,
            question.slot,
            since=question.since,
        )
        retrieved = [item.id for item in found]
        gold = {ids[key] for key in question.gold}
        forbidden = {ids[key] for key in question.forbidden}
        recall5, recall10 = question_scores(
            retrieved, gold, forbidden, abstain=question.abstain
        )
        rows.append((question.category, recall5, recall10))
    current = category_means(rows)
    text = _BASELINE.read_text(encoding="utf-8")
    failed = regressions(current, parse_baseline(text))
    if failed:
        details = await _knowledge_update_details(db_session, businesses, ids)
        assert not failed, "; ".join(failed) + " || " + "; ".join(details)
    assert not failed

    sample = QUESTIONS[0]
    started = perf_counter()
    await _search(
        db_session,
        businesses[sample.business].name,
        sample.query,
        sample.slot,
        since=sample.since,
    )
    cold_ms = (perf_counter() - started) * 1000
    warm: list[float] = []
    for _ in range(20):
        started = perf_counter()
        await _search(
            db_session,
            businesses[sample.business].name,
            sample.query,
            sample.slot,
            since=sample.since,
        )
        warm.append((perf_counter() - started) * 1000)
    warm_p95 = _p95(warm)
    rendered = ", ".join(
        name + " " + str(current[name][0]) + "/" + str(current[name][1])
        for name in current
    )
    warnings.warn(
        "m8 recall "
        + rendered
        + " cold_ms "
        + str(round(cold_ms, 1))
        + " warm_p95_ms "
        + str(round(warm_p95, 1)),
        stacklevel=1,
    )
    assert "Cold:" in text
    assert "Warm p95:" in text
    assert "PENDING" not in text


@final
class _Tenant:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id


def _crossed(
    rows: list[models.Document], workspace_name: str, *, harbor: str
) -> list[str]:
    leaks: list[str] = []
    for row in rows:
        if row.workspace_name != workspace_name:
            leaks.append("search returned a row from " + row.workspace_name)
        if workspace_name == harbor and "violetkey" in row.content:
            leaks.append("harbor search returned the northline secret")
        if workspace_name != harbor and "harbor glass" in row.content:
            leaks.append("the other business returned a harbor fact")
    return leaks


async def test_isolation_red_team_reports_zero_leaks(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    businesses, _ids = await _seed(db_session)
    harbor = businesses["harbor"].name
    northline = businesses["northline"].name
    secret = _by_key()["secret"]
    invoice = _by_key()["ie-invoice"]
    leaks: list[str] = []

    control = await _search(db_session, northline, secret.content, secret.slot)
    assert any(row.content == secret.content for row in control)

    harbor_secret = await _search(db_session, harbor, secret.content, secret.slot)
    harbor_same_vector = await _search(db_session, harbor, secret.content, invoice.slot)
    north_invoice = await _search(db_session, northline, invoice.content, invoice.slot)
    leaks.extend(_crossed(harbor_secret, harbor, harbor=harbor))
    leaks.extend(_crossed(harbor_same_vector, harbor, harbor=harbor))
    leaks.extend(_crossed(north_invoice, northline, harbor=harbor))

    brief = await prepare_brief(
        harbor,
        _PEER,
        token_budget=800,
        query=secret.content,
        top_k=10,
        embedding=text_embedding(secret.content, settings.EMBEDDING.VECTOR_DIMENSIONS),
    )
    if "violetkey" in brief.model_dump_json():
        leaks.append("brief included the other workspace secret")

    try:
        enforce_targets(harbor, [northline], [harbor])
        leaks.append("token workspace set included the other workspace")
    except AuthenticationException:
        pass

    def _instance() -> _Tenant:
        return _Tenant("tenant-a")

    monkeypatch.setenv("SENTICA_MODE", "production")
    monkeypatch.setattr("src.sentica.queue.require_instance", _instance)
    try:
        pop_and_check_tenant({"task_type": "representation", "tenant_id": "tenant-b"})
        leaks.append("queue accepted another tenant")
    except SenticaTenantRefused:
        pass

    text = _BASELINE.read_text(encoding="utf-8")
    assert baseline_leak_count(text) == 0
    assert leaks == []
