import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import ReadSessionLocal, SessionLocal, request_context

_OPEN_SESSIONS: ContextVar[int] = ContextVar("sentica_open_db_sessions", default=0)


def open_db_sessions() -> int:
    """How many request or tracked_db sessions are open on this task."""
    return _OPEN_SESSIONS.get()


async def get_db():
    """FastAPI Dependency Generator for Database.

    The session is lazy: it does NOT check out a pooled connection here. The
    AsyncSession checks one out on the first DB-touching call, so a handler doing
    non-DB work (embedding/file/LLM) before its first query does not pin a
    connection across it. The generator still counts as an open session so a
    model check cannot run while the request is holding it.
    """
    db: AsyncSession = SessionLocal()
    depth = _OPEN_SESSIONS.set(_OPEN_SESSIONS.get() + 1)
    try:
        yield db
    except Exception:
        await db.rollback()
        raise
    finally:
        _OPEN_SESSIONS.reset(depth)
        # Always send ROLLBACK unconditionally so the wire-level transaction
        # is closed before the TCP connection drops.  Supavisor v2 does NOT
        # clean up orphaned transactions on client disconnect in transaction-
        # pooling mode, so relying on `in_transaction()` (Python-side state)
        # can leave the backend pinned with an open BEGIN. (Cheap no-op if the
        # lazy session never checked out a connection.)
        await db.rollback()
        await db.close()


async def get_read_db():
    """FastAPI Dependency Generator for SELECT-only handlers.

    Same lazy-checkout semantics as get_db, but the session is bound to the
    read engine: the first statement opens BEGIN READ ONLY and the workspace
    setting is local to that transaction. rollback() in the teardown ends it,
    so the next checkout does not inherit the workspace.

    MUST only be used by handlers that never mutate; see ReadSessionLocal.
    """
    db: AsyncSession = ReadSessionLocal()
    depth = _OPEN_SESSIONS.set(_OPEN_SESSIONS.get() + 1)
    try:
        yield db
    finally:
        _OPEN_SESSIONS.reset(depth)
        # Ends the read-only transaction so the workspace setting dies with it.
        await db.rollback()
        await db.close()


@asynccontextmanager
async def tracked_db(operation_name: str | None = None, *, read_only: bool = False):
    """Context manager for tracked database sessions.

    Sets a task-scoped request_context so the lazy session picks it up for
    tracing/attribution, then yields a lazy session (see get_db).

    Pass read_only=True for SELECT-only windows: the session is then bound to
    the read engine and runs inside BEGIN READ ONLY. Never use read_only=True
    on a path that mutates. See ReadSessionLocal.
    """
    # Get request ID if available, or create operation-specific one
    context = request_context.get()
    token = None

    if not context and operation_name:
        context = f"task:{operation_name}:{str(uuid.uuid4())[:8]}"
        token = request_context.set(context)

    db = (ReadSessionLocal if read_only else SessionLocal)()
    depth = _OPEN_SESSIONS.set(_OPEN_SESSIONS.get() + 1)
    try:
        yield db
    except Exception:
        await db.rollback()
        raise
    finally:
        _OPEN_SESSIONS.reset(depth)
        # Always send ROLLBACK. A read-only session is a real transaction
        # (BEGIN READ ONLY), so rollback ends it and checkin clears
        # sentica.workspace. It is not AUTOCOMMIT and it is not a no-op.
        await db.rollback()
        await db.close()
        if token:  # Only reset if we set it
            request_context.reset(token)


# The test harness replaces the name tracked_db. This alias stays the
# implementation that counts open sessions.
real_tracked_db = tracked_db


db: AsyncSession = Depends(get_db)
read_db: AsyncSession = Depends(get_read_db)
