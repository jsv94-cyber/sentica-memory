"""Stale flags, superseding, decay, and hard deletion.

An update does not remove the old conclusion. It points ``superseded_by`` at
the new row. Retrieval skips those rows. A scheduled pass halves the weight
of conclusions nobody has used, soft-deletes below a floor, and later purges
those rows. A deletion record stores counts and no text.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from nanoid import generate as generate_nanoid
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from src import models

logger = logging.getLogger(__name__)


@dataclass
class DeletionCounts:
    """Counts for one hard delete. There is no text field."""

    id: str
    peer_name: str | None
    subject_ref: str | None
    document_count: int
    embedding_count: int
    summary_count: int


# Cosine distance. At or below this, the nearest same-subject conclusion is
# the one an update or contradiction replaces. Farther neighbors stay.
_SIMILAR_DISTANCE = 0.35
_CONTRADICTION = re.compile(
    r"\b(no longer|never|instead|does not|do not|is not|isn't|not)\b",
    re.IGNORECASE,
)
_last_scheduled: datetime | None = None


def decay_interval() -> timedelta:
    """How long an unused conclusion waits before its weight halves."""
    return timedelta(days=_positive_float("SENTICA_DECAY_INTERVAL_DAYS", 30))


def decay_floor() -> float:
    """Weight below this is soft-deleted."""
    return _positive_float("SENTICA_DECAY_FLOOR", 0.05)


def retention() -> timedelta:
    """How long a soft-deleted conclusion stays before it is purged."""
    return timedelta(days=_positive_float("SENTICA_DECAY_RETENTION_DAYS", 30))


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _as_floats(raw: Any) -> list[float] | None:
    if raw is None:
        return None
    try:
        values = cast(list[object], list(raw))
    except TypeError:
        return None
    numbers: list[float] = []
    for item in values:
        try:
            numbers.append(float(cast(Any, item)))
        except (TypeError, ValueError):
            return None
    if not numbers:
        return None
    return numbers


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def mark_subject_changed(
    db: AsyncSession, workspace_name: str, subject_ref: str
) -> int:
    """Flag every live conclusion for this subject as stale."""
    result = await db.execute(
        update(models.Document)
        .where(models.Document.workspace_name == workspace_name)
        .where(models.Document.subject_ref == subject_ref)
        .where(models.Document.deleted_at.is_(None))
        .where(models.Document.superseded_by.is_(None))
        .values(stale=True)
    )
    await db.commit()
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)


async def supersede_similar(
    db: AsyncSession,
    document: models.Document,
    *,
    skip_ids: set[str] | None = None,
) -> str:
    """Classify one new conclusion against the nearest fact for its subject.

    Returns ``new``, ``update``, or ``contradiction``. An update or a
    contradiction sets the old row's ``superseded_by`` and leaves it in place.
    """
    if not document.subject_ref:
        return "new"
    embedding = _as_floats(document.embedding)
    if embedding is None:
        return "new"
    distance = models.Document.embedding.cosine_distance(embedding)
    found = (
        await db.execute(
            select(models.Document, distance.label("distance"))
            .where(models.Document.workspace_name == document.workspace_name)
            .where(models.Document.observer == document.observer)
            .where(models.Document.observed == document.observed)
            .where(models.Document.subject_ref == document.subject_ref)
            .where(models.Document.id.not_in(sorted(skip_ids or {document.id})))
            .where(models.Document.deleted_at.is_(None))
            .where(models.Document.superseded_by.is_(None))
            .where(models.Document.embedding.isnot(None))
            .order_by(distance)
            .limit(1)
        )
    ).first()
    if found is None:
        return "new"
    old = found[0]
    distance_value = found[1]
    try:
        distance_number = float(distance_value)
    except (TypeError, ValueError):
        return "new"
    if distance_number > _SIMILAR_DISTANCE:
        return "new"
    relation = "contradiction" if _CONTRADICTION.search(document.content) else "update"
    old.superseded_by = document.id
    metadata = dict(document.internal_metadata or {})
    metadata["relation"] = relation
    document.internal_metadata = metadata
    flag_modified(document, "internal_metadata")
    await db.commit()
    return relation


async def note_retrieved(
    db: AsyncSession, document_ids: list[str], *, workspace_name: str
) -> None:
    """Count one retrieval and treat it as use for the decay clock."""
    await _note(db, document_ids, workspace_name=workspace_name, used=False)


async def note_used(
    db: AsyncSession, document_ids: list[str], *, workspace_name: str
) -> None:
    """Count one conclusion that was actually used in an answer."""
    await _note(db, document_ids, workspace_name=workspace_name, used=True)


async def _note(
    db: AsyncSession,
    document_ids: list[str],
    *,
    workspace_name: str,
    used: bool,
) -> None:
    if not document_ids:
        return
    values: dict[str, Any] = {"last_used_at": func.now()}
    if used:
        values["used_count"] = models.Document.used_count + 1
    else:
        values["retrieved_count"] = models.Document.retrieved_count + 1
    await db.execute(
        update(models.Document)
        .where(models.Document.workspace_name == workspace_name)
        .where(models.Document.id.in_(sorted(document_ids)))
        .values(**values)
    )
    await db.commit()


async def apply_decay(db: AsyncSession, *, workspace_name: str, now: datetime) -> int:
    """Halve unused weights and soft-delete anything under the floor.

    One call applies every interval that has elapsed since the last decay,
    so a clock jump of two intervals halves twice.
    """
    interval = decay_interval()
    floor = decay_floor()
    now = _aware(now)
    rows = list(
        await db.scalars(
            select(models.Document)
            .where(models.Document.workspace_name == workspace_name)
            .where(models.Document.deleted_at.is_(None))
            .where(models.Document.superseded_by.is_(None))
            .order_by(models.Document.id)
        )
    )
    changed = 0
    for row in rows:
        anchor = _aware(row.decayed_at or row.last_used_at or row.created_at)
        steps = int((now - anchor).total_seconds() // interval.total_seconds())
        if steps < 1:
            continue
        weight = float(row.weight)
        for _ in range(steps):
            weight = weight / 2
        row.weight = weight
        row.decayed_at = anchor + (interval * steps)
        if weight < floor:
            row.deleted_at = now
        changed += 1
    await db.commit()
    from src.sentica.ops_metrics import note_decay

    note_decay(changed)
    return changed


async def purge_expired(db: AsyncSession, *, workspace_name: str, now: datetime) -> int:
    """Hard-delete soft-deleted conclusions older than the retention window."""
    cutoff = _aware(now) - retention()
    ids = list(
        await db.scalars(
            select(models.Document.id)
            .where(models.Document.workspace_name == workspace_name)
            .where(models.Document.deleted_at.is_not(None))
            .where(models.Document.deleted_at < cutoff)
            .order_by(models.Document.id)
        )
    )
    if not ids:
        return 0
    await _delete_documents(db, ids)
    await db.commit()
    from src.sentica.ops_metrics import note_prune

    note_prune(len(ids))
    return len(ids)


async def forget(
    db: AsyncSession,
    workspace_name: str,
    *,
    peer_name: str | None = None,
    subject_ref: str | None = None,
) -> DeletionCounts:
    """Hard-delete a peer or a subject, including embeddings and summaries.

    The record is stored on the workspace and contains counts only.
    """
    if (peer_name is None) == (subject_ref is None):
        raise ValueError("set peer_name or subject_ref")
    documents = list(
        await db.scalars(
            _target_documents(
                workspace_name, peer_name=peer_name, subject_ref=subject_ref
            )
        )
    )
    message_ids, public_ids = _message_keys(documents)
    if peer_name is not None:
        peer_messages = (
            await db.execute(_peer_message_stmt(workspace_name, peer_name))
        ).all()
        for message_id, public_id in peer_messages:
            message_ids.add(int(message_id))
            public_ids.add(str(public_id))
    embedding_count = await _count_embeddings(db, workspace_name, public_ids)
    summary_count = await _scrub_summaries(db, workspace_name, message_ids)
    await _delete_messages(db, workspace_name, message_ids)
    await _delete_documents(db, [document.id for document in documents])
    record = DeletionCounts(
        id=generate_nanoid(),
        peer_name=peer_name,
        subject_ref=subject_ref,
        document_count=len(documents),
        embedding_count=embedding_count,
        summary_count=summary_count,
    )
    await _store_deletion(db, workspace_name, record)
    await db.commit()
    return record


async def _store_deletion(
    db: AsyncSession, workspace_name: str, record: DeletionCounts
) -> None:
    workspace = await db.scalar(
        select(models.Workspace).where(models.Workspace.name == workspace_name)
    )
    if workspace is None:
        return
    raw_meta: Any = workspace.internal_metadata or {}
    metadata = cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
    existing = metadata.get("deletion_records")
    records = cast(list[object], existing) if isinstance(existing, list) else []
    stored = {
        "id": record.id,
        "peer_name": record.peer_name,
        "subject_ref": record.subject_ref,
        "document_count": record.document_count,
        "embedding_count": record.embedding_count,
        "summary_count": record.summary_count,
    }
    updated = dict(metadata)
    updated["deletion_records"] = [*records, stored]
    workspace.internal_metadata = updated
    flag_modified(workspace, "internal_metadata")


def _peer_message_stmt(workspace_name: str, peer_name: str) -> Any:
    """Messages owned by one peer in one workspace."""
    return select(models.Message.id, models.Message.public_id).where(
        models.Message.workspace_name == workspace_name,
        models.Message.peer_name == peer_name,
    )


def _target_documents(
    workspace_name: str, *, peer_name: str | None, subject_ref: str | None
) -> Any:
    stmt = select(models.Document).where(
        models.Document.workspace_name == workspace_name
    )
    if subject_ref is not None:
        return stmt.where(models.Document.subject_ref == subject_ref)
    return stmt.where(
        (models.Document.observer == peer_name)
        | (models.Document.observed == peer_name)
    )


def _message_keys(
    documents: list[models.Document],
) -> tuple[set[int], set[str]]:
    message_ids: set[int] = set()
    public_ids: set[str] = set()
    for document in documents:
        raw_meta: Any = document.internal_metadata or {}
        if not isinstance(raw_meta, dict):
            continue
        metadata = cast(dict[str, Any], raw_meta)
        raw_ids = metadata.get("message_ids")
        if isinstance(raw_ids, list):
            for item in cast(list[object], raw_ids):
                if isinstance(item, int):
                    message_ids.add(item)
        raw_public = metadata.get("message_public_ids")
        if isinstance(raw_public, list):
            for item in cast(list[object], raw_public):
                if isinstance(item, str):
                    public_ids.add(item)
    return message_ids, public_ids


async def _count_embeddings(
    db: AsyncSession, workspace_name: str, public_ids: set[str]
) -> int:
    if not public_ids:
        return 0
    count = await db.scalar(
        select(func.count())
        .select_from(models.MessageEmbedding)
        .where(models.MessageEmbedding.workspace_name == workspace_name)
        .where(models.MessageEmbedding.message_id.in_(sorted(public_ids)))
    )
    return int(count or 0)


async def _scrub_summaries(
    db: AsyncSession, workspace_name: str, message_ids: set[int]
) -> int:
    if not message_ids:
        return 0
    removed = 0
    sessions = list(
        await db.scalars(
            select(models.Session)
            .where(models.Session.workspace_name == workspace_name)
            .order_by(models.Session.id)
        )
    )
    for session in sessions:
        raw_meta: Any = session.internal_metadata or {}
        if not isinstance(raw_meta, dict):
            continue
        metadata = cast(dict[str, Any], raw_meta)
        summaries = metadata.get("summaries")
        if not isinstance(summaries, dict):
            continue
        kept: dict[str, Any] = {}
        session_removed = 0
        summary_map = cast(dict[object, object], summaries)
        for key, value in summary_map.items():
            message_id = (
                cast(dict[str, Any], value).get("message_id")
                if isinstance(value, dict)
                else None
            )
            if isinstance(message_id, int) and message_id in message_ids:
                session_removed += 1
                continue
            kept[str(key)] = value
        if session_removed:
            updated = dict(metadata)
            updated["summaries"] = kept
            session.internal_metadata = updated
            flag_modified(session, "internal_metadata")
            removed += session_removed
    return removed


async def _delete_messages(
    db: AsyncSession, workspace_name: str, message_ids: set[int]
) -> None:
    if not message_ids:
        return
    ids = sorted(message_ids)
    await db.execute(
        delete(models.QueueItem).where(
            models.QueueItem.workspace_name == workspace_name,
            models.QueueItem.message_id.in_(ids),
        )
    )
    await db.execute(
        delete(models.Message).where(
            models.Message.workspace_name == workspace_name,
            models.Message.id.in_(ids),
        )
    )


async def _delete_documents(db: AsyncSession, document_ids: list[str]) -> None:
    if not document_ids:
        return
    ids = sorted(document_ids)
    await db.execute(
        delete(models.DocumentSource).where(
            models.DocumentSource.derived_id.in_(ids)
            | models.DocumentSource.source_id.in_(ids)
        )
    )
    await db.execute(delete(models.Document).where(models.Document.id.in_(ids)))


async def decay_due_workspaces() -> None:
    """Run decay and purge for every workspace, at most once an hour."""
    global _last_scheduled
    now = datetime.now(UTC)
    if _last_scheduled is not None and now - _last_scheduled < timedelta(hours=1):
        return
    _last_scheduled = now
    from src.dependencies import tracked_db
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    try:
        async with tracked_db("freshness_plan", read_only=True) as db:
            names = list(await db.scalars(select(models.Workspace.name)))
    except Exception:
        logger.warning("Freshness pass could not list workspaces", exc_info=True)
        return
    for name in names:
        token = set_workspace(name)
        try:
            async with tracked_db("freshness") as db:
                await apply_decay(db, workspace_name=name, now=now)
                await purge_expired(db, workspace_name=name, now=now)
        except Exception:
            logger.warning("Freshness pass failed for a workspace", exc_info=True)
        finally:
            reset_workspace(token)
