"""Cached readiness. A successful database check is reused for a few seconds."""

from __future__ import annotations

import time

_READY_TTL_SECONDS = 5.0
_cached_until = 0.0


def reset_ready_cache() -> None:
    """Drop the cached success. Tests call this between cases."""
    global _cached_until
    _cached_until = 0.0


def ready_cache_deadline() -> float:
    """Monotonic time until which a success is reused. Zero means uncached."""
    return _cached_until


async def database_ready() -> bool:
    """Return whether Postgres answered ``SELECT 1``.

    A success is cached for a few seconds so a probe does not open a new
    connection on every call. A failure is not cached.
    """
    global _cached_until
    now = time.monotonic()
    if now < _cached_until:
        return True
    from sqlalchemy import text

    from src.dependencies import tracked_db

    try:
        async with tracked_db("ready") as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        return False
    _cached_until = time.monotonic() + _READY_TTL_SECONDS
    return True
