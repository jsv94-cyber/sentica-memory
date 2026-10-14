import asyncio
import contextvars
import logging
import os
import socket
from typing import Any, cast

from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool, QueuePool

from src.config import settings
from src.sentica.db_connect import (
    apply_production_engine_args,
    install_certificate_reload,
)
from src.sentica.log_policy import install_production_log_filter
from src.sentica.runtime import capped_pool_recycle
from src.telemetry.prometheus.metrics import (
    db_connections_established_counter,
    db_connections_open_gauge,
    db_queries_in_flight_gauge,
)

logger = logging.getLogger(__name__)

connect_args = {
    "prepare_threshold": None,
    # Bound a single connection attempt so it fails fast instead of hanging when
    # the server/pooler is unreachable or stalled (psycopg, seconds).
    "connect_timeout": settings.DB.CONNECT_TIMEOUT_SECONDS,
}

# Context variable to store request context
request_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_context", default=None
)

engine_kwargs: dict[str, Any] = {}

_sentica_instance = apply_production_engine_args(connect_args, engine_kwargs)

if settings.DB.POOL_CLASS == "null":
    engine_kwargs["poolclass"] = NullPool
else:
    # Only add pool-related kwargs for pooled connections
    engine_kwargs.update(
        {
            "pool_pre_ping": settings.DB.POOL_PRE_PING,
            "pool_size": settings.DB.POOL_SIZE,
            "max_overflow": settings.DB.MAX_OVERFLOW,
            "pool_timeout": settings.DB.POOL_TIMEOUT,
            "pool_recycle": settings.DB.POOL_RECYCLE,
            "pool_use_lifo": settings.DB.POOL_USE_LIFO,
        }
    )
    if _sentica_instance is not None:
        engine_kwargs["pool_recycle"] = capped_pool_recycle(
            int(engine_kwargs["pool_recycle"])
        )
        engine_kwargs["pool_timeout"] = min(int(engine_kwargs["pool_timeout"]), 8)
        engine_kwargs["hide_parameters"] = True

_connection_uri = settings.DB.CONNECTION_URI
if _sentica_instance is not None:
    _connection_uri = _sentica_instance.connection_uri()
    settings.DB.SCHEMA = "memory"
    settings.DB.CONNECTION_URI = _connection_uri

engine = create_async_engine(
    _connection_uri,
    connect_args=connect_args,
    echo=False if _sentica_instance is not None else settings.DB.SQL_DEBUG,
    **engine_kwargs,
)

if _sentica_instance is not None:
    install_certificate_reload(engine, _sentica_instance)
    install_production_log_filter()

# API requests set this to 8 seconds. Deriver and maintenance leave it unset
# and use SENTICA_DB_CALL_TIMEOUT_SECONDS, which defaults to 120.
API_DB_CALL_TIMEOUT_SECONDS = 8.0
db_call_timeout: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "sentica_db_call_timeout", default=None
)


def maintenance_db_call_timeout() -> float:
    """Bound for deriver and maintenance sessions. Defaults to 120 seconds."""
    raw = os.environ.get("SENTICA_DB_CALL_TIMEOUT_SECONDS", "120").strip()
    try:
        value = float(raw)
    except ValueError:
        return 120.0
    if value <= 0:
        return 120.0
    return value


def _session_call_timeout() -> float:
    selected = db_call_timeout.get()
    if selected is None:
        return maintenance_db_call_timeout()
    return selected


def _fileno(dbapi_connection: Any) -> int | None:
    current: Any = dbapi_connection
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        pgconn = getattr(current, "pgconn", None)
        fileno = getattr(pgconn, "socket", None) if pgconn is not None else None
        if isinstance(fileno, int) and fileno >= 0:
            return fileno
        nxt = getattr(current, "_connection", None)
        if nxt is None:
            nxt = getattr(current, "driver_connection", None)
        current = nxt
    return None


# psycopg Ready.RW. The driver wait records this before it reads the socket.
_DRIVER_READY_RW = 3


def _wake_task_events(task: asyncio.Task[Any]) -> None:
    seen: set[int] = set()

    def visit(obj: Any, depth: int) -> None:
        if obj is None or depth > 20 or id(obj) in seen:
            return
        seen.add(id(obj))
        frame = getattr(obj, "cr_frame", None) or getattr(obj, "gr_frame", None)
        if frame is not None and hasattr(frame, "f_locals"):
            wakeup = frame.f_locals.get("wakeup")
            if frame.f_code.co_name == "wait_conn_async" and callable(wakeup):
                # A bare event wake reports "not ready", and the driver waits
                # again. Report the socket ready so it sees the shutdown.
                wakeup(_DRIVER_READY_RW)
            for value in frame.f_locals.values():
                if isinstance(value, asyncio.Event):
                    value.set()
            context = frame.f_locals.get("context")
            if context is not None and context is not obj:
                visit(context, depth + 1)
        nxt = getattr(obj, "cr_await", None)
        if nxt is not None and nxt is not obj:
            visit(nxt, depth + 1)

    visit(task.get_coro(), 0)


def _shutdown_fileno(fileno: int) -> None:
    try:
        held = socket.socket(fileno=fileno)
    except OSError:
        return
    try:
        held.shutdown(socket.SHUT_RDWR)
    except OSError:
        return
    finally:
        held.detach()


def _filenos_in_task(task: asyncio.Task[Any]) -> list[int]:
    """Sockets this call is blocked on, including a connect still in progress.

    A paused proxy accepts TCP and then never answers. The session has no
    connection yet, so the socket only exists in the driver wait frames.
    """
    found: list[int] = []
    seen: set[int] = set()

    def visit(obj: Any, depth: int) -> None:
        if obj is None or depth > 20 or id(obj) in seen:
            return
        seen.add(id(obj))
        frame = getattr(obj, "cr_frame", None) or getattr(obj, "gr_frame", None)
        if frame is not None and hasattr(frame, "f_locals"):
            filename = frame.f_code.co_filename
            for key, value in frame.f_locals.items():
                pgconn = getattr(value, "pgconn", None)
                sock = getattr(pgconn, "socket", None) if pgconn is not None else None
                if isinstance(sock, int) and sock >= 0:
                    found.append(sock)
                if (
                    key == "fileno"
                    and isinstance(value, int)
                    and value >= 0
                    and filename.endswith("waiting.py")
                ):
                    found.append(value)
            context = frame.f_locals.get("context")
            if context is not None and context is not obj:
                visit(context, depth + 1)
        nxt = getattr(obj, "cr_await", None)
        if nxt is not None and nxt is not obj:
            visit(nxt, depth + 1)

    visit(task.get_coro(), 0)
    return found


def _stop_session_connection(session: AsyncSession) -> None:
    """Close the socket for this session so a timed-out call cannot commit."""
    sync = session.sync_session
    try:
        transaction = sync.get_transaction()
    except Exception:
        return
    if transaction is None:
        return
    # ``SessionTransaction.connection`` is a method. The checked-out
    # connections live on ``_connections``.
    records = getattr(transaction, "_connections", None)
    if not records:
        return
    seen: set[int] = set()
    for record in records.values():
        if not isinstance(record, tuple) or not record:
            continue
        connection = cast(Any, record[0])
        if connection is None or id(connection) in seen:
            continue
        seen.add(id(connection))
        fairy = getattr(connection, "_dbapi_connection", None)
        if fairy is None:
            continue
        driver = getattr(fairy, "driver_connection", None)
        fileno = _fileno(driver if driver is not None else fairy)
        if fileno is None:
            fileno = _fileno(fairy)
        if fileno is None:
            continue
        _shutdown_fileno(fileno)


class _BoundedSession(AsyncSession):
    """Bound one database call without bounding the whole request.

    API requests use 8 seconds. A paused proxy still acknowledges TCP, so
    the bound lives in this process. Deriver and maintenance sessions use
    the longer configurable bound. The call returns even if the driver
    does not notice cancellation, and the socket is closed so the
    statement cannot commit later.
    """

    async def _bounded(self, awaitable: Any) -> Any:
        task: asyncio.Task[Any] = asyncio.create_task(awaitable)
        done, _pending = await asyncio.wait({task}, timeout=_session_call_timeout())
        if task in done:
            return task.result()
        # Close the socket, then let the driver call finish. Teardown
        # rollback must not overlap a connection that is still opening.
        _stop_session_connection(self)
        # Do not cancel. Cancellation writes a cancel message the paused
        # proxy never reads. Shut the socket down and tell the driver it is
        # ready so the handshake observes the error instead of waiting again.
        for _ in range(3):
            for fileno in _filenos_in_task(task):
                _shutdown_fileno(fileno)
            _wake_task_events(task)
            await asyncio.wait({task}, timeout=0.2)
            if task.done():
                break
        if task.done() and not task.cancelled():
            error = task.exception()
            if error is not None:
                raise TimeoutError from error
        raise TimeoutError

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._bounded(super().execute(statement, *args, **kwargs))

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._bounded(super().scalar(statement, *args, **kwargs))

    async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._bounded(super().scalars(statement, *args, **kwargs))

    async def commit(self) -> None:
        await self._bounded(super().commit())

    async def flush(self, objects: Any = None) -> None:
        await self._bounded(super().flush(objects))


_session_class = _BoundedSession if _sentica_instance is not None else AsyncSession
# Public name so tests can install the same bound the API uses.
BoundedSession = _BoundedSession

# A vanilla AsyncSession is lazy: it checks out a pooled connection on the first
# DB-touching call (not at construction) and couples the checkout to the
# statement, so a handler doing non-DB work (embedding/file/LLM) before its
# first query does not pin a connection across it. Connection acquisition is a
# single attempt with no retry — callers handle a saturated/unreachable DB (the
# API surfaces the error; the deriver backs off and retries on a later poll).
SessionLocal = async_sessionmaker(
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    bind=engine,
    class_=_session_class,
)

# Read-only engine: shares `engine`'s pool, but checks connections out in DBAPI
# AUTOCOMMIT mode, so psycopg emits NO BEGIN — a SELECT never autobegins a
# transaction. The backend therefore returns to state 'idle' (not 'idle in
# transaction') the moment a statement completes.
read_engine = engine.execution_options(isolation_level="AUTOCOMMIT")

# Sessions for SELECT-only work (same lazy-checkout semantics as SessionLocal).
# MUST NOT be used for writes: with no enclosing transaction, begin_nested()
# savepoints (see the crud get-or-create paths) break, and every flush would
# commit immediately. Use SessionLocal for anything that mutates.
ReadSessionLocal = async_sessionmaker(
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    bind=read_engine,
    class_=_session_class,
)


def _set_application_name_on_checkout(
    dbapi_connection: Any, _connection_record: Any, _connection_proxy: Any
) -> None:
    """Tag each checked-out connection with the current request context.

    Registered only when ``DB.TRACING`` is on. Fires on every pool checkout (so a
    reused pooled connection is re-tagged for the new caller), reading the
    per-task ``request_context`` the request/task scope has already set.
    Best-effort: a failure here must never break the checkout.

    Runs in autocommit so it never leaves the connection 'idle in transaction'
    at checkout: this hook fires BEFORE the dialect applies execution-option
    isolation levels, and psycopg refuses to switch a connection into AUTOCOMMIT
    (which the read engine does) while a transaction opened by this statement is
    still in progress. set_config(..., is_local=false) is session-scoped, so it
    persists past the autocommit boundary.
    """
    context = request_context.get() or "unknown"
    try:
        previous_autocommit = dbapi_connection.autocommit
        if not previous_autocommit:
            dbapi_connection.autocommit = True
        try:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(
                    "SELECT set_config('application_name', %s, false)", (context,)
                )
            finally:
                cursor.close()
        finally:
            if not previous_autocommit:
                dbapi_connection.autocommit = False
    except Exception:
        logger.debug("setting application_name on checkout failed", exc_info=True)


if settings.DB.TRACING:
    event.listen(engine.sync_engine, "checkout", _set_application_name_on_checkout)


def get_pool_stats() -> dict[str, int]:
    """Return live connection-pool stats for this process.

    ``engine.pool`` is the AsyncEngine's pool (the same object as
    ``engine.sync_engine.pool``); its stat methods are synchronous counter
    reads with no I/O, so they are safe to call without ``await``. Returns
    zeros for pools that do not track connections (e.g. ``NullPool``).
    """
    zeros = {"checked_out": 0, "checked_in": 0, "size": 0, "overflow": 0}
    pool = engine.pool
    # Only QueuePool (and its AsyncAdaptedQueuePool subclass) tracks connection
    # counts; NullPool and others have no meaningful stats.
    if not isinstance(pool, QueuePool):
        return zeros
    try:
        # overflow() is negative until the base pool fills (it starts at
        # -pool_size); clamp to the count of overflow connections actually open.
        return {
            "checked_out": pool.checkedout(),
            "checked_in": pool.checkedin(),
            "size": pool.size(),
            "overflow": max(0, pool.overflow()),
        }
    except Exception:
        return zeros


class DBQueryInflightTracker:
    """Tracks statements executing on the wire via SQLAlchemy cursor events.

    Drift-proof: marks ``Connection.info`` when a statement starts and clears it
    on completion OR error, so the gauge can't leak upward (an errored statement
    skips ``after_cursor_execute``) or go negative (a connect-time error has no
    matching start). Bound to a pre-resolved labeled gauge child so the
    per-statement hot path does no label resolution.
    """

    # Marker on Connection.info recording that we incremented for the current
    # statement, so we decrement exactly once on completion or error.
    INFLIGHT_KEY: str = "_honcho_inflight"

    def __init__(self, gauge_child: Any) -> None:
        self._child: Any = gauge_child

    def on_before(self, conn: Any, *_: Any) -> None:
        try:
            conn.info[self.INFLIGHT_KEY] = True
            self._child.inc()
        except Exception:
            logger.debug("in-flight gauge inc failed", exc_info=True)

    def on_after(self, conn: Any, *_: Any) -> None:
        try:
            if conn.info.pop(self.INFLIGHT_KEY, False):
                self._child.dec()
        except Exception:
            logger.debug("in-flight gauge dec failed", exc_info=True)

    def on_error(self, exception_context: Any) -> None:
        try:
            conn = exception_context.connection
            if conn is not None and conn.info.pop(self.INFLIGHT_KEY, False):
                self._child.dec()
        except Exception:
            logger.debug("in-flight gauge error-path dec failed", exc_info=True)


# Process-wide tracker, created at registration (None until then / if metrics off).
_inflight_tracker: DBQueryInflightTracker | None = None


_db_query_instrumentation_registered = False


def register_db_query_instrumentation(instance_type: str) -> None:
    """Attach per-statement in-flight tracking to the engine (no-op if off).

    Gated on METRICS.ENABLED so there is zero overhead — not even attached event
    listeners — when metrics are disabled. Idempotent: repeated calls (e.g. a
    re-run lifespan or test startup) won't attach duplicate listeners, which
    would double-count in-flight statements.
    """
    global _inflight_tracker, _db_query_instrumentation_registered
    if not settings.METRICS.ENABLED or _db_query_instrumentation_registered:
        return
    child = db_queries_in_flight_gauge.labels(instance_type=instance_type)
    _inflight_tracker = DBQueryInflightTracker(child)
    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _inflight_tracker.on_before)
    event.listen(sync_engine, "after_cursor_execute", _inflight_tracker.on_after)
    event.listen(sync_engine, "handle_error", _inflight_tracker.on_error)
    _db_query_instrumentation_registered = True


class DBConnectionTracker:
    """Tracks physical DB connections open on this engine via pool lifecycle events.

    Drift-proof, mirroring ``DBQueryInflightTracker``: marks the ``ConnectionRecord``
    on ``connect`` and decrements only if that mark is still present on
    ``close``/``invalidate``, so each physical connection increments the gauge
    exactly once and decrements at most once — it can't leak upward or go negative
    when both events fire during invalidation cleanup. Works for every pool class,
    including ``NullPool`` (whose pool keeps no records, so the scrape-time
    ``db_pool_connections`` collector reads zero).
    """

    # Marker on ConnectionRecord.info recording that we incremented for this
    # connection, so we decrement exactly once across close/invalidate.
    OPEN_KEY: str = "_honcho_conn_open"

    def __init__(self, open_child: Any, established_child: Any) -> None:
        self._open: Any = open_child
        self._established: Any = established_child

    def on_connect(self, _dbapi_connection: Any, connection_record: Any) -> None:
        try:
            connection_record.info[self.OPEN_KEY] = True
            self._established.inc()
            self._open.inc()
        except Exception:
            logger.debug("db-connection gauge inc failed", exc_info=True)

    def on_close(self, _dbapi_connection: Any, connection_record: Any, *_: Any) -> None:
        try:
            if connection_record is not None and connection_record.info.pop(
                self.OPEN_KEY, False
            ):
                self._open.dec()
        except Exception:
            logger.debug("db-connection gauge dec failed", exc_info=True)


# Process-wide tracker, created at registration (None until then / if metrics off).
_connection_tracker: DBConnectionTracker | None = None


_db_connection_instrumentation_registered = False


def register_db_connection_instrumentation(instance_type: str) -> None:
    """Attach physical-connection tracking to the engine (no-op if metrics off).

    Counts connections via pool lifecycle events, so it reports real numbers under
    any pool class — unlike the pool-object collector, which reads zero under
    ``NullPool``. Pre-resolving the labeled children materializes both series at 0,
    so an absent series signals a broken scrape rather than "no connections" (the
    zero-init convention). Idempotent: repeated calls won't attach duplicate
    listeners, which would double-count connections.
    """
    global _connection_tracker, _db_connection_instrumentation_registered
    if not settings.METRICS.ENABLED or _db_connection_instrumentation_registered:
        return
    open_child = db_connections_open_gauge.labels(instance_type=instance_type)
    established_child = db_connections_established_counter.labels(
        instance_type=instance_type
    )
    _connection_tracker = DBConnectionTracker(open_child, established_child)
    sync_engine = engine.sync_engine
    event.listen(sync_engine, "connect", _connection_tracker.on_connect)
    # A connection is torn down by close (normal return / recycle discard),
    # invalidate (broken connection), or detach — the last fires on GC-cleanup of an
    # abandoned async connection, where NullPool's close is a no-op so `close` never
    # fires. All three carry the ConnectionRecord; the marker dedupes if more than
    # one fires for the same connection.
    for teardown_event in ("close", "invalidate", "detach"):
        event.listen(sync_engine, teardown_event, _connection_tracker.on_close)
    _db_connection_instrumentation_registered = True


# Define your naming convention
convention = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",  # Index - supports multi-column
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",  # Unique constraint - supports multi-column
    "ck": "ck_%(table_name)s_%(constraint_name)s",  # Check constraint
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",  # Foreign key - supports composite keys
    "pk": "pk_%(table_name)s",  # Primary key
}

table_schema = settings.DB.SCHEMA
# Note: column_0_N_name expands to include all columns in multi-column constraints
# e.g., "workspace_id_tenant_id" for a composite constraint on both columns
meta = MetaData(naming_convention=convention)
meta.schema = table_schema
Base = declarative_base(metadata=meta)
