"""TLS client-certificate connections to the database proxy."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import DisconnectionError
from sqlalchemy.ext.asyncio import AsyncEngine

from .runtime import SenticaInstance, load_instance

logger = logging.getLogger(__name__)


def production_connect_args() -> dict[str, str]:
    """TLS arguments for a production connection, or nothing in upstream mode."""
    instance = load_instance()
    if instance is None:
        return {}
    return {
        "sslmode": "verify-full",
        "sslrootcert": str(instance.ca_path),
        "sslcert": str(instance.cert_path),
        "sslkey": str(instance.key_path),
    }


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


def install_certificate_reload(engine: AsyncEngine, instance: SenticaInstance) -> None:
    """Drop pooled connections when the client certificate file changes.

    New connections read the certificate path again. Connections that were
    opened with the previous file are invalid on checkout. ``pool_recycle``
    still retires anything older than one hour.
    """
    sync_engine = engine.sync_engine

    def _fingerprint() -> str:
        try:
            data = instance.cert_path.read_bytes()
        except OSError as exc:
            raise DisconnectionError("client certificate is unreadable") from exc
        if not data:
            raise DisconnectionError("client certificate is empty")
        return hashlib.sha256(data).hexdigest()

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
