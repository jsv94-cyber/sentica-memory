"""Unit locks for the second M1 review: stamps, logs, recycle, rotation."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import jwt
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


def test_log_filter_hides_names_and_detail() -> None:
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "permission denied for workspace %s\nDETAIL: key %s",
        ("acme-name", "acme-name"),
        None,
    )
    assert ProductionLogFilter().filter(record)
    message = record.getMessage()
    assert "acme-name" not in message
    assert "DETAIL:" not in message
    access = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        "POST /v3/workspaces/%s/sessions/%s",
        ("acme-name", "sess-name"),
        None,
    )
    assert ProductionLogFilter().filter(access) is False


def test_maintenance_timeout_defaults_to_120(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.db import maintenance_db_call_timeout

    monkeypatch.delenv("SENTICA_DB_CALL_TIMEOUT_SECONDS", raising=False)
    assert maintenance_db_call_timeout() == 120
    monkeypatch.setenv("SENTICA_DB_CALL_TIMEOUT_SECONDS", "45")
    assert maintenance_db_call_timeout() == 45


def test_db_call_timeout_refuses_unparsable_and_over_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.db import maintenance_db_call_timeout

    monkeypatch.setenv("SENTICA_DB_CALL_TIMEOUT_SECONDS", "nope")
    with pytest.raises(SenticaStartupError, match="not a number"):
        maintenance_db_call_timeout()
    monkeypatch.setenv("SENTICA_DB_CALL_TIMEOUT_SECONDS", "601")
    with pytest.raises(SenticaStartupError, match="600"):
        maintenance_db_call_timeout()
    monkeypatch.setenv("SENTICA_MODE", "test")
    monkeypatch.setenv("SENTICA_DB_CALL_TIMEOUT_SECONDS", "nope")
    with pytest.raises(SenticaStartupError, match="not a number"):
        load_instance()
    assert not Path("docker/sentica/userlist-a.txt").exists()
    assert not Path("docker/sentica/userlist-b.txt").exists()
    instance_doc = Path("docs/INSTANCE.md").read_text(encoding="utf-8")
    assert "bind-mounted as a" in instance_doc
    assert "not as individual files" in instance_doc


def test_m2_summary_defers_proxy_tls_redis_and_internal_networks() -> None:
    text = Path(".review/M2-summary.md").read_text(encoding="utf-8")
    heading = text.index("## Deferred to M9")
    deferred = text[heading:]
    assert "server_tls_sslmode" in deferred
    assert "verify-full" in deferred
    assert "Redis" in deferred
    assert "non-root" in deferred or "root" in deferred
    assert "internal: true" in deferred
    assert "db" in deferred
    assert "net-a" in deferred
    assert "net-b" in deferred


def test_production_connect_args_include_connect_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.sentica.db_connect import production_connect_args

    _production(monkeypatch, tmp_path)
    args = production_connect_args()
    assert args["connect_timeout"]
    assert args["sslmode"] == "verify-full"


def test_install_certificate_reload_drops_the_old_connection(tmp_path: Path) -> None:
    import asyncio
    from types import SimpleNamespace
    from typing import cast

    from sqlalchemy.ext.asyncio import create_async_engine

    from src.sentica.db_connect import install_certificate_reload
    from src.sentica.runtime import SenticaInstance

    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_bytes(b"cert-v1")
    key.write_bytes(b"key-v1")
    engine = create_async_engine(
        os.environ["DB_CONNECTION_URI"], pool_size=1, max_overflow=0
    )
    instance = cast(
        SenticaInstance, cast(object, SimpleNamespace(cert_path=cert, key_path=key))
    )
    install_certificate_reload(engine, instance)

    async def _pid() -> int:
        async with engine.connect() as conn:
            value = await conn.scalar(text("select pg_backend_pid()"))
        assert isinstance(value, int)
        return value

    async def _run() -> None:
        first = await _pid()
        cert.write_bytes(b"cert-v2")
        key.write_bytes(b"key-v2")
        second = await _pid()
        assert first != second
        await engine.dispose()

    asyncio.run(_run())


def test_slow_llm_chat_streams_fully(
    client: object,
    sample_data: tuple[models.Workspace, models.Peer],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    from starlette.testclient import TestClient

    from src.db import BoundedSession, ReadSessionLocal, SessionLocal
    from src.sentica.runtime import TOKEN_AUDIENCE

    assert isinstance(client, TestClient)
    workspace, peer = sample_data
    private = ed25519.Ed25519PrivateKey.generate()
    public = tmp_path / "token.pub"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    for path in ("client.crt", "client.key", "ca.crt"):
        (tmp_path / path).write_text("present\n", encoding="utf-8")
    monkeypatch.setenv("SENTICA_MODE", "production")
    monkeypatch.setenv("SENTICA_TENANT_ID", "tenant-a")
    monkeypatch.setenv("SENTICA_DB_PROXY_HOST", "pgbouncer")
    monkeypatch.setenv("SENTICA_DB_PROXY_PORT", "6432")
    monkeypatch.setenv("SENTICA_DB_USER", "tenant_a")
    monkeypatch.setenv("SENTICA_DB_NAME", "tenant_a")
    monkeypatch.setenv("SENTICA_CLIENT_CERT_PATH", str(tmp_path / "client.crt"))
    monkeypatch.setenv("SENTICA_CLIENT_KEY_PATH", str(tmp_path / "client.key"))
    monkeypatch.setenv("SENTICA_PROXY_CA_PATH", str(tmp_path / "ca.crt"))
    monkeypatch.setenv("SENTICA_TOKEN_PUBLIC_KEY_PATH", str(public))
    monkeypatch.setenv("SENTICA_TOKEN_ISSUER", "sentica-broker")
    monkeypatch.setenv("SENTICA_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "DB_CONNECTION_URI",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "DB_PASSWORD",
        "PGPASSWORD",
        "PGPASSFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    issued = int(time.time())
    token = jwt.encode(
        {
            "iss": "sentica-broker",
            "aud": TOKEN_AUDIENCE,
            "iat": issued,
            "exp": issued + 60,
            "tenant_id": "tenant-a",
            "request_id": "slow-llm",
            "w": workspace.name,
        },
        private,
        algorithm="EdDSA",
    )
    write_class = SessionLocal.class_
    read_class = ReadSessionLocal.class_
    SessionLocal.configure(class_=BoundedSession)
    ReadSessionLocal.configure(class_=BoundedSession)

    async def slow_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        await asyncio.sleep(15)
        yield "slow-llm-token"
        yield " tail"

    monkeypatch.setattr("src.routers.peers.agentic_chat_stream", slow_stream)
    started = time.monotonic()
    try:
        with client.stream(
            "POST",
            f"/v3/workspaces/{workspace.name}/peers/{peer.name}/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "hello", "stream": True},
        ) as response:
            assert response.status_code == 200, response.read()
            body = response.read().decode()
    finally:
        SessionLocal.configure(class_=write_class)
        ReadSessionLocal.configure(class_=read_class)
    assert time.monotonic() - started >= 14
    assert "slow-llm-token" in body
    assert " tail" in body
