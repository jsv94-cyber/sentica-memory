"""Counters for filtered conclusions, decay, and prune.

The in-process numbers are always updated. Prometheus is updated only when
metrics are enabled, and a metrics error never leaves this function.
"""

from __future__ import annotations

import logging
import threading

from prometheus_client import Counter

from src.config import settings

logger = logging.getLogger(__name__)

FILTER_CATEGORIES: tuple[str, ...] = (
    "health",
    "sexual",
    "religion",
    "politics",
    "immigration",
    "criminal",
    "finances",
    "leave",
    "model",
    "unspecified",
)

_LOCK = threading.Lock()
_FILTERED: dict[str, int] = {}
_decay_count = 0
_prune_count = 0

_filtered = Counter(
    "conclusions_filtered",
    "Conclusions dropped by the safety filter",
    ["namespace", "category"],
)
_decayed = Counter(
    "conclusions_decayed",
    "Conclusions whose weight was decayed",
    ["namespace"],
)
_pruned = Counter(
    "conclusions_pruned",
    "Soft-deleted conclusions removed after retention",
    ["namespace"],
)


def _namespace() -> str:
    name = settings.METRICS.NAMESPACE
    if isinstance(name, str) and name.strip():
        return name
    return "sentica"


def _inc(counter: Counter, amount: int, **labels: str) -> None:
    if not settings.METRICS.ENABLED or amount <= 0:
        return
    try:
        counter.labels(namespace=_namespace(), **labels).inc(amount)
    except Exception as exc:
        logger.warning("ops metric failed: %s", exc.__class__.__name__)


def note_filtered(category: str) -> None:
    """Count one dropped conclusion."""
    label = category if category in FILTER_CATEGORIES else "unspecified"
    with _LOCK:
        _FILTERED[label] = _FILTERED.get(label, 0) + 1
    _inc(_filtered, 1, category=label)


def note_decay(count: int) -> None:
    """Count conclusions whose weight was updated by decay."""
    global _decay_count
    if count <= 0:
        return
    with _LOCK:
        _decay_count += count
    _inc(_decayed, count)


def note_prune(count: int) -> None:
    """Count conclusions removed after the retention window."""
    global _prune_count
    if count <= 0:
        return
    with _LOCK:
        _prune_count += count
    _inc(_pruned, count)


def filtered(category: str | None = None) -> int:
    """How many conclusions this process has filtered."""
    with _LOCK:
        if category is None:
            return sum(_FILTERED.values())
        return _FILTERED.get(category, 0)


def decayed() -> int:
    """How many conclusions this process has decayed."""
    with _LOCK:
        return _decay_count


def pruned() -> int:
    """How many conclusions this process has pruned."""
    with _LOCK:
        return _prune_count


def reset_ops() -> None:
    """Clear the in-process counters. Tests call this between cases."""
    global _decay_count, _prune_count
    with _LOCK:
        _FILTERED.clear()
        _decay_count = 0
        _prune_count = 0


def initialize_ops_metrics() -> None:
    """Materialize the bounded series at zero."""
    if not settings.METRICS.ENABLED:
        return
    namespace = _namespace()
    try:
        for category in FILTER_CATEGORIES:
            _filtered.labels(namespace=namespace, category=category)
        _decayed.labels(namespace=namespace)
        _pruned.labels(namespace=namespace)
    except Exception as exc:
        logger.warning("ops metric failed: %s", exc.__class__.__name__)
