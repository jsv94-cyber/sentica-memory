"""HNSW neighbor order: distance first, then a Python distance cutoff.

Ordering by a boolean before the distance stops Postgres from using the HNSW
index. Stale and deleted rows stay out of the partial index predicate, and
the distance cutoff is applied after a bounded candidate set.

A filtered HNSW scan stops after ``hnsw.ef_search`` candidates. Iterative
scan keeps walking the graph until the limit is filled. Sequential scan and
an explicit sort are turned off for that statement so the planner keeps the
index instead of filtering with a btree and sorting.
"""

from __future__ import annotations

import math

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from src import models

CANDIDATE_FACTOR = 4
CURRENT_PREDICATE = (
    "stale IS FALSE AND deleted_at IS NULL "
    "AND superseded_by IS NULL AND embedding IS NOT NULL"
)
# relaxed_order fills the page. strict_order can abandon the index.
_ITERATIVE_SCAN = "relaxed_order"
_MAX_SCAN_TUPLES = "20000"
_EF_SEARCH = "40"
_HNSW_SCAN_SQL = text(
    "SELECT set_config('hnsw.iterative_scan', :scan, true), "
    "set_config('hnsw.max_scan_tuples', :tuples, true), "
    "set_config('hnsw.ef_search', :ef, true), "
    "set_config('enable_seqscan', 'off', true), "
    "set_config('enable_sort', 'off', true)"
)
_RESTORE_SCAN_SQL = text(
    "SELECT set_config('enable_seqscan', :seqscan, true), "
    "set_config('enable_sort', :sort, true)"
)


def current_row_filters() -> tuple[ColumnElement[bool], ...]:
    """Predicates that match ``ix_documents_embedding_hnsw_current``."""
    return (
        models.Document.stale.is_(False),
        models.Document.deleted_at.is_(None),
        models.Document.superseded_by.is_(None),
        models.Document.embedding.isnot(None),
    )


def neighbor_statement(
    stmt: Select[tuple[models.Document]],
    embedding: list[float],
    limit: int,
) -> Select[tuple[models.Document]]:
    """Order current rows by cosine distance and keep a bounded candidate set."""
    distance = models.Document.embedding.cosine_distance(embedding)
    bounded = max(limit, 1) * CANDIDATE_FACTOR
    return stmt.where(*current_row_filters()).order_by(distance).limit(bounded)


async def apply_hnsw_scan(db: AsyncSession) -> tuple[str, str]:
    """Make a filtered HNSW scan fill its limit.

    Returns the previous ``enable_seqscan`` and ``enable_sort`` values.
    ``SET LOCAL`` lasts for the transaction, so callers restore both after the
    neighbor statement. Turning sort off keeps a btree-plus-sort plan from
    beating the HNSW index on a selective workspace filter.
    """
    # Loading the vector type defines the hnsw.* custom variables.
    await db.execute(text("SELECT CAST('[0]' AS vector)"))
    seqscan = await db.scalar(text("SELECT current_setting('enable_seqscan')"))
    sort = await db.scalar(text("SELECT current_setting('enable_sort')"))
    await db.execute(
        _HNSW_SCAN_SQL,
        {"scan": _ITERATIVE_SCAN, "tuples": _MAX_SCAN_TUPLES, "ef": _EF_SEARCH},
    )
    return (
        "on" if seqscan is None else str(seqscan),
        "on" if sort is None else str(sort),
    )


async def _restore_scan(db: AsyncSession, seqscan: str, sort: str) -> None:
    await db.execute(_RESTORE_SCAN_SQL, {"seqscan": seqscan, "sort": sort})


async def execute_neighbors(
    db: AsyncSession,
    stmt: Select[tuple[models.Document]],
    embedding: list[float],
    limit: int,
) -> list[models.Document]:
    """Return up to ``limit * CANDIDATE_FACTOR`` neighbors from the HNSW index."""
    seqscan, sort = await apply_hnsw_scan(db)
    try:
        result = await db.execute(neighbor_statement(stmt, embedding, limit))
        return list(result.scalars().all())
    finally:
        await _restore_scan(db, seqscan, sort)


def cosine_distance(left: list[float], right: list[float]) -> float:
    """Cosine distance, matching pgvector: ``1 - cosine similarity``."""
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for first, second in zip(left, right, strict=False):
        dot += first * second
        left_norm += first * first
        right_norm += second * second
    if left_norm == 0.0 or right_norm == 0.0:
        return 1.0
    return 1.0 - dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def within_distance(
    rows: list[models.Document],
    embedding: list[float],
    max_distance: float,
    limit: int,
) -> list[models.Document]:
    """Keep the closest rows whose distance is within ``max_distance``."""
    kept: list[models.Document] = []
    for row in rows:
        vector = row.embedding
        if vector is None:
            continue
        values = [float(item) for item in vector]
        if cosine_distance(values, embedding) <= max_distance:
            kept.append(row)
        if len(kept) >= limit:
            break
    return kept
