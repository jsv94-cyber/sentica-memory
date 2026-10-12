"""Two-instance proof for the local certificate proxy.

Run with SENTICA_M1_STACK=1. The default suite does not collect this file.
The stack is Docker Compose on this machine: a local test CA, two
PgBouncers, two APIs, two derivers, two Redis processes, and one Postgres
whose data directory is tmpfs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import psycopg
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.sentica.yml"
PROJECT = "sentica-m1"
cert_dir_holder: list[str] = []
API_A = "http://127.0.0.1:8101"
API_B = "http://127.0.0.1:8102"
PROXY_A = 6433
PROXY_B = 6434


def _run(
    args: list[str], *, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        args,
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=check,
    )


def _compose(
    args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    if not cert_dir_holder:
        raise RuntimeError("CERT_DIR is not set")
    return _run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args],
        check=check,
        env={"CERT_DIR": cert_dir_holder[0]},
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
        "w": workspace,
    }
    if admin:
        payload["ad"] = True
    encoded = jwt.encode(payload, private, algorithm="EdDSA")
    assert isinstance(encoded, str)
    return encoded


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


def _reissue_client(cert_dir: Path, common_name: str) -> None:
    ca_key = serialization.load_pem_private_key(
        (cert_dir / "ca.key").read_bytes(), password=None
    )
    ca_cert = x509.load_pem_x509_certificate((cert_dir / "ca.crt").read_bytes())
    assert isinstance(ca_key, rsa.RSAPrivateKey)
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
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(key_path, 0o600)


@pytest.fixture(scope="module")
def stack() -> Iterator[Path]:
    cert_dir = Path("/dev/shm") / f"sentica-m1-{uuid.uuid4().hex}"
    cert_dir.mkdir(parents=True, exist_ok=False)
    cert_dir_holder.clear()
    cert_dir_holder.append(str(cert_dir))
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
        _compose(["build", "api-a"])
        image = f"{PROJECT}-api-a"
        uid_run = _run(
            ["docker", "run", "--rm", "--entrypoint", "id", image, "-u"],
            check=False,
        )
        if uid_run.returncode != 0:
            raise AssertionError(uid_run.stderr + uid_run.stdout)
        uid = uid_run.stdout.strip()
        for name in ("tenant_a.key", "tenant_b.key"):
            path = cert_dir / name
            os.chown(path, int(uid), int(uid))
            os.chmod(path, 0o600)
        proxy_uid = _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "id",
                "edoburu/pgbouncer:v1.24.1-p1",
                "-u",
            ]
        ).stdout.strip()
        for name in ("pgbouncer-a.key", "pgbouncer-b.key"):
            path = cert_dir / name
            os.chown(path, int(proxy_uid), int(proxy_uid))
            os.chmod(path, 0o600)
        started = _compose(["up", "-d", "--build"], check=False)
        if started.returncode != 0:
            raise AssertionError(started.stderr[-4000:] + started.stdout[-2000:])
        deadline = time.monotonic() + 180
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
            logs = _compose(["logs", "--no-color"], check=False)
            raise AssertionError(logs.stdout[-8000:] + logs.stderr[-4000:] + last)
        yield cert_dir
    finally:
        _compose(["down", "--volumes", "--remove-orphans"], check=False)
        shutil.rmtree(cert_dir, ignore_errors=True)
        volumes = _run(
            ["docker", "volume", "ls", "-q", "--filter", f"name={PROJECT}"],
            check=False,
        )
        if volumes.stdout.strip() or cert_dir.exists():
            raise AssertionError("memory content or certificates remained after stop")


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
    key_a = (stack / "tenant-a.token.key").read_bytes()
    key_b = (stack / "tenant-b.token.key").read_bytes()
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
    for path, methods in paths.items():
        for method in methods:
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            if path in {"/health", "/metrics"}:
                continue
            url = API_A + path.replace("{", "x").replace("}", "")
            response = httpx.request(
                method.upper(),
                url,
                headers={"Authorization": f"Bearer {good_b}"},
                timeout=20,
            )
            assert response.status_code == 401, (
                method,
                path,
                response.status_code,
                response.text,
            )
            assert response.json()["detail"] in {"wrong tenant", "forged token"}

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

    redis = _compose(
        ["exec", "-T", "redis-a", "redis-cli", "CONFIG", "GET", "appendonly"]
    )
    assert "no" in redis.stdout
    save = _compose(["exec", "-T", "redis-a", "redis-cli", "CONFIG", "GET", "save"])
    assert (
        save.stdout.strip().endswith("save")
        or "\n\n" in save.stdout
        or save.stdout.strip() == "save"
    )

    with (
        _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
        conn.cursor() as cur,
    ):
        with pytest.raises(psycopg.Error):
            cur.execute("SELECT id FROM public.outside_memory")
        conn.rollback()

    with pytest.raises(psycopg.Error):
        _connect(PROXY_B, stack, "tenant_a", "tenant_b", "tenant_a")


def test_certificate_swap_has_no_failed_requests(stack: Path) -> None:
    key_a = (stack / "tenant-a.token.key").read_bytes()
    token = _token(
        key_a, issuer="sentica-test-issuer-a", tenant="tenant-a", request_id="swap"
    )
    headers = {"Authorization": f"Bearer {token}"}
    failures: list[str] = []

    def _once(i: int) -> None:
        response = httpx.post(
            f"{API_A}/v3/workspaces",
            headers=headers,
            json={"name": "m1-workspace"},
            timeout=20,
        )
        if response.status_code not in (200, 201):
            failures.append(f"{i}:{response.status_code}:{response.text[:200]}")

    for i in range(5):
        _once(i)
    uid = (stack / "tenant_a.key").stat().st_uid
    _reissue_client(stack, "tenant_a")
    os.chown(stack / "tenant_a.key", uid, uid)
    os.chmod(stack / "tenant_a.key", 0o600)
    for i in range(20):
        _once(i + 5)
    assert failures == []


def test_revocation_rolls_back_and_reports_unavailable(stack: Path) -> None:
    with _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS memory.revoke_probe (id int primary key)"
            )
            cur.execute("DELETE FROM memory.revoke_probe")
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("INSERT INTO memory.revoke_probe (id) VALUES (1)")
            cur.execute("INSERT INTO memory.revoke_probe (id) VALUES (2)")
            _compose(["kill", "-s", "KILL", "pgbouncer-a"])
            with pytest.raises(psycopg.Error):
                conn.commit()

    _compose(["start", "pgbouncer-a"])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with (
                _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn,
                conn.cursor() as cur,
            ):
                cur.execute("SELECT count(*) FROM memory.revoke_probe")
                row = cur.fetchone()
                assert row is not None
                assert row[0] == 0
            break
        except psycopg.Error:
            time.sleep(1)
    else:
        raise AssertionError("proxy did not recover")

    _compose(["stop", "pgbouncer-a"])
    try:
        key_a = (stack / "tenant-a.token.key").read_bytes()
        workspace = f"revoke-{uuid.uuid4().hex[:12]}"
        token = _token(
            key_a,
            issuer="sentica-test-issuer-a",
            tenant="tenant-a",
            request_id="revoked",
            workspace=workspace,
        )
        response = httpx.post(
            f"{API_A}/v3/workspaces",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": workspace},
            timeout=20,
        )
        assert response.status_code == 503, response.text
        assert response.json()["detail"] == "memory unavailable"
    finally:
        _compose(["start", "pgbouncer-a"])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with _connect(
                    PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a"
                ) as conn:
                    conn.close()
                break
            except psycopg.Error:
                time.sleep(1)
        else:
            raise AssertionError("proxy did not come back")


def test_deriver_refuses_other_tenant(stack: Path) -> None:
    canary = f"canary-{uuid.uuid4().hex}"
    with _connect(PROXY_A, stack, "tenant_a", "tenant_a", "tenant_a") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memory.queue (work_unit_key, task_type, payload, processed)
                VALUES (%s, 'deletion', %s::jsonb, false)
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
    del stack
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
    volumes = _run(
        ["docker", "volume", "ls", "-q", "--filter", f"name={PROJECT}"], check=False
    )
    assert volumes.stdout.strip() == ""
