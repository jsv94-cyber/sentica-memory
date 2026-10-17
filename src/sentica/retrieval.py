"""Hybrid conclusion retrieval and the bounded call brief."""

from __future__ import annotations

import datetime
import logging
import re
from typing import cast

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select
from sqlalchemy.sql.functions import func

from src import models, schemas
from src.config import settings
from src.crud.peer_card import get_peer_card
from src.dependencies import tracked_db
from src.sentica.freshness import note_retrieved, note_used
from src.sentica.vector_query import execute_neighbors, within_distance
from src.utils.formatting import ILIKE_ESCAPE_CHAR, escape_ilike_pattern
from src.utils.search import reciprocal_rank_fusion
from src.utils.tokens import estimate_tokens, tokenizer

logger = logging.getLogger(__name__)

_SPECIAL = re.compile(r"[~`!@#$%^&*()_+=\[\]{};':\"\\|,.<>/?-]")
# Orthogonal test vectors sit at distance 1. Real paraphrases sit well below this.
_MAX_DISTANCE = 0.65


def _uses_pgvector() -> bool:
    return (
        settings.VECTOR_STORE.TYPE == "pgvector" or not settings.VECTOR_STORE.MIGRATED
    )


def _scoped(
    workspace_name: str,
    observer: str,
    observed: str,
    *,
    subject_ref: str | None,
    since: datetime.datetime | None,
) -> Select[tuple[models.Document]]:
    stmt = (
        select(models.Document)
        .where(models.Document.workspace_name == workspace_name)
        .where(models.Document.observer == observer)
        .where(models.Document.observed == observed)
        .where(models.Document.deleted_at.is_(None))
        .where(models.Document.superseded_by.is_(None))
    )
    if subject_ref:
        stmt = stmt.where(models.Document.subject_ref == subject_ref)
    if since is not None:
        stmt = stmt.where(models.Document.created_at >= since)
    return stmt


async def _keyword(
    db: AsyncSession,
    stmt: Select[tuple[models.Document]],
    query: str,
    limit: int,
) -> list[models.Document]:
    escaped = escape_ilike_pattern(query)
    like = models.Document.content.ilike(f"%{escaped}%", escape=ILIKE_ESCAPE_CHAR)
    if _SPECIAL.search(query):
        ranked = stmt.where(like).order_by(models.Document.created_at.desc())
    else:
        vector = func.to_tsvector("english", models.Document.content)
        tsquery = func.plainto_tsquery("english", query)
        ranked = stmt.where(or_(vector.op("@@")(tsquery), like)).order_by(
            func.coalesce(func.ts_rank(vector, tsquery), 0).desc(),
            models.Document.created_at.desc(),
        )
    result = await db.execute(ranked.limit(limit))
    return list(result.scalars().all())


async def _vector(
    db: AsyncSession,
    stmt: Select[tuple[models.Document]],
    embedding: list[float],
    limit: int,
) -> list[models.Document]:
    rows = await execute_neighbors(db, stmt, embedding, limit)
    return within_distance(rows, embedding, _MAX_DISTANCE, limit)


def _merge(
    groups: list[list[models.Document]],
    *,
    limit: int,
) -> list[models.Document]:
    by_id: dict[str, models.Document] = {}
    id_lists: list[list[str]] = []
    for group in groups:
        ids: list[str] = []
        for document in group:
            by_id[document.id] = document
            ids.append(document.id)
        if ids:
            id_lists.append(ids)
    if not id_lists:
        return []
    fused = reciprocal_rank_fusion(*id_lists, limit=max(limit * 2, limit))
    ordered = [by_id[document_id] for document_id in fused if document_id in by_id]
    fresh = [document for document in ordered if not document.stale]
    stale = [document for document in ordered if document.stale]
    return (fresh + stale)[:limit]


async def search_conclusions(
    db: AsyncSession,
    workspace_name: str,
    query: str,
    *,
    observer: str,
    observed: str,
    subject_ref: str | None = None,
    since: datetime.datetime | None = None,
    top_k: int = 10,
    embedding: list[float] | None = None,
) -> list[models.Document]:
    """Keyword plus vector, merged by reciprocal rank, stale items last."""
    if top_k <= 0 or not query.strip():
        return []
    stmt = _scoped(
        workspace_name,
        observer,
        observed,
        subject_ref=subject_ref,
        since=since,
    )
    groups: list[list[models.Document]] = [await _keyword(db, stmt, query, top_k * 2)]
    if embedding is not None and _uses_pgvector():
        groups.append(await _vector(db, stmt, embedding, top_k * 2))
    return _merge(groups, limit=top_k)


async def _recent(
    db: AsyncSession,
    workspace_name: str,
    *,
    observer: str,
    observed: str,
    subject_ref: str | None,
    since: datetime.datetime | None,
    top_k: int,
) -> list[models.Document]:
    stmt = (
        _scoped(
            workspace_name,
            observer,
            observed,
            subject_ref=subject_ref,
            since=since,
        )
        .where(models.Document.stale.is_(False))
        .order_by(models.Document.created_at.desc())
        .limit(top_k)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _stale_flags(
    db: AsyncSession,
    workspace_name: str,
    *,
    observer: str,
    observed: str,
    subject_ref: str | None,
) -> list[models.Document]:
    stmt = (
        _scoped(
            workspace_name,
            observer,
            observed,
            subject_ref=subject_ref,
            since=None,
        )
        .where(models.Document.stale.is_(True))
        .order_by(models.Document.created_at.desc())
        .limit(50)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _latest_summary(
    db: AsyncSession,
    workspace_name: str,
    peer_name: str,
) -> str | None:
    stmt = (
        select(models.Session.internal_metadata)
        .join(
            models.session_peers_table,
            (models.Session.name == models.session_peers_table.c.session_name)
            & (
                models.Session.workspace_name
                == models.session_peers_table.c.workspace_name
            ),
        )
        .where(models.session_peers_table.c.workspace_name == workspace_name)
        .where(models.session_peers_table.c.peer_name == peer_name)
        .where(models.session_peers_table.c.left_at.is_(None))
        .where(models.Session.is_active.is_(True))
        .order_by(models.Session.created_at.desc(), models.Session.id.desc())
        .limit(20)
    )
    rows = (await db.execute(stmt)).scalars().all()
    best_at = ""
    best: str | None = None
    for raw in rows:
        summaries_raw = raw.get("summaries")
        if not isinstance(summaries_raw, dict):
            continue
        summaries = cast("dict[str, object]", summaries_raw)
        for item in summaries.values():
            if not isinstance(item, dict):
                continue
            entry = cast("dict[str, object]", item)
            content = entry.get("content")
            created = entry.get("created_at")
            if (
                isinstance(content, str)
                and isinstance(created, str)
                and created >= best_at
            ):
                best_at = created
                best = content
    return best


def _fit(text: str, remaining: int) -> tuple[str | None, int]:
    count = estimate_tokens(text)
    if count <= remaining:
        return text, count
    if remaining < 8:
        return None, 0
    clipped = tokenizer.decode(tokenizer.encode(text)[:remaining])
    return clipped, estimate_tokens(clipped)


def _pack(
    *,
    peer_id: str,
    workspace_name: str,
    card: list[str],
    conclusions: list[models.Document],
    summary: str | None,
    stale: list[models.Document],
    token_budget: int,
) -> schemas.PeerBrief:
    used = 0
    kept_card: list[str] = []
    for line in card:
        count = estimate_tokens(line)
        if used + count > token_budget:
            break
        kept_card.append(line)
        used += count
    kept_docs: list[models.Document] = []
    for document in conclusions:
        if document.stale:
            continue
        count = estimate_tokens(document.content)
        if used + count > token_budget:
            break
        kept_docs.append(document)
        used += count
    kept_summary: str | None = None
    if summary:
        clipped, count = _fit(summary, token_budget - used)
        if clipped:
            kept_summary = clipped
            used += count
    kept_stale: list[schemas.StaleFlag] = []
    for document in stale:
        line = document.content
        count = estimate_tokens(line)
        if used + count > token_budget:
            break
        kept_stale.append(
            schemas.StaleFlag(
                id=document.id,
                subject_ref=document.subject_ref,
                content=line,
            )
        )
        used += count
    return schemas.PeerBrief(
        peer_id=peer_id,
        card=kept_card,
        conclusions=[schemas.Conclusion.model_validate(doc) for doc in kept_docs],
        summary=kept_summary,
        stale=kept_stale,
        tokens_used=used,
        workspace_id=workspace_name,
    )


async def build_brief(
    db: AsyncSession,
    workspace_name: str,
    peer_id: str,
    *,
    token_budget: int,
    query: str | None = None,
    subject_ref: str | None = None,
    since: datetime.datetime | None = None,
    top_k: int = 10,
    embedding: list[float] | None = None,
) -> schemas.PeerBrief:
    """Card, conclusions, summary, and open stale flags inside a token budget."""
    card = await get_peer_card(db, workspace_name, observer=peer_id, observed=peer_id)
    text = query.strip() if query else ""
    if text:
        conclusions = await search_conclusions(
            db,
            workspace_name,
            text,
            observer=peer_id,
            observed=peer_id,
            subject_ref=subject_ref,
            since=since,
            top_k=top_k,
            embedding=embedding,
        )
    else:
        conclusions = await _recent(
            db,
            workspace_name,
            observer=peer_id,
            observed=peer_id,
            subject_ref=subject_ref,
            since=since,
            top_k=top_k,
        )
    return _pack(
        peer_id=peer_id,
        workspace_name=workspace_name,
        card=card or [],
        conclusions=conclusions,
        summary=await _latest_summary(db, workspace_name, peer_id),
        stale=await _stale_flags(
            db,
            workspace_name,
            observer=peer_id,
            observed=peer_id,
            subject_ref=subject_ref,
        ),
        token_budget=token_budget,
    )


async def prepare_brief(
    workspace_name: str,
    peer_id: str,
    *,
    token_budget: int,
    query: str | None = None,
    subject_ref: str | None = None,
    since: datetime.datetime | None = None,
    top_k: int = 10,
    embedding: list[float] | None = None,
) -> schemas.PeerBrief:
    """Read the brief, then count the conclusions that were returned."""
    from src.crud.peer import reject_scope_observed

    async with tracked_db("peers.brief", read_only=True) as db:
        await reject_scope_observed(
            db,
            workspace_name,
            [peer_id],
            action="Use the scope option on the read routes instead.",
        )
        brief = await build_brief(
            db,
            workspace_name,
            peer_id,
            token_budget=token_budget,
            query=query,
            subject_ref=subject_ref,
            since=since,
            top_k=top_k,
            embedding=embedding,
        )
    ids = [item.id for item in brief.conclusions]
    if ids:
        try:
            async with tracked_db("peers.brief.used") as write_db:
                await note_retrieved(write_db, ids, workspace_name=workspace_name)
                await note_used(write_db, ids, workspace_name=workspace_name)
        except Exception:
            logger.warning("brief use note skipped")
    return brief
