"""Two-instance proof for the local certificate proxy.

The stack is Docker Compose on this machine: a local test CA, two
PgBouncers, two APIs, two derivers, two Redis processes, and one Postgres
whose data directory is tmpfs. CI runs this file in its own job.
"""

# The embedded scripts are adjacent string literals on purpose.
# pyright: reportImplicitStringConcatenation=false

from __future__ import annotations

import inspect
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import jwt
import psycopg
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.x509.oid import NameOID

from tests.sentica.scram import scram_verifier

pytestmark = pytest.mark.xdist_group(name="sentica_m1_stack")

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.sentica.yml"
PROJECT = "sentica-m1"
cert_dir_holder: list[str] = []
secret_dir_holder: list[str] = []
API_A = "http://127.0.0.1:8101"
API_B = "http://127.0.0.1:8102"
PROXY_A = 6433
PROXY_B = 6434


def _run(
    args: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        args,
        cwd=ROOT,
        env=merged,
        text=True,
        input=stdin,
        timeout=timeout,
        capture_output=True,
        check=check,
    )


def _compose(
    args: list[str],
    *,
    check: bool = True,
    stdin: str | None = None,
    timeout: float | None = None,
    extra_files: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if not cert_dir_holder:
        raise RuntimeError("CERT_DIR is not set")
    file_args = ["-f", str(COMPOSE_FILE)]
    extras = list(extra_files or [])
    if os.environ.get("SENTICA_GVISOR") == "1":
        extras.append(str(ROOT / "docker-compose.gvisor.yml"))
    for extra in extras:
        file_args.extend(["-f", extra])
    return _run(
        ["docker", "compose", "-p", PROJECT, *file_args, *args],
        check=check,
        env={
            "CERT_DIR": cert_dir_holder[0],
            "SECRET_DIR": secret_dir_holder[0] if secret_dir_holder else "/tmp",
            "SENTICA_UID": str(os.getuid()),
            "SENTICA_GID": str(os.getgid()),
        },
        stdin=stdin,
        timeout=timeout,
    )


def _token(
    private_pem: bytes,
    *,
    issuer: str,
    tenant: str,
    lifetime: timedelta = timedelta(minutes=4),
    expired: bool = False,
    admin: bool = False,
    request_id: str = "req-m1",
    workspace: str = "m1-workspace",
) -> str:
    private = serialization.load_pem_private_key(private_pem, password=None)
    assert isinstance(private, ed25519.Ed25519PrivateKey)
    now = datetime.now(UTC)
    if expired:
        iat = now - timedelta(minutes=10)
        exp = now - timedelta(minutes=6)
    else:
        iat = now
        exp = now + lifetime
    payload: dict[str, object] = {
        "iss": issuer,
        "iat": iat,
        "exp": exp,
        "request_id": request_id,
        "tenant_id": tenant,
        "aud": "sentica-memory",
        "w": workspace,
    }
    if admin:
        payload["ad"] = True
    encoded = jwt.encode(payload, private, algorithm="EdDSA")
    assert isinstance(encoded, str)
    return encoded


def _assert_sqlstate(cert_dir: Path, sql: str, sqlstate: str) -> None:
    with (
        _connect(PROXY_A, cert_dir, "tenant_a", "tenant_a", "tenant_a") as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.Error) as exc,
    ):
        cur.execute(sql)  # pyright: ignore[reportCallIssue, reportArgumentType]
    assert exc.value.sqlstate == sqlstate


def _bind_workspace(cur: Any, workspace: str) -> None:
    """Let a raw tenant connection see one workspace under row-level security."""
    cur.execute(
        "SELECT set_config('sentica.workspace', %s, true)",
        (workspace,),
    )


def _ensure_workspace(cur: Any, workspace: str) -> None:
    cur.execute(
        """
        INSERT INTO memory.workspaces (id, name)
        VALUES (%s, %s)
        ON CONFLICT (name) DO NOTHING
        """,
        (uuid.uuid4().hex[:21], workspace),
    )


def _connect(
    port: int, cert_dir: Path, user: str, dbname: str, cert_name: str
) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host="127.0.0.1",
        port=port,
        user=user,
        dbname=dbname,
        sslmode="verify-full",
        sslrootcert=str(cert_dir / "ca.crt"),
        sslcert=str(cert_dir / f"{cert_name}.crt"),
        sslkey=str(cert_dir / f"{cert_name}.key"),
        connect_timeout=5,
    )


def _rename_into_place(path: Path, data: bytes, mode: int) -> None:
    """Replace a per-file bind mount by writing the existing inode.

    A rename would leave the container holding the old file. The unit test
    of rename-into-place covers the directory-mount case.
    """
    if path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
        os.chmod(path, mode)
        return
    path.write_bytes(data)
    os.chmod(path, mode)


def _reissue_client(
    cert_dir: Path,
    common_name: str,
    *,
    ca_key: rsa.RSAPrivateKey | None = None,
    ca_cert: x509.Certificate | None = None,
) -> None:
    if ca_key is None or ca_cert is None:
        loaded = serialization.load_pem_private_key(
            (cert_dir.parent / "private" / "ca.key").read_bytes(), password=None
        )
        assert isinstance(loaded, rsa.RSAPrivateKey)
        ca_key = loaded
        ca_cert = x509.load_pem_x509_certificate((cert_dir / "ca.crt").read_bytes())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=48))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, SHA256())
    )
    key_path = cert_dir / f"{common_name}.key"
    cert_path = cert_dir / f"{common_name}.crt"
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    _rename_into_place(key_path, key_bytes, 0o600)
    _rename_into_place(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)


@pytest.fixture(scope="module")
def stack() -> Iterator[Path]:
    cert_dir = Path("/dev/shm") / f"sentica-m1-{uuid.uuid4().hex}"
    cert_dir.mkdir(parents=True, exist_ok=False)
    cert_dir_holder.clear()
    cert_dir_holder.append(str(cert_dir))
    secret_dir = cert_dir / "secrets"
    secret_dir.mkdir()
    secret_dir_holder.clear()
    secret_dir_holder.append(str(secret_dir))
    for name in (
        "tenant-a",
        "tenant-b",
        "tenant-a-ddl",
        "tenant-b-ddl",
        "userlist-a.txt",
        "userlist-b.txt",
    ):
        (secret_dir / name).write_text("placeholder\n", encoding="utf-8")
    try:
        subprocess.run(
            [
                "uv",
                "run",
                "python",
                "scripts/sentica_test_ca.py",
                "--out",
                str(cert_dir),
            ],
            cwd=ROOT,
            check=True,
        )
        private = cert_dir / "private"
        public = cert_dir / "public"
        private.mkdir()
        public.mkdir()
        for name in ("ca.key", "tenant-a.token.key", "tenant-b.token.key"):
            (cert_dir / name).rename(private / name)
        for item in list(cert_dir.iterdir()):
            if item.name in {"private", "public", "secrets"}:
                continue
            item.rename(public / item.name)
        cert_dir_holder.clear()
        cert_dir_holder.append(str(public))
        password_a = secrets.token_hex(24)
        password_b = secrets.token_hex(24)
        password_a_ddl = secrets.token_hex(24)
        password_b_ddl = secrets.token_hex(24)
        (secret_dir / "tenant-a").write_text(
            scram_verifier(password_a), encoding="utf-8"
        )
        (secret_dir / "tenant-b").write_text(
            scram_verifier(password_b), encoding="utf-8"
        )
        (secret_dir / "tenant-a-ddl").write_text(
            scram_verifier(password_a_ddl), encoding="utf-8"
        )
        (secret_dir / "tenant-b-ddl").write_text(
            scram_verifier(password_b_ddl), encoding="utf-8"
        )
        (secret_dir / "userlist-a.txt").write_text(
            f'"tenant_a" "{password_a}"\n'
            f'"tenant_a_ddl" "{password_a_ddl}"\n'
            '"sentica_operator" ""\n',
            encoding="utf-8",
        )
        (secret_dir / "userlist-b.txt").write_text(
            f'"tenant_b" "{password_b}"\n'
            f'"tenant_b_ddl" "{password_b_ddl}"\n'
            '"sentica_operator" ""\n',
            encoding="utf-8",
        )
        for path in secret_dir.iterdir():
            os.chmod(path, 0o600)
        for name in (
            "tenant_a.key",
            "tenant_b.key",
            "tenant_a_ddl.key",
            "tenant_b_ddl.key",
            "sentica_operator.key",
            "pgbouncer-a.key",
            "pgbouncer-b.key",
        ):
            os.chmod(public / name, 0o600)
        started = _compose(["up", "-d", "--build"], check=False)
        if started.returncode != 0:
            raise AssertionError(started.stderr[-4000:] + started.stdout[-2000:])
        deadline = time.monotonic() + 300
        last = ""
        while time.monotonic() < deadline:
            probe = _compose(["ps", "--format", "json"], check=False)
            last = probe.stdout + probe.stderr
            if '"healthy"' in last and last.count('"healthy"') >= 4:
                try:
                    httpx.get(f"{API_A}/health", timeout=2).raise_for_status()
                    httpx.get(f"{API_B}/health", timeout=2).raise_for_status()
                    break
                except httpx.HTTPError as exc:
                    last = str(exc)
            time.sleep(3)
        else:
            state = _compose(["ps"], check=False)
            raise AssertionError(state.stdout + state.stderr + last)
        yield public
    finally:
        _compose(["down", "--volumes", "--remove-orphans"], check=False)
        shutil.rmtree(cert_dir, ignore_errors=True)
        # cert_dir is the parent of the mounted public directory.
        volumes = _run(
            ["docker", "volume", "ls", "-q", "--filter", f"name={PROJECT}"],
            check=False,
        )
        if volumes.stdout.strip() or cert_dir.exists():
            raise AssertionError("memory content or certificates remained after stop")


def _token_key(stack: Path, tenant: str) -> bytes:
    return (stack.parent / "private" / f"tenant-{tenant}.token.key").read_bytes()


def test_missing_tenant_refuses_to_start(stack: Path) -> None:
    del stack
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "SENTICA_MODE=production",
            "--entrypoint",
            "/app/.venv/bin/python",
            f"{PROJECT}-api-a",
            "-c",
            "import src.db",
        ],
        check=False,
    )
    assert result.returncode != 0
    assert "SENTICA_TENANT_ID" in result.stderr + result.stdout


def test_tokens_and_isolation(stack: Path) -> None:
    key_a = _token_key(stack, "a")
    key_b = _token_key(stack, "b")
    good_a = _token(key_a, issuer="sentica-test-issuer-a", tenant="tenant-a")
    good_b = _token(key_b, issuer="sentica-test-issuer-b", tenant="tenant-b")
    headers_a = {"Authorization": f"Bearer {good_a}"}

    created = httpx.post(
        f"{API_A}/v3/workspaces",
        headers=headers_a,
        json={"name": "m1-workspace"},
        timeout=20,
    )
    assert created.status_code in (200, 201), created.text

    spec = httpx.get(f"{API_A}/openapi.json", headers=headers_a, timeout=20)
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert paths
    wrong_tenant = {
        "Authorization": "Bearer "
        + _token(key_a, issuer="sentica-test-issuer-a", tenant="tenant-b")
    }
    hidden = "/v3/workspaces/m1-workspace/sessions/m1-session/messages/"
    targets = [(path, method) for path, methods in paths.items() for method in methods]
    targets.append((hidden, "post"))
    targets.append(("/deriver/metrics", "get"))
    for path, method in targets:
        if method not in {"get", "post", "put", "patch", "delete"}:
            continue
        if path in {"/health", "/ready", "/metrics"}:
            continue
        url = API_A + path.replace("{", "x").replace("}", "")
        response = httpx.request(
            method.upper(),
            url,
            headers=wrong_tenant,
            timeout=20,
        )
        assert response.status_code == 401, (
            method,
            path,
            response.status_code,
            response.text,
        )
        assert response.json()["detail"] == "wrong tenant"

    metrics = httpx.get(
        f"{API_A}/deriver/metrics",
        headers={"Authorization": f"Bearer {good_b}"},
        timeout=20,
    )
    assert metrics.status_code == 401

    refused: dict[str, tuple[dict[str, str], str]] = {
        "wrong_tenant": (
            {
                "Authorization": "Bearer "
                + _token(key_a, issuer="sentica-test-issuer-a", tenant="tenant-b")
            },
            "wrong tenant",
        ),
        "missing": (dict[str, str](), "missing token"),
        "forged": ({"Authorization": "Bearer not-a-jwt"}, "forged token"),
        "expired": (
            {
                "Authorization": "Bearer "
                + _token(
                    key_a,
                    issuer="sentica-test-issuer-a",
                    tenant="tenant-a",
                    expired=True,
                )
            },
            "expired token",
        ),
        "issuer": (
            {
                "Authorization": "Bearer "
                + _token(key_a, issuer="other-issuer", tenant="tenant-a")
            },
            "wrong issuer",
        ),
        "admin": (
            {
                "Authorization": "Bearer "
                + _token(
                    key_a, issuer="sentica-test-issuer-a", tenant="tenant-a", admin=True
                )
            },
            "admin tokens are disabled",
        ),
        "lifetime": (
            {
                "Authorization": "Bearer "
                + _token(
                    key_a,
                    issuer="sentica-test-issuer-a",
                    tenant="tenant-a",
                    lifetime=timedelta(minutes=6),
                )
            },
            "token lifetime exceeds 5 minutes",
        ),
    }
    for name, (headers, detail) in refused.items():
        response = httpx.post(
            f"{API_A}/v3/workspaces",
            headers=headers,
            json={"name": "m1-workspace"},
            timeout=20,
        )
        assert response.status_code == 401, (name, response.status_code, response.text)
        assert response.json()["detail"] == detail

    listed = httpx.post(
        f"{API_A}/v3/workspaces/list", headers=headers_a, json={}, timeout=20
    )
    assert listed.status_code == 401, listed.text
    assert listed.json()["detail"] == "Resource requires admin privileges"

    env_dump = _compose(["exec", "-T", "api-a", "printenv"]).stdout
    for forbidden in (
        "DB_CONNECTION_URI",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "DB_PASSWORD",
        "PGPASSWORD",
    ):
        assert f"{forbidden}=" not in env_dump

    password = _compose(
        ["exec", "-T", "redis-a", "cat", "/secret/password"]
    ).stdout.strip()
    redis = _compose(
        [
            "exec",
            "-T",
            "redis-a",
            "redis-cli",
            "-a",
            password,
            "--no-auth-warning",
            "CONFIG",
            "GET",
            "appendonly",
        ]
    )
    assert "no" in redis.stdout

    _assert_sqlstate(stack, "SELECT id FROM public.outside_memory", "42501")
    _assert_sqlstate(stack, "CREATE SCHEMA sentica_escape", "42501")
    _assert_sqlstate(stack, "CREATE TABLE public.t (id int)", "42501")
    _assert_sqlstate(stack, "DROP TABLE public.outside_memory", "42501")

    with pytest.raises(psycopg.OperationalError) as user_mismatch:
        _connect(PROXY_A, stack, "tenant_b", "tenant_a", "tenant_a")
    mismatch_text = str(user_mismatch.value).lower()
    assert "connection refused" not in mismatch_text
    assert any(
        word in mismatch_text for word in ("auth", "login", "certificate", "user")
    )

    with pytest.raises(psycopg.OperationalError) as other_db:
        _connect(PROXY_A, stack, "tenant_a", "tenant_b", "tenant_a")
    other_text = str(other_db.value).lower()
    assert "no such database" in other_text or "does not exist" in other_text

    with (
        _connect(PROXY_B, stack, "tenant_b", "tenant_b", "tenant_b") as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT 1")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


def test_certificate_swap_has_no_failed_requests(stack: Path) -> None:
    key_a = _token_key(stack, "a")
    token = _token(
        key_a, issuer="sentica-test-issuer-a", tenant="tenant-a", request_id="swap"
    )
    headers = {"Authorization": f"Bearer {token}"}
    created = httpx.post(
        f"{API_A}/v3/workspaces",
        headers=headers,
        json={"name": "m1-workspace"},
        timeout=20,
    )
    assert created.status_code in (200, 201), created.text
    old_cert = (stack / "tenant_a.crt").read_bytes()
    old_key = (stack / "tenant_a.key").read_bytes()
    failures: list[str] = []
    stop = threading.Event()
    overlap: bytes | None = None

    def _traffic() -> None:
        while not stop.is_set():
            try:
                response = httpx.post(
                    f"{API_A}/v3/workspaces",
                    headers=headers,
                    json={"name": "m1-workspace"},
                    timeout=10,
                )
            except httpx.HTTPError as exc:
                failures.append(str(exc))
                continue
            if response.status_code not in (200, 201):
                failures.append(f"{response.status_code}:{response.text[:160]}")

    worker = threading.Thread(target=_traffic)
    worker.start()
    time.sleep(0.4)
    try:
        new_ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(UTC)
        new_ca = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca2")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca2")]))
            .public_key(new_ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=48))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(new_ca_key, SHA256())
        )
        both = (stack / "ca.crt").read_bytes() + new_ca.public_bytes(
            serialization.Encoding.PEM
        )
        overlap = both
        _rename_into_place(stack / "client-ca.crt", both, 0o644)
        _compose(["kill", "-s", "HUP", "pgbouncer-a"])
        time.sleep(0.5)
        _reissue_client(stack, "tenant_a", ca_key=new_ca_key, ca_cert=new_ca)
        time.sleep(3.5)
        only_new = new_ca.public_bytes(serialization.Encoding.PEM)
        _rename_into_place(stack / "client-ca.crt", only_new, 0o644)
        _compose(["kill", "-s", "HUP", "pgbouncer-a"])
        time.sleep(1.0)
        for _ in range(8):
            response = httpx.post(
                f"{API_A}/v3/workspaces",
                headers=headers,
                json={"name": "m1-workspace"},
                timeout=10,
            )
            if response.status_code not in (200, 201):
                failures.append(f"after:{response.status_code}:{response.text[:160]}")
    finally:
        stop.set()
        worker.join(timeout=15)

    assert failures == [], failures[:3]
    old_dir = stack / "old-client"
    old_dir.mkdir()
    (old_dir / "ca.crt").write_bytes((stack / "ca.crt").read_bytes())
    (old_dir / "tenant_a.crt").write_bytes(old_cert)
    (old_dir / "tenant_a.key").write_bytes(old_key)
    os.chmod(old_dir / "tenant_a.key", 0o600)
    with pytest.raises(psycopg.OperationalError) as old_attempt:
        _connect(PROXY_A, old_dir, "tenant_a", "tenant_a", "tenant_a")
    assert "connection refused" not in str(old_attempt.value).lower()
    with (
        _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
    assert overlap is not None
    _rename_into_place(stack / "client-ca.crt", overlap, 0o644)
    _compose(["kill", "-s", "HUP", "pgbouncer-a"])
    time.sleep(0.5)


def test_revocation_rolls_back_and_reports_unavailable(stack: Path) -> None:
    key_a = _token_key(stack, "a")
    workspace = f"revoke-{uuid.uuid4().hex[:12]}"
    token = _token(
        key_a,
        issuer="sentica-test-issuer-a",
        tenant="tenant-a",
        request_id="revoked",
        workspace=workspace,
    )
    headers = {"Authorization": f"Bearer {token}"}
    created = httpx.post(
        f"{API_A}/v3/workspaces",
        headers=headers,
        json={"name": workspace},
        timeout=20,
    )
    assert created.status_code in (200, 201), created.text
    session = httpx.post(
        f"{API_A}/v3/workspaces/{workspace}/sessions",
        headers=headers,
        json={"id": "s1"},
        timeout=20,
    )
    assert session.status_code in (200, 201), session.text
    peer = httpx.post(
        f"{API_A}/v3/workspaces/{workspace}/peers",
        headers=headers,
        json={"name": "p1"},
        timeout=20,
    )
    assert peer.status_code in (200, 201), peer.text
    canary = f"canary-write-{uuid.uuid4().hex}"
    _compose(["pause", "pgbouncer-a"])
    started = time.monotonic()
    try:
        response = httpx.post(
            f"{API_A}/v3/workspaces/{workspace}/sessions/s1/messages",
            headers=headers,
            json={
                "messages": [
                    {"peer_id": "p1", "content": canary, "source_trust": "owner"},
                    {
                        "peer_id": "p1",
                        "content": canary + "-2",
                        "source_trust": "owner",
                    },
                ]
            },
            timeout=12,
        )
    finally:
        _compose(["unpause", "pgbouncer-a"])
    assert time.monotonic() - started < 10
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "memory unavailable"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with (
                _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
                conn.cursor() as cur,
            ):
                _bind_workspace(cur, workspace)
                cur.execute(
                    "SELECT count(*) FROM memory.messages WHERE content = %s",
                    (canary,),
                )
                row = cur.fetchone()
                assert row is not None
                assert row[0] == 0
            break
        except psycopg.Error:
            time.sleep(0.5)
    else:
        raise AssertionError("proxy did not recover")


def test_deriver_refuses_other_tenant(stack: Path) -> None:
    canary = f"canary-{uuid.uuid4().hex}"
    with _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn:
        with conn.cursor() as cur:
            _ensure_workspace(cur, "m1ws")
            _bind_workspace(cur, "m1ws")
            cur.execute(
                """
                INSERT INTO memory.queue (
                    work_unit_key, task_type, payload, processed, workspace_name
                )
                VALUES (%s, 'deletion', %s::jsonb, false, %s)
                """,
                (
                    "deletion:m1ws:message:m1msg",
                    json.dumps(
                        {
                            "tenant_id": "tenant-b",
                            "deletion_type": "message",
                            "resource_id": "m1msg",
                            "marker": canary,
                        }
                    ),
                    "m1ws",
                ),
            )
        conn.commit()

    deadline = time.monotonic() + 40
    error = None
    while time.monotonic() < deadline:
        with (
            _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
            conn.cursor() as cur,
        ):
            _bind_workspace(cur, "m1ws")
            cur.execute(
                "SELECT error FROM memory.queue WHERE work_unit_key = %s",
                ("deletion:m1ws:message:m1msg",),
            )
            row = cur.fetchone()
            if row and row[0]:
                error = row[0]
                break
        time.sleep(1)
    if error is None:
        logs = _compose(
            ["logs", "deriver-a", "--no-color", "--tail", "80"], check=False
        )
        ps = _compose(["ps", "deriver-a"], check=False)
        raise AssertionError(
            ps.stdout + ps.stderr + logs.stdout[-3000:] + logs.stderr[-2000:]
        )
    assert "SenticaTenantRefused" in str(error)
    logs = _compose(["logs", "deriver-a", "--no-color"], check=False)
    combined = logs.stdout + logs.stderr
    assert canary not in combined


def test_stopped_stack_leaves_no_memory_on_disk(stack: Path) -> None:
    """The fixture teardown runs after this test. Check live mounts first."""
    canary = f"disk-canary-{uuid.uuid4().hex}"
    key_a = _token_key(stack, "a")
    token = _token(
        key_a, issuer="sentica-test-issuer-a", tenant="tenant-a", request_id="disk"
    )
    httpx.post(
        f"{API_A}/v3/workspaces",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": canary},
        timeout=20,
    )
    mounts = _compose(
        [
            "exec",
            "-T",
            "database",
            "findmnt",
            "-n",
            "-o",
            "FSTYPE",
            "/var/lib/postgresql/data",
        ]
    )
    assert "tmpfs" in mounts.stdout
    redis = _compose(
        ["exec", "-T", "redis-a", "findmnt", "-n", "-o", "FSTYPE", "/data"]
    )
    assert "tmpfs" in redis.stdout
    for service in ("api-a", "deriver-a", "redis-a", "database"):
        kind = _run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.HostConfig.LogConfig.Type}}",
                f"{PROJECT}-{service}-1",
            ]
        )
        assert kind.stdout.strip() == "none"
        logs = _run(["docker", "logs", f"{PROJECT}-{service}-1"], check=False)
        assert canary not in logs.stdout + logs.stderr
    scanned = _run(
        ["sudo", "-E", "grep", "-R", "-l", canary, "/var/lib/docker/containers"],
        check=False,
    )
    assert scanned.returncode == 1
    volumes = _run(
        ["docker", "volume", "ls", "-q", "--filter", f"name={PROJECT}"], check=False
    )
    names = [line for line in volumes.stdout.splitlines() if line.strip()]
    assert names
    for name in names:
        inspected = _run(["docker", "volume", "inspect", name])
        assert "tmpfs" in inspected.stdout


def test_instances_cannot_reach_each_other(stack: Path) -> None:
    del stack
    redis_cross = _compose(
        [
            "exec",
            "-T",
            "api-b",
            "/app/.venv/bin/python",
            "-c",
            "import socket,sys\n"
            "try:\n"
            " socket.create_connection(('redis-a', 6379), 2)\n"
            "except OSError:\n"
            " sys.exit(0)\n"
            "sys.exit(1)\n",
        ]
    )
    assert redis_cross.returncode == 0
    database = _compose(
        [
            "exec",
            "-T",
            "api-a",
            "/app/.venv/bin/python",
            "-c",
            "import socket,sys\n"
            "try:\n"
            " socket.create_connection(('database', 5432), 2)\n"
            "except OSError:\n"
            " sys.exit(0)\n"
            "sys.exit(1)\n",
        ]
    )
    assert database.returncode == 0
    redis_ip = _compose(
        [
            "exec",
            "-T",
            "api-b",
            "/app/.venv/bin/python",
            "-c",
            "import socket,sys\n"
            "try:\n"
            " socket.create_connection(('172.28.10.12', 6379), 2)\n"
            "except OSError:\n"
            " sys.exit(0)\n"
            "sys.exit(1)\n",
        ]
    )
    assert redis_ip.returncode == 0
    database_ip = _compose(
        [
            "exec",
            "-T",
            "api-a",
            "/app/.venv/bin/python",
            "-c",
            "import socket,sys\n"
            "try:\n"
            " socket.create_connection(('172.28.12.10', 5432), 2)\n"
            "except OSError:\n"
            " sys.exit(0)\n"
            "sys.exit(1)\n",
        ]
    )
    assert database_ip.returncode == 0
    unauth = _compose(
        [
            "exec",
            "-T",
            "api-a",
            "/app/.venv/bin/python",
            "-c",
            "import socket\n"
            "s=socket.create_connection(('redis-a',6379),2)\n"
            "s.sendall(b'*1\\r\\n$4\\r\\nPING\\r\\n')\n"
            "data=s.recv(128)\n"
            "raise SystemExit(0 if b'NOAUTH' in data or b'ERR' in data else 1)\n",
        ]
    )
    assert unauth.returncode == 0, unauth.stdout + unauth.stderr
    save = _compose(
        [
            "exec",
            "-T",
            "api-a",
            "/app/.venv/bin/python",
            "-c",
            "import socket\n"
            "pw=open('/secret/password').read().strip().encode()\n"
            "s=socket.create_connection(('redis-a',6379),2)\n"
            "def send(parts):\n"
            "    body=b'*'+str(len(parts)).encode()+b'\\r\\n'\n"
            "    for part in parts:\n"
            "        body += b'$'+str(len(part)).encode()+b'\\r\\n'+part+b'\\r\\n'\n"
            "    s.sendall(body)\n"
            "send([b'AUTH', pw])\n"
            "s.recv(128)\n"
            "send([b'CONFIG', b'GET', b'save'])\n"
            "data=s.recv(256)\n"
            "raise SystemExit(0 if b'save' in data and b'$0\\r\\n' in data else 1)\n",
        ]
    )
    assert save.returncode == 0, save.stdout + save.stderr


def test_pool_recycle_is_short(stack: Path) -> None:
    del stack
    result = _compose(
        [
            "exec",
            "-T",
            "api-a",
            "/app/.venv/bin/python",
            "-c",
            "from src.db import engine; print(engine.pool._recycle)",
        ]
    )
    assert result.stdout.strip() == "2"


def test_production_image_refuses_test_mode(stack: Path) -> None:
    del stack
    result = _run(
        ["docker", "run", "--rm", "-e", "SENTICA_MODE=test", f"{PROJECT}-api-a"],
        check=False,
    )
    assert result.returncode != 0
    assert "production image" in result.stderr + result.stdout


def test_reconciler_and_webhook_complete(stack: Path) -> None:
    key_a = _token_key(stack, "a")
    token = _token(
        key_a, issuer="sentica-test-issuer-a", tenant="tenant-a", request_id="tasks"
    )
    created = httpx.post(
        f"{API_A}/v3/workspaces",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "m1-workspace"},
        timeout=20,
    )
    assert created.status_code in (200, 201), created.text
    enqueued = _compose(
        [
            "exec",
            "-T",
            "deriver-a",
            "/app/.venv/bin/python",
            "-c",
            "import asyncio\n"
            "from src.reconciler.scheduler import RECONCILER_TASKS, ReconcilerScheduler\n"
            "from src.webhooks.events import TestEvent, publish_webhook_event\n"
            "async def main():\n"
            "    scheduler = ReconcilerScheduler()\n"
            "    await scheduler._try_enqueue_task(RECONCILER_TASKS['cleanup_queue'])\n"
            "    await publish_webhook_event(TestEvent(workspace_id='m1-workspace'))\n"
            "asyncio.run(main())\n",
        ]
    )
    assert enqueued.returncode == 0, enqueued.stderr[-2000:]
    deadline = time.monotonic() + 40
    seen: dict[str, tuple[bool, str | None, str]] = {}
    while time.monotonic() < deadline:
        with (
            _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
            conn.cursor() as cur,
        ):
            _bind_workspace(cur, "m1-workspace")
            cur.execute(
                "SELECT task_type, processed, error, payload->>'tenant_id' "
                "FROM memory.queue WHERE task_type IN ('reconciler', 'webhook') "
                "ORDER BY id DESC LIMIT 10"
            )
            for task_type, processed, error, tenant in cur.fetchall():
                message = None if error is None else str(error)
                seen[str(task_type)] = (bool(processed), message, str(tenant))
        if (
            seen.get("reconciler", (False, "x", ""))[0]
            and seen.get("webhook", (False, "x", ""))[0]
        ):
            break
        time.sleep(1)
    assert seen.get("reconciler", (False, None, ""))[0] is True
    assert seen["reconciler"][1] in (None, "")
    assert seen["reconciler"][2] == "tenant-a"
    assert seen.get("webhook", (False, None, ""))[0] is True
    assert seen["webhook"][1] in (None, "")
    assert seen["webhook"][2] == "tenant-a"


def test_representation_rows_for_other_tenant_are_all_errored(stack: Path) -> None:
    from src.deriver.queue_manager import QueueManager

    source = inspect.getsource(
        QueueManager._process_work_unit  # pyright: ignore[reportPrivateUsage]
    )
    assert source.index("await self.reject_foreign_queue_rows") < source.index(
        "await self.get_queue_item_batch"
    )
    canary = f"rep-canary-{uuid.uuid4().hex}"
    work_unit = "representation:m1-workspace:m1-session:m1-peer"
    with _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn:
        with conn.cursor() as cur:
            _ensure_workspace(cur, "m1-workspace")
            _bind_workspace(cur, "m1-workspace")
            for _ in range(2):
                cur.execute(
                    """
                    INSERT INTO memory.queue (
                        work_unit_key, task_type, payload, processed, workspace_name
                    )
                    VALUES (%s, 'representation', %s::jsonb, false, %s)
                    """,
                    (
                        work_unit,
                        json.dumps({"tenant_id": "tenant-b", "content": canary}),
                        "m1-workspace",
                    ),
                )
        conn.commit()
    deadline = time.monotonic() + 40
    rows: list[tuple[Any, ...]] = []
    while time.monotonic() < deadline:
        with (
            _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
            conn.cursor() as cur,
        ):
            _bind_workspace(cur, "m1-workspace")
            cur.execute(
                "SELECT processed, error FROM memory.queue WHERE work_unit_key = %s",
                (work_unit,),
            )
            rows = cast(list[tuple[Any, ...]], cur.fetchall())
        if len(rows) >= 2 and all(row[0] and row[1] for row in rows):
            break
        time.sleep(1)
    assert len(rows) >= 2, rows
    assert all(row[0] for row in rows), rows
    assert all(row[1] and "SenticaTenantRefused" in str(row[1]) for row in rows)
    logs = _compose(["logs", "deriver-a", "--no-color"], check=False)
    assert canary not in logs.stdout + logs.stderr


def test_deriver_resumes_a_pending_row_once(stack: Path) -> None:
    workspace = "m1-resume"
    work_unit = "reconciler:cleanup_queue:m1-resume"
    _compose(["stop", "deriver-a"])
    count = 0
    error = "pending"
    try:
        with _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn:
            with conn.cursor() as cur:
                _ensure_workspace(cur, workspace)
                _bind_workspace(cur, workspace)
                cur.execute(
                    """
                    INSERT INTO memory.queue (
                        work_unit_key, task_type, payload, processed,
                        workspace_name
                    )
                    VALUES (%s, 'reconciler', %s::jsonb, false, %s)
                    """,
                    (
                        work_unit,
                        json.dumps(
                            {
                                "tenant_id": "tenant-a",
                                "reconciler_type": "cleanup_queue",
                            }
                        ),
                        workspace,
                    ),
                )
            conn.commit()
        _compose(["stop", "pgbouncer-a"])
        _compose(["start", "deriver-a"])
        time.sleep(2)
        _compose(["start", "pgbouncer-a"])
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                with (
                    _connect(
                        PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a"
                    ) as conn,
                    conn.cursor() as cur,
                ):
                    _bind_workspace(cur, workspace)
                    cur.execute(
                        "SELECT count(*), max(error) FROM memory.queue "
                        "WHERE work_unit_key = %s AND processed",
                        (work_unit,),
                    )
                    row = cur.fetchone()
                    assert row is not None
                    count = int(str(row[0]))
                    error = "" if row[1] is None else str(row[1])
                    if count == 1 and not error:
                        break
            except psycopg.Error:
                time.sleep(0.5)
                continue
            time.sleep(0.5)
    finally:
        _compose(["start", "pgbouncer-a"], check=False)
        _compose(["start", "deriver-a"], check=False)
    assert count == 1
    assert not error


def test_tenant_is_not_pgbouncer_admin(stack: Path) -> None:
    with (
        pytest.raises(psycopg.Error) as tenant_admin,
        _connect(PROXY_A, stack, "tenant_a", "pgbouncer", "tenant_a") as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SHUTDOWN")
    assert "connection refused" not in str(tenant_admin.value).lower()
    admin = _connect(
        PROXY_A, stack, "sentica_operator", "pgbouncer", "sentica_operator"
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute("SHOW VERSION")
            assert cur.fetchone() is not None
    finally:
        admin.close()


def test_database_error_canary_is_absent_from_logs(stack: Path) -> None:
    canary = f"log-canary-{uuid.uuid4().hex}"
    key_a = _token_key(stack, "a")
    token = _token(
        key_a,
        issuer="sentica-test-issuer-a",
        tenant="tenant-a",
        request_id="logs",
        workspace=canary,
    )
    headers = {"Authorization": f"Bearer {token}"}
    first = httpx.post(
        f"{API_A}/v3/workspaces",
        headers=headers,
        json={"name": canary},
        timeout=20,
    )
    assert first.status_code in (200, 201), first.text
    session_name = f"sess-{uuid.uuid4().hex[:8]}"
    peer_name = f"peer-{uuid.uuid4().hex[:8]}"
    session = httpx.post(
        f"{API_A}/v3/workspaces/{canary}/sessions",
        headers=headers,
        json={"id": session_name},
        timeout=20,
    )
    assert session.status_code in (200, 201), session.text
    peer = httpx.post(
        f"{API_A}/v3/workspaces/{canary}/peers",
        headers=headers,
        json={"name": peer_name},
        timeout=20,
    )
    assert peer.status_code in (200, 201), peer.text
    script = (
        "import asyncio, logging\n"
        "from sqlalchemy import text\n"
        "from src.db import SessionLocal\n"
        "async def main():\n"
        "    log = logging.getLogger('sentica.canary')\n"
        "    try:\n"
        "        async with SessionLocal() as db:\n"
        "            await db.execute(text('SELECT CAST(:canary AS integer)'), "
        "{'canary': " + repr(canary) + "})\n"
        "    except Exception:\n"
        "        log.exception('query failed')\n"
        "    logging.getLogger('uvicorn.access').info(\n"
        "        '127.0.0.1 - \"POST /v3/workspaces/%s/sessions/%s HTTP/1.1\" 201',\n"
        "        " + repr(canary) + ", " + repr(session_name) + ")\n"
        "    logging.getLogger('sentica.named').error(\n"
        "        'permission denied for workspace %s peer %s session %s\\n"
        "DETAIL: key %s',\n"
        "        "
        + repr(canary)
        + ", "
        + repr(peer_name)
        + ", "
        + repr(session_name)
        + ", "
        + repr(canary)
        + ")\n"
        "asyncio.run(main())\n"
    )
    ran = _compose(
        ["exec", "-T", "api-a", "/app/.venv/bin/python", "-"],
        check=False,
        stdin=script,
    )
    combined = ran.stdout + ran.stderr
    assert ran.returncode == 0, ran.stderr[-1500:]
    for secret in (canary, session_name, peer_name):
        assert secret not in combined
    assert "DETAIL:" not in combined
    assert "sentica.named" in combined


def test_named_request_and_dream_log_omit_names(stack: Path) -> None:
    canary = f"dream-canary-{uuid.uuid4().hex}"
    key_a = _token_key(stack, "a")
    token = _token(
        key_a,
        issuer="sentica-test-issuer-a",
        tenant="tenant-a",
        request_id="dream-logs",
        workspace=canary,
    )
    headers = {"Authorization": f"Bearer {token}"}
    created = httpx.post(
        f"{API_A}/v3/workspaces",
        headers=headers,
        json={"name": canary},
        timeout=20,
    )
    assert created.status_code in (200, 201), created.text
    session_name = f"sess-{uuid.uuid4().hex[:8]}"
    peer_name = f"peer-{uuid.uuid4().hex[:8]}"
    session = httpx.post(
        f"{API_A}/v3/workspaces/{canary}/sessions",
        headers=headers,
        json={"id": session_name},
        timeout=20,
    )
    assert session.status_code in (200, 201), session.text
    peer = httpx.post(
        f"{API_A}/v3/workspaces/{canary}/peers",
        headers=headers,
        json={"name": peer_name},
        timeout=20,
    )
    assert peer.status_code in (200, 201), peer.text
    script = (
        "import logging\n"
        "logging.basicConfig(level=logging.INFO)\n"
        "from src.db import SessionLocal\n"
        "workspace = " + repr(canary) + "\n"
        "peer = " + repr(peer_name) + "\n"
        "session = " + repr(session_name) + "\n"
        "logging.getLogger('src.dreamer.orchestrator').info(\n"
        "    f'dream cycle for {workspace}/{peer}/{session}')\n"
        "logging.getLogger('src.dreamer.orchestrator').info(\n"
        "    'observed=%s', peer)\n"
        "print('logged', SessionLocal is not None)\n"
    )
    for service in ("api-a", "deriver-a"):
        ran = _compose(
            ["exec", "-T", service, "/app/.venv/bin/python", "-"],
            check=False,
            stdin=script,
        )
        combined = ran.stdout + ran.stderr
        assert ran.returncode == 0, ran.stderr[-1500:]
        assert "logged" in combined
        for secret in (canary, session_name, peer_name):
            assert secret not in combined


def test_pg_hba_rejects_trust_and_cross_tenant(stack: Path) -> None:
    del stack
    shown = _compose(
        [
            "exec",
            "-T",
            "database",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-tAc",
            "SHOW hba_file",
        ]
    )
    assert shown.stdout.strip() == "/etc/sentica/pg_hba.conf"
    counted = _compose(
        [
            "exec",
            "-T",
            "database",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-tAc",
            "SELECT count(*) FROM pg_hba_file_rules"
            + " WHERE type LIKE 'host%' AND auth_method = 'trust'",
        ]
    )
    assert counted.stdout.strip() == "0"
    rejected = _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            f"container:{PROJECT}-pgbouncer-b-1",
            "pgvector/pgvector:pg15",
            "psql",
            "host=172.28.12.10 port=5432 user=tenant_a dbname=tenant_a"
            + " sslmode=require connect_timeout=5",
            "-c",
            "SELECT 1",
        ],
        check=False,
        timeout=20,
    )
    assert rejected.returncode != 0
    assert "pg_hba" in (rejected.stderr + rejected.stdout).lower()


def test_containers_cannot_read_another_tenant_key(stack: Path) -> None:
    del stack
    own = _compose(["exec", "-T", "api-a", "sh", "-c", "test -r /certs/tenant_a.key"])
    assert own.returncode == 0
    for service, path in (
        ("api-a", "/certs/tenant_b.key"),
        ("deriver-a", "/certs/tenant_b.key"),
        ("api-b", "/certs/tenant_a.key"),
        ("deriver-b", "/certs/tenant_a.key"),
        ("pgbouncer-a", "/certs/tenant_a.key"),
        ("pgbouncer-b", "/in/tenant_b.key"),
    ):
        missing = _compose(["exec", "-T", service, "sh", "-c", f"test ! -e {path}"])
        assert missing.returncode == 0, service


def test_migration_exits_when_proxy_is_paused(stack: Path) -> None:
    del stack
    _compose(["pause", "pgbouncer-a"])
    started = time.monotonic()
    try:
        result = _compose(
            [
                "run",
                "--no-deps",
                "--rm",
                "-T",
                "-e",
                "DB_MIGRATION_LOCK_WAIT_SECONDS=2",
                "-e",
                "SENTICA_MIGRATION_TIMEOUT_SECONDS=8",
                "migrate-a",
            ],
            check=False,
            timeout=40,
        )
    finally:
        _compose(["unpause", "pgbouncer-a"], check=False)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output[-2000:]
    assert time.monotonic() - started < 25
    assert "Starting API server" not in output


def test_migration_exits_when_cert_is_revoked(stack: Path) -> None:
    revoked = stack.parent / "revoked"
    revoked.mkdir()
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "other")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "other")]))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tenant_a")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=2))
        .sign(ca_key, SHA256())
    )
    cert_path = revoked / "tenant_a.crt"
    key_path = revoked / "tenant_a.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    started = time.monotonic()
    result = _compose(
        [
            "run",
            "--no-deps",
            "--rm",
            "-T",
            "-v",
            f"{cert_path}:/certs/revoked.crt:ro",
            "-v",
            f"{key_path}:/certs/revoked.key:ro",
            "-e",
            "SENTICA_CLIENT_CERT_PATH=/certs/revoked.crt",
            "-e",
            "SENTICA_CLIENT_KEY_PATH=/certs/revoked.key",
            "api-a",
        ],
        check=False,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output[-2000:]
    assert time.monotonic() - started < 120
    assert "Starting API server" not in output


def test_api_and_deriver_have_no_tenant_password(stack: Path) -> None:
    secret_dir = stack.parent / "secrets"
    passwords: list[str] = []
    for name in ("userlist-a.txt", "userlist-b.txt"):
        for line in (secret_dir / name).read_text(encoding="utf-8").splitlines():
            if not line or "sentica_operator" in line:
                continue
            passwords.append(line.split('"')[3])
    assert len(passwords) == 4
    # Search inside the container and print only hits. Copying every file
    # back to the test process exhausts memory.
    script = (
        "import pathlib\n"
        f"passwords = {passwords!r}\n"
        "env = pathlib.Path('/proc/1/environ').read_bytes()\n"
        "for password in passwords:\n"
        "    if password.encode() in env:\n"
        "        print('ENV_HIT')\n"
        "skip = ('/proc', '/sys', '/dev')\n"
        "for path in pathlib.Path('/').rglob('*'):\n"
        "    text = str(path)\n"
        "    if text.startswith(skip):\n"
        "        continue\n"
        "    if (\n"
        "        text == '/secrets' or text.startswith('/secrets/')\n"
        "        or text == '/in' or text.startswith('/in/')\n"
        "        or path.name.startswith('userlist')\n"
        "    ):\n"
        "        print('FORBIDDEN', text)\n"
        "    if not path.is_file():\n"
        "        continue\n"
        "    try:\n"
        "        data = path.read_bytes()\n"
        "    except OSError:\n"
        "        continue\n"
        "    if any(password.encode() in data for password in passwords):\n"
        "        print('FILE_HIT', text)\n"
    )
    for service in ("api-a", "api-b", "deriver-a", "deriver-b"):
        ran = _compose(
            ["exec", "-T", service, "/app/.venv/bin/python", "-"],
            check=False,
            stdin=script,
        )
        assert ran.returncode == 0, ran.stderr[-1500:]
        combined = ran.stdout + ran.stderr
        assert "FORBIDDEN" not in combined
        assert "ENV_HIT" not in combined
        assert "FILE_HIT" not in combined


def test_tenant_a_cannot_escape_its_workspace(stack: Path) -> None:
    for statement in (
        "ALTER TABLE memory.messages NO FORCE ROW LEVEL SECURITY",
        "DROP POLICY sentica_workspace ON memory.messages",
        "CREATE TABLE memory.should_fail (id int)",
    ):
        _assert_sqlstate(stack, statement, "42501")
    left = "a" + uuid.uuid4().hex[:20]
    right = "b" + uuid.uuid4().hex[:20]
    with (
        _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT pg_has_role(current_user, 'tenant_a_ddl', 'USAGE')")
        assert cur.fetchone() == (False,)
        for name in (left, right):
            _ensure_workspace(cur, name)
        conn.commit()
        _bind_workspace(cur, left)
        cur.execute(
            "INSERT INTO memory.peers (id, name, workspace_name) VALUES (%s, %s, %s)",
            ("p" + uuid.uuid4().hex[:20], "peer", left),
        )
        with pytest.raises(psycopg.Error) as cross:
            cur.execute(
                "INSERT INTO memory.peers (id, name, workspace_name) "
                "VALUES (%s, %s, %s)",
                ("q" + uuid.uuid4().hex[:20], "peer", right),
            )
        assert cross.value.sqlstate == "42501"
        conn.rollback()
        _bind_workspace(cur, left)
        cur.execute(
            "INSERT INTO memory.peers (id, name, workspace_name) VALUES (%s, %s, %s)",
            ("p" + uuid.uuid4().hex[:20], "peer", left),
        )
        with pytest.raises(psycopg.Error) as moved:
            cur.execute(
                "UPDATE memory.peers SET workspace_name = %s WHERE workspace_name = %s",
                (right, left),
            )
        assert moved.value.sqlstate == "42501"
        conn.rollback()
        _bind_workspace(cur, left)
        cur.execute(
            "INSERT INTO memory.peers (id, name, workspace_name) VALUES (%s, %s, %s)",
            ("p" + uuid.uuid4().hex[:20], "peer", left),
        )
        conn.commit()
        _bind_workspace(cur, "")
        cur.execute("SELECT count(*) FROM memory.peers")
        hidden = cur.fetchone()
        assert hidden is not None and hidden[0] == 0
        _bind_workspace(cur, left)
        cur.execute("SELECT count(*) FROM memory.peers")
        visible = cur.fetchone()
        assert visible is not None and visible[0] == 1


def test_ddl_key_is_not_mounted_into_api_or_deriver(stack: Path) -> None:
    del stack
    for service in ("api-a", "api-b", "deriver-a", "deriver-b"):
        ran = _compose(
            [
                "exec",
                "-T",
                service,
                "sh",
                "-c",
                "test ! -e /certs/tenant_a_ddl.key && test ! -e /certs/tenant_b_ddl.key",
            ],
            check=False,
        )
        assert ran.returncode == 0, ran.stderr[-500:]


def test_posted_message_embedding_leaves_pending(stack: Path) -> None:
    workspace = "m2embed" + uuid.uuid4().hex[:8]
    content = "embed-" + uuid.uuid4().hex
    private = _token_key(stack, "a")
    token = _token(
        private,
        issuer="sentica-test-issuer-a",
        tenant="tenant-a",
        workspace=workspace,
    )
    headers = {"Authorization": f"Bearer {token}"}
    created = httpx.post(
        f"{API_A}/v3/workspaces",
        headers=headers,
        json={"name": workspace},
        timeout=20,
    )
    assert created.status_code in (200, 201), created.text
    assert httpx.post(
        f"{API_A}/v3/workspaces/{workspace}/sessions",
        headers=headers,
        json={"id": "s1"},
        timeout=20,
    ).status_code in (200, 201)
    assert httpx.post(
        f"{API_A}/v3/workspaces/{workspace}/peers",
        headers=headers,
        json={"name": "p1"},
        timeout=20,
    ).status_code in (200, 201)
    posted = httpx.post(
        f"{API_A}/v3/workspaces/{workspace}/sessions/s1/messages",
        headers=headers,
        json={
            "messages": [{"peer_id": "p1", "content": content, "source_trust": "owner"}]
        },
        timeout=20,
    )
    assert posted.status_code in (200, 201), posted.text
    # The request path may embed the row immediately. Put it back so the
    # scheduler gate, not that path, is what the deriver claims.
    deadline = time.monotonic() + 20
    saw_row = False
    while time.monotonic() < deadline:
        with (
            _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
            conn.cursor() as cur,
        ):
            _bind_workspace(cur, workspace)
            cur.execute(
                "SELECT sync_state FROM memory.message_embeddings WHERE content = %s",
                (content,),
            )
            row = cur.fetchone()
        if row is not None:
            saw_row = True
            if str(row[0]) != "pending":
                break
        time.sleep(0.5)
    assert saw_row
    with (
        _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
        conn.cursor() as cur,
    ):
        _bind_workspace(cur, workspace)
        cur.execute(
            "UPDATE memory.message_embeddings SET sync_state = 'pending', "
            "embedding = NULL, last_sync_at = NULL WHERE content = %s",
            (content,),
        )
        conn.commit()
    script = (
        "import asyncio\n"
        "from src.dependencies import tracked_db\n"
        "from src.reconciler.scheduler import RECONCILER_TASKS, ReconcilerScheduler\n"
        "from src.sentica.workspace_scope import fetch_pending_counts\n"
        "wanted = " + repr(workspace) + "\n"
        "async def main():\n"
        "    async with tracked_db('stack.counts', read_only=True) as db:\n"
        "        counts = await fetch_pending_counts(db)\n"
        "    assert counts is not None\n"
        "    names = [\n"
        "        row.workspace_name for row in counts if row.pending_embeddings > 0\n"
        "    ]\n"
        "    print('gate', wanted in names)\n"
        "    scheduler = ReconcilerScheduler()\n"
        "    enqueued = await scheduler._try_enqueue_task(\n"
        "        RECONCILER_TASKS['sync_vectors']\n"
        "    )\n"
        "    print('enqueued', bool(enqueued))\n"
        "asyncio.run(main())\n"
    )
    assert "run_vector_reconciliation_cycle" not in script
    ran = _compose(
        ["exec", "-T", "deriver-a", "/app/.venv/bin/python", "-"],
        check=False,
        stdin=script,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr[-1500:] + ran.stdout[-1500:]
    assert "gate True" in ran.stdout
    assert "enqueued True" in ran.stdout
    deadline = time.monotonic() + 90
    state = "pending"
    queue_ok = False
    row: tuple[object, ...] | None = None
    queued: tuple[object, ...] | None = None
    while time.monotonic() < deadline and (state != "synced" or not queue_ok):
        with (
            _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
            conn.cursor() as cur,
        ):
            _bind_workspace(cur, workspace)
            cur.execute(
                "SELECT sync_state, embedding::text "
                "FROM memory.message_embeddings WHERE content = %s",
                (content,),
            )
            row = cur.fetchone()
            cur.execute(
                "SELECT processed, error IS NULL FROM memory.queue "
                "WHERE work_unit_key = %s",
                ("reconciler:sync_vectors:" + workspace,),
            )
            queued = cur.fetchone()
        assert row is not None
        state = str(row[0])
        queue_ok = queued is not None and queued[0] is True and queued[1] is True
        if state != "synced" or not queue_ok:
            time.sleep(1)
    assert state == "synced", (state, row, queued)
    assert row is not None
    vector = str(row[1])
    assert vector.startswith("[")
    assert not vector.startswith("[0,0,")
    assert queue_ok


def test_api_refuses_a_stale_schema_and_a_runtime_cert_as_ddl(stack: Path) -> None:
    """api-a will not boot on an old revision, and the runtime cert is not ddl."""
    with (
        _connect(PROXY_A, stack, "tenant_a_ddl", "tenant_a", "tenant_a_ddl") as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT version_num FROM memory.alembic_version")
        current = cur.fetchone()
        assert current is not None
        head = str(current[0])
        cur.execute(
            "UPDATE memory.alembic_version SET version_num = %s",
            ("3e7a1c9d5b20",),
        )
        conn.commit()
    # api-a already owns 172.28.10.10. The one-off uses another address on net-a.
    override = stack.parent / "api-a-once.yml"
    override.write_text(
        "services:\n"
        "  api-a:\n"
        "    networks:\n"
        "      net-a:\n"
        "        ipv4_address: 172.28.10.50\n",
        encoding="utf-8",
    )
    try:
        ran = _compose(
            ["run", "--no-deps", "--rm", "-T", "api-a"],
            check=False,
            timeout=90,
            extra_files=[str(override)],
        )
        output = ran.stdout + ran.stderr
        assert ran.returncode != 0, output[-1500:]
        assert "Refusing to start: schema revision is behind" in output
    finally:
        override.unlink(missing_ok=True)
        with (
            _connect(
                PROXY_A, stack, "tenant_a_ddl", "tenant_a", "tenant_a_ddl"
            ) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "UPDATE memory.alembic_version SET version_num = %s",
                (head,),
            )
            conn.commit()
    with pytest.raises(psycopg.OperationalError):
        _connect(PROXY_A, stack, "tenant_a_ddl", "tenant_a", "tenant_a").close()


def test_deriver_reaches_the_mock_gateway_with_the_four_headers(stack: Path) -> None:
    workspace = "m2gate" + uuid.uuid4().hex[:8]
    content = "derive-" + uuid.uuid4().hex
    private = _token_key(stack, "a")
    token = _token(
        private,
        issuer="sentica-test-issuer-a",
        tenant="tenant-a",
        workspace=workspace,
    )
    headers = {"Authorization": f"Bearer {token}"}
    created = httpx.post(
        f"{API_A}/v3/workspaces",
        headers=headers,
        json={"name": workspace},
        timeout=20,
    )
    assert created.status_code in (200, 201), created.text
    assert httpx.post(
        f"{API_A}/v3/workspaces/{workspace}/sessions",
        headers=headers,
        json={"id": "s1"},
        timeout=20,
    ).status_code in (200, 201)
    assert httpx.post(
        f"{API_A}/v3/workspaces/{workspace}/peers",
        headers=headers,
        json={"name": "p1"},
        timeout=20,
    ).status_code in (200, 201)
    posted = httpx.post(
        f"{API_A}/v3/workspaces/{workspace}/sessions/s1/messages",
        headers=headers,
        json={
            "messages": [{"peer_id": "p1", "content": content, "source_trust": "owner"}]
        },
        timeout=20,
    )
    assert posted.status_code in (200, 201), posted.text
    deadline = time.monotonic() + 90
    found = False
    while time.monotonic() < deadline and not found:
        with (
            _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
            conn.cursor() as cur,
        ):
            _bind_workspace(cur, workspace)
            cur.execute(
                "SELECT content FROM memory.documents WHERE workspace_name = %s",
                (workspace,),
            )
            found = any(row[0] == "likes tea" for row in cur.fetchall())
        if not found:
            time.sleep(1)
    assert found
    captured = _compose(
        [
            "exec",
            "-T",
            "embeddings",
            "python",
            "-c",
            "import urllib.request; "
            "print(urllib.request.urlopen("
            "'http://127.0.0.1:8080/__capture', timeout=5).read().decode())",
        ],
        timeout=30,
    )
    payload = json.loads(captured.stdout)
    requests = payload["requests"]
    assert requests
    wanted = {
        "X-Sentica-Tenant",
        "X-Sentica-Workspace",
        "X-Sentica-Purpose",
        "X-Sentica-Request-Id",
    }
    matched = [
        row
        for row in requests
        if wanted <= set(row["headers"])
        and all(row["headers"][name] for name in wanted)
        and row["headers"]["X-Sentica-Workspace"] == workspace
    ]
    assert matched
    assert any(row["headers"]["X-Sentica-Purpose"] == "deriver" for row in matched)


def _service_id(service: str) -> str:
    """Container id, including a one-shot service that has already exited.

    ``compose ps`` hides exited containers. The migrate jobs exit 0, so a
    runtime check has to ask for every status.
    """
    found = _compose(["ps", "-a", "-q", service], check=True)
    ids = [line.strip() for line in found.stdout.splitlines() if line.strip()]
    assert len(ids) == 1, (service, ids)
    return ids[0]


def test_api_and_deriver_cannot_reach_the_internet(stack: Path) -> None:
    """Internal networks have no route to the public internet."""
    del stack
    script = (
        "import socket\n"
        "sock = socket.socket()\n"
        "sock.settimeout(3)\n"
        "try:\n"
        "    sock.connect(('1.1.1.1', 443))\n"
        "except OSError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    for service in ("api-a", "api-b", "deriver-a", "deriver-b"):
        result = _compose(
            ["exec", "-T", service, "python", "-c", script],
            check=False,
        )
        assert result.returncode == 0, service + result.stderr


def test_root_filesystem_is_read_only(stack: Path) -> None:
    """The app user cannot create a file under /app on a read-only root."""
    del stack
    script = "open('/app/probe-write', 'w').write('x')\n"
    for service in ("api-a", "api-b", "deriver-a", "deriver-b"):
        inspected = _run(["docker", "inspect", _service_id(service)], check=True)
        payload = cast(list[dict[str, Any]], json.loads(inspected.stdout))
        host = cast(dict[str, Any], payload[0]["HostConfig"])
        assert host.get("ReadonlyRootfs") is True, service
        result = _compose(
            ["exec", "-T", service, "python", "-c", script],
            check=False,
        )
        assert result.returncode != 0, service
        assert "Read-only file system" in result.stderr, service + result.stderr


def test_redis_runs_as_a_non_root_user(stack: Path) -> None:
    del stack
    for service in ("redis-a", "redis-b"):
        result = _compose(
            ["exec", "-T", service, "cat", "/proc/1/status"],
            check=True,
        )
        uid = ""
        for line in result.stdout.splitlines():
            if line.startswith("Uid:"):
                uid = line.split()[1]
        assert uid and uid != "0", service + " " + result.stdout


def test_no_container_has_net_admin(stack: Path) -> None:
    del stack
    listing = _compose(["ps", "-q"], check=True)
    ids = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
    assert ids
    for container_id in ids:
        inspected = _run(["docker", "inspect", container_id], check=True)
        payload = cast(list[dict[str, Any]], json.loads(inspected.stdout))
        host = cast(dict[str, Any], payload[0]["HostConfig"])
        added = cast(list[object], host.get("CapAdd") or [])
        dropped = [str(item) for item in cast(list[object], host.get("CapDrop") or [])]
        assert "NET_ADMIN" not in added, container_id
        assert host.get("Privileged") is False, container_id
        labels = cast(dict[str, object], payload[0]["Config"].get("Labels") or {})
        service = str(labels.get("com.docker.compose.service", ""))
        hardened = service.startswith(("api-", "deriver-", "migrate-", "publish-"))
        if hardened:
            assert "ALL" in dropped, service


def _runsc_service_names(overlay: str) -> list[str]:
    """Service names in the gVisor overlay whose runtime is runsc."""
    current = ""
    found: list[str] = []
    for line in overlay.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.endswith(":"):
            current = line.strip()[:-1]
        if line.strip() == "runtime: runsc" and current:
            found.append(current)
    return found


def test_gvisor_services_use_runsc(stack: Path) -> None:
    """Every service the overlay assigns to runsc is on that runtime.

    The default stack job does not install runsc. It still checks the overlay
    and the pinned checksum. The gVisor job sets SENTICA_GVISOR and reads
    HostConfig.Runtime.
    """
    del stack
    overlay = Path("docker-compose.gvisor.yml").read_text(encoding="utf-8")
    services = _runsc_service_names(overlay)
    required = {"migrate-a", "migrate-b", "api-a", "api-b", "deriver-a", "deriver-b"}
    assert required.issubset(services)
    assert "embeddings" not in services
    pin = Path("docker/sentica/runsc-20261012.sha512").read_text(encoding="utf-8")
    assert (
        "9606445eed5aedfcf05310e7ccef61d994089914c6622fc82624c24d2b18db1b"
        "2210f40e40a7be27b8b7772859de7d95c501b4eba66423053c960bf671377c5e" in pin
    )
    assert (
        "2fe7425abd16ad6ca649ab9bc3d7e8e76756834d86ffcc08dbe97c82c3b7262a"
        "7d6401f23632f2b296649cc1408b560416b2ab65a4043aa123fc7b90f3a1b2e2" in pin
    )
    workflow = Path(".github/workflows/unittest.yml").read_text(encoding="utf-8")
    assert "release/latest" not in workflow
    assert "release/20261012/" in workflow
    if os.environ.get("SENTICA_GVISOR") != "1":
        return
    for service in services:
        inspected = _run(["docker", "inspect", _service_id(service)], check=True)
        payload = cast(list[dict[str, Any]], json.loads(inspected.stdout))
        host = cast(dict[str, Any], payload[0]["HostConfig"])
        assert host.get("Runtime") == "runsc", service


def test_publishers_are_single_port_and_non_root(stack: Path) -> None:
    del stack
    expected = {
        "publish-a-api",
        "publish-a-proxy",
        "publish-b-api",
        "publish-b-proxy",
    }
    for service in expected:
        container_id = _service_id(service)
        inspected = _run(["docker", "inspect", container_id], check=True)
        payload = cast(list[dict[str, Any]], json.loads(inspected.stdout))
        config = cast(dict[str, Any], payload[0]["Config"])
        host = cast(dict[str, Any], payload[0]["HostConfig"])
        assert str(config.get("User", "")).startswith("65534")
        assert host.get("ReadonlyRootfs") is True
        bindings = cast(dict[str, object], host.get("PortBindings") or {})
        assert len(bindings) == 1, service
        for targets in bindings.values():
            assert isinstance(targets, list), service
            entries = cast(list[object], targets)
            for binding in entries:
                record = cast(dict[str, object], binding)
                assert record.get("HostIp") == "127.0.0.1", service
