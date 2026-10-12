"""TLS client-certificate connections to the database proxy."""

from __future__ import annotations

import hashlib
import logging
import socket
import threading
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import DisconnectionError
from sqlalchemy.ext.asyncio import AsyncEngine

from .runtime import SenticaInstance, load_instance

logger = logging.getLogger(__name__)


def production_connect_args() -> dict[str, str]:
    """TLS and TCP arguments for a production connection, or nothing in test mode."""
    instance = load_instance()
    if instance is None:
        return {}
    return {
        "sslmode": "verify-full",
        "sslrootcert": str(instance.ca_path),
        "sslcert": str(instance.cert_path),
        "sslkey": str(instance.key_path),
        # Bound a blackholed proxy. tcp_user_timeout is milliseconds.
        "keepalives": "1",
        "keepalives_idle": "30",
        "keepalives_interval": "10",
        "keepalives_count": "3",
        "tcp_user_timeout": "5000",
    }


def certificate_fingerprint(cert_path: Any, key_path: Any) -> str:
    """Hash the client certificate and key together.

    Replace both files by writing a temporary name in the same directory and
    renaming it onto the destination. Readers then never see a partial pair.
    A change to either file changes this fingerprint.
    """
    digest = hashlib.sha256()
    for path in (cert_path, key_path):
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DisconnectionError("client certificate is unreadable") from exc
        if not data:
            raise DisconnectionError("client certificate is empty")
        digest.update(path.name.encode())
        digest.update(data)
    return digest.hexdigest()


def apply_production_engine_args(
    connect_args: dict[str, Any], _engine_kwargs: dict[str, Any]
) -> SenticaInstance | None:
    """Point a new engine at the proxy when production mode is on.

    Called while the engine is being built. Unset mode leaves the upstream
    engine alone so the test suite can keep its local database.

    Args:
        connect_args: psycopg connect arguments, mutated in place.
        engine_kwargs: SQLAlchemy engine keyword arguments, mutated in place.

    Returns:
        The instance when production mode is configured.
    """
    instance = load_instance()
    if instance is None:
        return None

    connect_args.update(production_connect_args())

    logger.info("Database connections use the proxy with verify-full")
    return instance


_live_fds: set[int] = set()
_fd_lock = threading.Lock()


def abort_checked_out_connections() -> None:
    """Shut every database socket this process still has checked out."""
    with _fd_lock:
        fds = list(_live_fds)
    for fd in fds:
        try:
            raw = socket.socket(fileno=fd)
        except OSError:
            continue
        try:
            raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            continue
        finally:
            raw.detach()


def install_call_timeout(sync_engine: Any, seconds: float = 6) -> None:
    """Close a connection that stays checked out too long.

    A paused proxy keeps acknowledging TCP, so a client read can wait
    forever. A thread outside the event loop shuts the socket, the driver
    raises, and the request becomes "memory unavailable".
    """

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

    def _checkout(dbapi_connection: Any, connection_record: Any, _proxy: Any) -> None:
        fileno = _fileno(dbapi_connection)
        if fileno is not None:
            with _fd_lock:
                _live_fds.add(fileno)

        def _cut(fd: int | None = fileno) -> None:
            if fd is None:
                return
            try:
                raw = socket.socket(fileno=fd)
            except OSError:
                return
            try:
                raw.shutdown(socket.SHUT_RDWR)
            except OSError:
                return
            finally:
                raw.detach()

        timer = threading.Timer(seconds, _cut)
        timer.daemon = True
        timer.start()
        connection_record.info["sentica_call_timer"] = timer

    def _checkin(dbapi_connection: Any, connection_record: Any) -> None:
        fileno = _fileno(dbapi_connection)
        if fileno is not None:
            with _fd_lock:
                _live_fds.discard(fileno)
        timer = connection_record.info.pop("sentica_call_timer", None)
        if isinstance(timer, threading.Timer):
            timer.cancel()

    event.listen(sync_engine.pool, "checkout", _checkout)
    event.listen(sync_engine.pool, "checkin", _checkin)


def install_certificate_reload(engine: AsyncEngine, instance: SenticaInstance) -> None:
    """Drop pooled connections when the client certificate file changes.

    New connections read the certificate path again. Connections that were
    opened with the previous file are invalid on checkout. ``pool_recycle``
    still retires anything older than one hour.
    """
    sync_engine = engine.sync_engine

    def _fingerprint() -> str:
        return certificate_fingerprint(instance.cert_path, instance.key_path)

    def _mark(_dbapi_connection: Any, connection_record: Any) -> None:
        connection_record.info["sentica_cert"] = _fingerprint()

    def _retire_stale(
        _dbapi_connection: Any, connection_record: Any, _proxy: Any
    ) -> None:
        current = _fingerprint()
        if connection_record.info.get("sentica_cert") != current:
            raise DisconnectionError("client certificate changed")

    event.listen(sync_engine, "connect", _mark)
    event.listen(sync_engine.pool, "checkout", _retire_stale)
