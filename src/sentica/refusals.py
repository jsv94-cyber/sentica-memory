"""Counts of refused tenants and tokens. Ids and reasons only."""

from __future__ import annotations

from threading import Lock

_lock = Lock()
_counts: dict[str, int] = {}


def record_refusal(reason: str) -> None:
    """Increment the counter for a refusal reason.

    Args:
        reason: Stable label such as ``wrong_tenant`` or ``expired``.
    """
    with _lock:
        _counts[reason] = _counts.get(reason, 0) + 1


def refusal_count(reason: str) -> int:
    """Return how many times ``reason`` has been recorded in this process."""
    with _lock:
        return _counts.get(reason, 0)


def reset_refusals() -> None:
    """Clear counters. Tests use this between cases."""
    with _lock:
        _counts.clear()
