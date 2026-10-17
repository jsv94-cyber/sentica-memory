"""HNSW neighbor order: distance first, then a Python distance cutoff.

Ordering by a boolean before the distance stops Postgres from using the HNSW
index. Stale and deleted rows stay out of the partial index predicate, and
the distance cutoff is applied after a bounded candidate set.
"""

from __future__ import annotations

import math

from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from src import models

CANDIDATE_FACTOR = 4
CURRENT_PREDICATE = (
    "stale IS FALSE AND deleted_at IS NULL "
    "AND superseded_by IS NULL AND embedding IS NOT NULL"
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
