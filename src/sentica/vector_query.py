"""HNSW neighbor order: fresh distance first, then stale distance.

Ordering by a boolean before the distance stops Postgres from using the HNSW
index, so fresh and stale rows are two scans. The fresh scan matches the
partial index. The stale scan uses the full HNSW index and is appended after
the fresh rows. Superseded and deleted rows stay out of both.

A filtered HNSW scan stops after ``hnsw.ef_search`` candidates. Iterative
scan keeps walking the graph until the limit is filled. Sequential scan and
an explicit sort are turned off for that statement so the planner keeps the
index instead of filtering with a btree and sorting.
"""

from __future__ import annotations

import contextlib
import math
from typing import NamedTuple

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
_MAX_SCAN_TUPLES = "100000"
_EF_SEARCH = "200"
_HNSW_SCAN_SQL = text(
    "SELECT set_config('hnsw.iterative_scan', :scan, true), "
    + "set_config('hnsw.max_scan_tuples', :tuples, true), "
    + "set_config('hnsw.ef_search', :ef, true), "
    + "set_config('enable_seqscan', 'off', true), "
    + "set_config('enable_sort', 'off', true)"
)
_RESTORE_SCAN_SQL = text(
    "SELECT set_config('hnsw.iterative_scan', :scan, true), "
    + "set_config('hnsw.max_scan_tuples', :tuples, true), "
    + "set_config('hnsw.ef_search', :ef, true), "
    + "set_config('enable_seqscan', :seqscan, true), "
    + "set_config('enable_sort', :sort, true)"
)


class _ScanState(NamedTuple):
    """Values to put back after one neighbor scan."""

    iterative: str
    tuples: str
    ef: str
    seqscan: str
    sort: str


def _setting(value: object, default: str) -> str:
    if value is None:
        return default
    text_value = str(value).strip()
    return text_value or default


def current_row_filters() -> tuple[ColumnElement[bool], ...]:
    """Predicates that match ``ix_documents_embedding_hnsw_current``."""
    return (
        models.Document.stale.is_(False),
        models.Document.deleted_at.is_(None),
        models.Document.superseded_by.is_(None),
        models.Document.embedding.isnot(None),
    )


def stale_row_filters() -> tuple[ColumnElement[bool], ...]:
    """Stale rows that are still live. The full HNSW index covers these."""
    return (
        models.Document.stale.is_(True),
        models.Document.deleted_at.is_(None),
        models.Document.superseded_by.is_(None),
        models.Document.embedding.isnot(None),
    )


def _distance_page(
    stmt: Select[tuple[models.Document]],
    embedding: list[float],
    limit: int,
    filters: tuple[ColumnElement[bool], ...],
) -> Select[tuple[models.Document]]:
    distance = models.Document.embedding.cosine_distance(embedding)
    bounded = max(limit, 1) * CANDIDATE_FACTOR
    return stmt.where(*filters).order_by(distance).limit(bounded)


def neighbor_statement(
    stmt: Select[tuple[models.Document]],
    embedding: list[float],
    limit: int,
) -> Select[tuple[models.Document]]:
    """Order current rows by cosine distance and keep a bounded candidate set."""
    return _distance_page(stmt, embedding, limit, current_row_filters())


def stale_neighbor_statement(
    stmt: Select[tuple[models.Document]],
    embedding: list[float],
    limit: int,
) -> Select[tuple[models.Document]]:
    """Order stale live rows by cosine distance, after the fresh page."""
    return _distance_page(stmt, embedding, limit, stale_row_filters())


async def apply_hnsw_scan(db: AsyncSession) -> _ScanState:
    """Make a filtered HNSW scan fill its limit.

    Returns every setting ``_HNSW_SCAN_SQL`` changes. ``SET LOCAL`` lasts for
    the transaction, so callers restore all of them after the neighbor
    statement. Turning sort off keeps a btree-plus-sort plan from beating the
    HNSW index on a selective workspace filter.
    """
    # Loading the vector type defines the hnsw.* custom variables.
    await db.execute(text("SELECT CAST('[0]' AS vector)"))
    iterative = await db.scalar(
        text("SELECT current_setting('hnsw.iterative_scan', true)")
    )
    tuples = await db.scalar(
        text("SELECT current_setting('hnsw.max_scan_tuples', true)")
    )
    ef = await db.scalar(text("SELECT current_setting('hnsw.ef_search', true)"))
    seqscan = await db.scalar(text("SELECT current_setting('enable_seqscan')"))
    sort = await db.scalar(text("SELECT current_setting('enable_sort')"))
    await db.execute(
        _HNSW_SCAN_SQL,
        {"scan": _ITERATIVE_SCAN, "tuples": _MAX_SCAN_TUPLES, "ef": _EF_SEARCH},
    )
    return _ScanState(
        iterative=_setting(iterative, "off"),
        tuples=_setting(tuples, "20000"),
        ef=_setting(ef, "40"),
        seqscan=_setting(seqscan, "on"),
        sort=_setting(sort, "on"),
    )


async def _restore_scan(db: AsyncSession, state: _ScanState) -> None:
    await db.execute(
        _RESTORE_SCAN_SQL,
        {
            "scan": state.iterative,
            "tuples": state.tuples,
            "ef": state.ef,
            "seqscan": state.seqscan,
            "sort": state.sort,
        },
    )


_EXACT_SCAN_SQL = text(
    "SELECT set_config('enable_indexscan', 'off', true), "
    + "set_config('enable_bitmapscan', 'off', true), "
    + "set_config('enable_seqscan', 'on', true), "
    + "set_config('enable_sort', 'on', true)"
)
_INDEX_MODE_SQL = text(
    "SELECT set_config('enable_indexscan', :indexscan, true), "
    + "set_config('enable_bitmapscan', :bitmap, true), "
    + "set_config('enable_seqscan', 'off', true), "
    + "set_config('enable_sort', 'off', true), "
    + "set_config('hnsw.iterative_scan', :scan, true), "
    + "set_config('hnsw.max_scan_tuples', :tuples, true), "
    + "set_config('hnsw.ef_search', :ef, true)"
)


async def _index_mode(db: AsyncSession, indexscan: str, bitmap: str) -> None:
    await db.execute(
        _INDEX_MODE_SQL,
        {
            "indexscan": indexscan,
            "bitmap": bitmap,
            "scan": _ITERATIVE_SCAN,
            "tuples": _MAX_SCAN_TUPLES,
            "ef": _EF_SEARCH,
        },
    )


async def _exact_page(
    db: AsyncSession,
    stmt: Select[tuple[models.Document]],
) -> list[models.Document]:
    """Order the same predicate without the HNSW index.

    Dead index entries can make a filtered scan stop before the limit. The
    exact scan is the page when it finds more rows than the index did.
    """
    indexscan = _setting(
        await db.scalar(text("SELECT current_setting('enable_indexscan')")),
        "on",
    )
    bitmap = _setting(
        await db.scalar(text("SELECT current_setting('enable_bitmapscan')")),
        "on",
    )
    await db.execute(_EXACT_SCAN_SQL)
    try:
        result = await db.execute(stmt)
        rows = list(result.scalars().all())
    except Exception:
        with contextlib.suppress(Exception):
            await _index_mode(db, indexscan, bitmap)
        raise
    await _index_mode(db, indexscan, bitmap)
    return rows


async def _page(
    db: AsyncSession,
    stmt: Select[tuple[models.Document]],
    limit: int,
) -> list[models.Document]:
    wanted = max(limit, 1) * CANDIDATE_FACTOR
    indexed = list((await db.execute(stmt)).scalars().all())
    if len(indexed) >= wanted:
        return indexed
    exact = await _exact_page(db, stmt)
    if len(exact) > len(indexed):
        return exact
    return indexed


async def execute_neighbors(
    db: AsyncSession,
    stmt: Select[tuple[models.Document]],
    embedding: list[float],
    limit: int,
) -> list[models.Document]:
    """Return fresh neighbors, then stale ones, from the HNSW indexes."""
    state = await apply_hnsw_scan(db)
    try:
        fresh = await _page(db, neighbor_statement(stmt, embedding, limit), limit)
        stale = await _page(db, stale_neighbor_statement(stmt, embedding, limit), limit)
        return fresh + stale
    finally:
        await _restore_scan(db, state)


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
