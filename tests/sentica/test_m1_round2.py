"""Unit locks for the second M1 review: stamps, logs, recycle, rotation."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import TimeoutError as SqlTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from src import models
from src.main import _memory_unavailable  # pyright: ignore[reportPrivateUsage]
from src.sentica.db_connect import certificate_fingerprint
from src.sentica.log_policy import ProductionLogFilter
from src.sentica.runtime import (
    SenticaStartupError,
    capped_pool_recycle,
    load_instance,
)


def _production(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    private = ed25519.Ed25519PrivateKey.generate()
    public = tmp_path / "token.pub"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    for name in ("client.crt", "client.key", "ca.crt"):
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    env = {
        "SENTICA_MODE": "production",
        "SENTICA_TENANT_ID": "tenant-a",
        "SENTICA_DB_PROXY_HOST": "pgbouncer",
        "SENTICA_DB_PROXY_PORT": "6432",
        "SENTICA_DB_USER": "tenant_a",
        "SENTICA_DB_NAME": "tenant_a",
        "SENTICA_CLIENT_CERT_PATH": str(tmp_path / "client.crt"),
        "SENTICA_CLIENT_KEY_PATH": str(tmp_path / "client.key"),
        "SENTICA_PROXY_CA_PATH": str(tmp_path / "ca.crt"),
        "SENTICA_TOKEN_PUBLIC_KEY_PATH": str(public),
        "SENTICA_TOKEN_ISSUER": "sentica-broker",
        "SENTICA_BIND_HOST": "127.0.0.1",
        "HOME": str(tmp_path),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in (
        "DB_CONNECTION_URI",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "DB_PASSWORD",
        "PGPASSWORD",
        "PGPASSFILE",
        "DB_SQL_DEBUG",
        "COLLECT_METRICS_LOCAL",
        "REASONING_TRACES_FILE",
        "METRICS_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)


def test_log_filter_hides_parameters_and_work_unit_keys() -> None:
    canary = "canary-parameter-value"
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "failed %s",
        (canary,),
        None,
    )
    record.exc_info = (RuntimeError, RuntimeError(f"[parameters: {canary}]"), None)
    assert ProductionLogFilter().filter(record)
    assert canary not in record.getMessage()

    work = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "work unit %s payload %s",
        ("representation:workspace:session:peer", {"content": canary}),
        None,
    )
    assert ProductionLogFilter().filter(work)
    assert canary not in work.getMessage()
    assert "workspace" not in work.getMessage()


def test_timeout_is_memory_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTICA_MODE", "production")
    assert _memory_unavailable(SqlTimeoutError())
    monkeypatch.setenv("SENTICA_MODE", "test")
    assert _memory_unavailable(SqlTimeoutError()) is False


def test_pool_recycle_cap() -> None:
    assert capped_pool_recycle(7200) == 3600
    assert capped_pool_recycle(2) == 2


def test_short_pool_recycle_opens_a_new_connection() -> None:
    uri = os.environ["DB_CONNECTION_URI"]
    engine = create_engine(uri, pool_recycle=1, pool_size=1, max_overflow=0)
    try:
        with engine.connect() as conn:
            first = conn.scalar(text("select pg_backend_pid()"))
        time.sleep(1.2)
        with engine.connect() as conn:
            second = conn.scalar(text("select pg_backend_pid()"))
        assert first != second
    finally:
        engine.dispose()


def test_rename_into_place_changes_the_fingerprint(tmp_path: Path) -> None:
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_bytes(b"cert-old")
    key.write_bytes(b"key-old")
    original = certificate_fingerprint(cert, key)
    staged = tmp_path / "client.crt.next"
    staged.write_bytes(b"cert-new")
    assert certificate_fingerprint(cert, key) == original
    os_replace = staged.replace
    os_replace(cert)
    after_cert = certificate_fingerprint(cert, key)
    assert after_cert != original
    staged_key = tmp_path / "client.key.next"
    staged_key.write_bytes(b"key-new")
    assert certificate_fingerprint(cert, key) == after_cert
    staged_key.replace(key)
    assert certificate_fingerprint(cert, key) != after_cert


@pytest.mark.asyncio
async def test_orm_and_core_inserts_stamp_tenant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_session: AsyncSession,
) -> None:
    from sqlalchemy import insert

    _production(monkeypatch, tmp_path)
    assert load_instance() is not None
    item = models.QueueItem(
        work_unit_key="reconciler:cleanup_queue-round2",
        task_type="reconciler",
        payload={"reconciler_type": "cleanup_queue"},
        processed=False,
    )
    db_session.add(item)
    await db_session.flush()
    assert item.payload["tenant_id"] == "tenant-a"

    await db_session.execute(
        insert(models.QueueItem),
        [
            {
                "work_unit_key": "webhook:ws-round2",
                "task_type": "webhook",
                "payload": {"event_type": "test.event", "data": {}},
                "processed": False,
            }
        ],
    )
    stored = await db_session.scalar(
        select(models.QueueItem).where(
            models.QueueItem.work_unit_key == "webhook:ws-round2"
        )
    )
    assert stored is not None
    assert stored.payload["tenant_id"] == "tenant-a"


def test_production_without_password_files_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _production(monkeypatch, tmp_path)
    instance = load_instance()
    assert instance is not None
    again = load_instance()
    assert again is instance
    with pytest.raises(SenticaStartupError):
        monkeypatch.setenv("SENTICA_MODE", "nope")
        load_instance()
