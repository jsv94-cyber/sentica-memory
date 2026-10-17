"""Deterministic scores for the synthetic-business retrieval eval.

No model judges a result. Recall is the share of gold conclusions found in
the top k of hybrid keyword-plus-vector search. An abstention question
scores full marks only when the search returns nothing. A forbidden id in
the results scores zero, which is how a superseded or out-of-date fact fails
the question.

Embeddings are hashed character n-grams of the text. They are not one-hot
slot vectors, so a query is not an exact copy of its gold fact.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime
from typing import NamedTuple

CATEGORIES: tuple[str, ...] = (
    "information_extraction",
    "multi_session_reasoning",
    "temporal_reasoning",
    "knowledge_updates",
    "abstention",
)

# CI fails when a category falls by more than this many points.
DROP_POINTS = 2.0

_RATE_OLD = datetime(2024, 6, 1, tzinfo=UTC)
_RATE_NEW = datetime(2026, 3, 1, tzinfo=UTC)
_RATE_SINCE = datetime(2026, 1, 1, tzinfo=UTC)
_PHONE_OLD = datetime(2023, 1, 15, tzinfo=UTC)
_PHONE_NEW = datetime(2026, 4, 2, tzinfo=UTC)
_PHONE_SINCE = datetime(2025, 1, 1, tzinfo=UTC)


class Fact(NamedTuple):
    key: str
    content: str
    slot: int
    session_name: str
    business: str
    subject_ref: str | None = None
    created_at: datetime | None = None
    phase: int = 1


class Question(NamedTuple):
    category: str
    name: str
    query: str
    slot: int
    business: str
    gold: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    since: datetime | None = None
    abstain: bool = False


def text_embedding(text: str, dimensions: int) -> list[float]:
    """L2-normalized hashed character n-grams.

    Widths 3, 4, and 5 are signed into buckets with SHA-256. Two different
    strings do not land on the same single axis, so vector recall is not 100
    by construction.
    """
    if dimensions < 2:
        raise ValueError("dimensions must be at least 2")
    buckets = [0.0] * dimensions
    folded = " " + text.lower() + " "
    for width in (3, 4, 5):
        if len(folded) < width:
            continue
        for index in range(len(folded) - width + 1):
            gram = folded[index : index + width]
            digest = hashlib.sha256(gram.encode()).digest()
            bucket = int.from_bytes(digest[:4], "big") % dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            buckets[bucket] += sign
    norm = math.sqrt(sum(value * value for value in buckets))
    if norm == 0.0:
        buckets[0] = 1.0
        return buckets
    return [value / norm for value in buckets]


def _shared_marker(seed: str) -> str:
    """One token that a fact and its vector-only query can share as n-grams."""
    return seed * 8


def _vector_only_query(text: str, seed: str) -> str:
    """Keep the English question, and add n-gram pieces that are not that token.

    The pieces overlap the fact's marker, so cosine distance falls under the
    retrieval cutoff. They are not a token of the fact, so keyword search misses.
    """
    marker = _shared_marker(seed)
    parts = [marker[index : index + 6] for index in (0, 6, 12, 18)]
    return text + " " + " ".join(parts + parts)


def _noise() -> tuple[Fact, ...]:
    rows: list[Fact] = []
    for index in range(12):
        rows.append(
            Fact(
                "noise-" + str(index),
                "marker" + f"{index:02d}" + " spool hue" + str(index),
                100 + index,
                "s-main",
                "harbor",
            )
        )
    return tuple(rows)


def _facts() -> tuple[Fact, ...]:
    return _noise() + (
        Fact(
            "ie-invoice",
            "harbor glass bills net thirty from the invoice date "
            + _shared_marker("bqjwm"),
            0,
            "s-main",
            "harbor",
        ),
        Fact(
            "ie-filters",
            "fuel strainers sit in cabinet nine " + _shared_marker("fkpld"),
            2,
            "s-main",
            "northline",
        ),
        Fact(
            "oak-closed",
            "the oak street shop is closed on mondays",
            3,
            "s-mon",
            "harbor",
        ),
        Fact(
            "oak-days",
            "oak street jobs are scheduled tuesday through friday",
            4,
            "s-tue",
            "harbor",
        ),
        Fact(
            "crane-expired",
            "the crane inspection expired in march",
            5,
            "s-mon",
            "harbor",
        ),
        Fact(
            "crane-wait",
            "jobs that need the crane wait for a new inspection certificate",
            6,
            "s-tue",
            "harbor",
        ),
        Fact(
            "rate-old",
            "the weekday service rate is ninety dollars",
            7,
            "s-main",
            "harbor",
            created_at=_RATE_OLD,
        ),
        Fact(
            "rate-new",
            "the weekday service rate is one hundred twenty dollars",
            8,
            "s-main",
            "harbor",
            created_at=_RATE_NEW,
        ),
        Fact(
            "phone-old",
            "the after hours phone is 5550100",
            9,
            "s-main",
            "harbor",
            created_at=_PHONE_OLD,
        ),
        Fact(
            "phone-new",
            "the after hours phone is 5550199",
            10,
            "s-main",
            "harbor",
            created_at=_PHONE_NEW,
        ),
        Fact(
            "owner-old",
            "the owner prefers email for scheduling " + _shared_marker("cnhtr"),
            11,
            "s-main",
            "harbor",
            subject_ref="owner:ada",
        ),
        Fact(
            "owner-new",
            "the owner does not prefer email and instead wants text messages "
            + _shared_marker("cnhtr"),
            11,
            "s-main",
            "harbor",
            subject_ref="owner:ada",
            phase=2,
        ),
        Fact(
            "hours-old",
            "weekday opening time is seven " + _shared_marker("gmwsv"),
            13,
            "s-main",
            "harbor",
            subject_ref="hours:shop",
        ),
        Fact(
            "hours-new",
            "weekday opening time is not seven and instead is eight "
            + _shared_marker("gmwsv"),
            13,
            "s-main",
            "harbor",
            subject_ref="hours:shop",
            phase=2,
        ),
        Fact(
            "secret",
            "northline violetkey 8841",
            50,
            "s-main",
            "northline",
        ),
    )


FACTS: tuple[Fact, ...] = _facts()

QUESTIONS: tuple[Question, ...] = (
    Question(
        "information_extraction",
        "invoice-terms",
        _vector_only_query("when is payment due for the glazing account", "bqjwm"),
        0,
        "harbor",
        gold=("ie-invoice",),
    ),
    Question(
        "information_extraction",
        "filter-cabinet",
        _vector_only_query("which cupboard holds the replacement cartridges", "fkpld"),
        2,
        "northline",
        gold=("ie-filters",),
    ),
    Question(
        "multi_session_reasoning",
        "oak-schedule",
        "oak street",
        900,
        "harbor",
        gold=("oak-closed", "oak-days"),
    ),
    Question(
        "multi_session_reasoning",
        "crane-status",
        "crane inspection",
        901,
        "harbor",
        gold=("crane-expired", "crane-wait"),
    ),
    Question(
        "temporal_reasoning",
        "current-rate",
        "weekday service rate",
        902,
        "harbor",
        gold=("rate-new",),
        forbidden=("rate-old",),
        since=_RATE_SINCE,
    ),
    Question(
        "temporal_reasoning",
        "current-phone",
        "after hours phone",
        903,
        "harbor",
        gold=("phone-new",),
        forbidden=("phone-old",),
        since=_PHONE_SINCE,
    ),
    Question(
        "knowledge_updates",
        "owner-contact",
        _vector_only_query("how should staff reach ada", "cnhtr"),
        11,
        "harbor",
        gold=("owner-new",),
        forbidden=("owner-old",),
    ),
    Question(
        "knowledge_updates",
        "opening-time",
        _vector_only_query("start of the business day", "gmwsv"),
        13,
        "harbor",
        gold=("hours-new",),
        forbidden=("hours-old",),
    ),
    Question(
        "abstention",
        "zephyr",
        "zephyr kiln quota",
        500,
        "harbor",
        abstain=True,
    ),
    Question(
        "abstention",
        "kelp",
        "orbital kelp license",
        501,
        "harbor",
        abstain=True,
    ),
)


def recall_at(gold: set[str], retrieved: list[str], k: int) -> float:
    """Percent of gold ids present in the first k retrieved ids."""
    if not gold:
        return 0.0
    window = set(retrieved[:k])
    return 100.0 * len(gold.intersection(window)) / len(gold)


def question_scores(
    retrieved: list[str],
    gold: set[str],
    forbidden: set[str],
    *,
    abstain: bool,
) -> tuple[float, float]:
    """Recall at 5 and at 10. Abstention and forbidden hits are all or nothing."""
    if abstain:
        points = 100.0 if not retrieved else 0.0
        return points, points
    if forbidden.intersection(retrieved):
        return 0.0, 0.0
    return recall_at(gold, retrieved, 5), recall_at(gold, retrieved, 10)


def category_means(
    rows: list[tuple[str, float, float]],
) -> dict[str, tuple[float, float]]:
    """Mean recall at 5 and at 10 for each category."""
    grouped: dict[str, list[tuple[float, float]]] = {name: [] for name in CATEGORIES}
    for category, recall5, recall10 in rows:
        if category not in grouped:
            raise ValueError("unknown category " + category)
        grouped[category].append((recall5, recall10))
    means: dict[str, tuple[float, float]] = {}
    for name in CATEGORIES:
        samples = grouped[name]
        if len(samples) < 2:
            raise ValueError(name + " needs at least two questions")
        count = len(samples)
        recall5 = sum(item[0] for item in samples) / count
        recall10 = sum(item[1] for item in samples) / count
        means[name] = (recall5, recall10)
    return means


_ROW = re.compile(
    r"^\| ([a-z0-9_]+) \| ([0-9]+(?:\.[0-9]+)?) \| ([0-9]+(?:\.[0-9]+)?) \|$"
)
_LEAKS = re.compile(r"^Leaks: ([0-9]+)$", re.MULTILINE)


def parse_baseline(text: str) -> dict[str, tuple[float, float]]:
    """Read the category table from eval/BASELINE.md."""
    found: dict[str, tuple[float, float]] = {}
    for line in text.splitlines():
        match = _ROW.match(line.strip())
        if match is None:
            continue
        category = match.group(1)
        if category not in CATEGORIES:
            raise ValueError("unknown baseline category " + category)
        if category in found:
            raise ValueError("duplicate baseline category " + category)
        found[category] = (float(match.group(2)), float(match.group(3)))
    missing = [name for name in CATEGORIES if name not in found]
    if missing:
        raise ValueError("baseline missing " + ", ".join(missing))
    return found


def regressions(
    current: dict[str, tuple[float, float]],
    baseline: dict[str, tuple[float, float]],
) -> list[str]:
    """Categories that fell more than DROP_POINTS below the recorded baseline."""
    failures: list[str] = []
    for category in CATEGORIES:
        if category not in current or category not in baseline:
            failures.append(category + " is missing from the score or the baseline")
            continue
        labels = ("recall@5", "recall@10")
        for index, label in enumerate(labels):
            got = current[category][index]
            expected = baseline[category][index]
            if got < expected - DROP_POINTS:
                failures.append(
                    category
                    + " "
                    + label
                    + " "
                    + str(got)
                    + " is more than "
                    + str(DROP_POINTS)
                    + " below "
                    + str(expected)
                )
    return failures


def baseline_leak_count(text: str) -> int:
    """The isolation line in the baseline. There must be exactly one."""
    found = _LEAKS.findall(text)
    if len(found) != 1:
        raise ValueError("baseline must record one leak count")
    return int(found[0])
