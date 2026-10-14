"""Runtime-role proof. The CI job migrates as tenant_rls_ddl and connects as tenant_rls."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from src.sentica.tokens import verify_sentica_token
from tests.sentica.rls_job import ddl_dsn, defined_m2_tests, rls_job, runtime_dsn

ROOT = Path(__file__).resolve().parents[2]


def test_signed_token_names_its_workspaces(tmp_path: object) -> None:
    import time
    from pathlib import Path

    import jwt

    from src.sentica.runtime import TOKEN_AUDIENCE, SenticaInstance

    root = tmp_path if isinstance(tmp_path, Path) else Path(str(tmp_path))
    private = ed25519.Ed25519PrivateKey.generate()
    public_path = root / "token.pub"
    public_bytes = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_path.write_bytes(public_bytes)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "sentica-rls-issuer",
            "aud": TOKEN_AUDIENCE,
            "iat": now,
            "exp": now + 60,
            "request_id": "rls-proof",
            "tenant_id": "tenant-rls",
            "w": "left",
            "workspaces": ["left", "right"],
        },
        private,
        algorithm="EdDSA",
    )
    instance = SenticaInstance(
        tenant_id="tenant-rls",
        proxy_host="localhost",
        proxy_port=5432,
        db_user="tenant_rls",
        db_name="tenant_rls",
        cert_path=public_path,
        key_path=public_path,
        ca_path=public_path,
        token_public_key_path=public_path,
        token_public_key=private.public_key(),
        token_issuer="sentica-rls-issuer",
        bind_host="127.0.0.1",
    )
    params = verify_sentica_token(token, instance)
    assert params.workspaces == ["left", "right"]


def test_rls_job_collects_every_m2_file(request: pytest.FixtureRequest) -> None:
    """The job's collected count covers every test function in test_m2_*.py."""
    if rls_job():
        collected = [
            item.nodeid
            for item in request.session.items
            if "tests/sentica/test_m2_" in item.nodeid
        ]
        assert len(collected) >= defined_m2_tests()
        return
    workflow = (ROOT / ".github" / "workflows" / "unittest.yml").read_text(
        encoding="utf-8"
    )
    proof = workflow.split("Run the runtime-role proof", 1)[1]
    for name in (
        "test_m2_rls.py",
        "test_m2_fix.py",
        "test_m2_partitions.py",
        "test_m2_carryover.py",
    ):
        assert name in proof
    assert "test-m2-rls collected" in (ROOT / "tests" / "conftest.py").read_text(
        encoding="utf-8"
    )


async def test_app_engine_is_the_runtime_role() -> None:
    """current_user on the app engine is the runtime role, not a superuser."""
    from sqlalchemy import text

    from src.db import engine

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls "
                    + "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    assert row[0] == "tenant_rls"
    assert row[1] is False
    assert row[2] is False


def test_runtime_role_lockdown() -> None:
    dsn = runtime_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn:
        who = conn.execute(
            "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert who is not None
        assert who[0] == "tenant_rls"
        assert who[1] is False
        assert who[2] is False
        member = conn.execute(
            "SELECT pg_has_role(current_user, 'tenant_rls_ddl', 'USAGE')"
        ).fetchone()
        assert member == (False,)
        for statement in (
            "ALTER TABLE memory.messages NO FORCE ROW LEVEL SECURITY",
            "DROP POLICY sentica_workspace ON memory.messages",
            "CREATE TABLE memory.should_fail (id int)",
        ):
            with pytest.raises(psycopg.Error) as denied:
                conn.execute(statement)
            assert denied.value.sqlstate == "42501"
        version = conn.execute(
            "SELECT version_num FROM memory.alembic_version"
        ).fetchone()
        assert version is not None
        description = conn.execute(
            "SELECT * FROM memory.sentica_pending_counts() LIMIT 0"
        )
        columns = description.description
        assert columns is not None
        assert [col.name for col in columns] == [
            "workspace_name",
            "pending_queue",
            "pending_embeddings",
        ]
        left = "a" + uuid.uuid4().hex[:20]
        right = "b" + uuid.uuid4().hex[:20]
        for name in (left, right):
            conn.execute(
                "INSERT INTO memory.workspaces (id, name) VALUES (%s, %s)",
                (name, name),
            )
        conn.execute("BEGIN")
        conn.execute("SELECT set_config('sentica.workspace', %s, true)", (left,))
        conn.execute(
            "INSERT INTO memory.peers (id, name, workspace_name) VALUES (%s, %s, %s)",
            ("p" + uuid.uuid4().hex[:20], "peer", left),
        )
        conn.execute("COMMIT")
        conn.execute("BEGIN")
        conn.execute("SELECT set_config('sentica.workspace', %s, true)", (left,))
        with pytest.raises(psycopg.Error) as cross:
            conn.execute(
                "INSERT INTO memory.peers (id, name, workspace_name) "
                + "VALUES (%s, %s, %s)",
                ("q" + uuid.uuid4().hex[:20], "peer", right),
            )
        assert cross.value.sqlstate == "42501"
        conn.execute("ROLLBACK")
        conn.execute("BEGIN")
        conn.execute("SELECT set_config('sentica.workspace', '', true)")
        empty = conn.execute("SELECT count(*) FROM memory.peers").fetchone()
        conn.execute("COMMIT")
        assert empty is not None and empty[0] == 0
        conn.execute("BEGIN")
        conn.execute("SELECT set_config('sentica.workspace', %s, true)", (left,))
        visible = conn.execute("SELECT workspace_name FROM memory.peers").fetchall()
        conn.execute("COMMIT")
        assert visible == [(left,)]
        ddl = ddl_dsn()
        with psycopg.connect(ddl, autocommit=True) as owner:
            owner.execute("ALTER TABLE memory.peers DISABLE ROW LEVEL SECURITY")
        leaked = conn.execute(
            "SELECT workspace_name FROM memory.peers WHERE workspace_name = %s",
            (left,),
        ).fetchall()
        assert leaked == [(left,)]
        with psycopg.connect(ddl, autocommit=True) as owner:
            owner.execute("ALTER TABLE memory.peers ENABLE ROW LEVEL SECURITY")
            owner.execute("ALTER TABLE memory.peers FORCE ROW LEVEL SECURITY")
