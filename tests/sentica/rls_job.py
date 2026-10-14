"""Helpers for the runtime-role job. The unit suite never sets SENTICA_RLS_JOB."""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.sentica.runtime import TOKEN_AUDIENCE


def rls_job() -> bool:
    """True when pytest is the test-m2-rls job."""
    return os.environ.get("SENTICA_RLS_JOB") == "1"


def defined_m2_tests() -> int:
    """How many test functions the M2 files define, before parametrization."""
    root = Path(__file__).resolve().parent
    total = 0
    for path in sorted(root.glob("test_m2_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name.startswith("test_"):
                total += 1
    return total


def runtime_dsn() -> str:
    """Passwordless TLS URI for the runtime role."""
    user = os.environ["SENTICA_DB_USER"]
    host = os.environ["SENTICA_DB_PROXY_HOST"]
    port = os.environ["SENTICA_DB_PROXY_PORT"]
    name = os.environ["SENTICA_DB_NAME"]
    ca = quote(os.environ["SENTICA_PROXY_CA_PATH"], safe="")
    cert = quote(os.environ["SENTICA_CLIENT_CERT_PATH"], safe="")
    key = quote(os.environ["SENTICA_CLIENT_KEY_PATH"], safe="")
    return (
        "postgresql://"
        + user
        + "@"
        + host
        + ":"
        + port
        + "/"
        + name
        + "?sslmode=verify-full&sslrootcert="
        + ca
        + "&sslcert="
        + cert
        + "&sslkey="
        + key
    )


def runtime_sqlalchemy_url() -> str:
    """SQLAlchemy URL for the same runtime connection."""
    return runtime_dsn().replace("postgresql://", "postgresql+psycopg://", 1)


def ddl_dsn() -> str:
    """Trust connection for the migration role. It is not the app engine."""
    host = os.environ["SENTICA_DB_PROXY_HOST"]
    port = os.environ["SENTICA_DB_PROXY_PORT"]
    name = os.environ["SENTICA_DB_NAME"]
    user = os.environ["SENTICA_DB_USER"] + "_ddl"
    return "postgresql://" + user + "@" + host + ":" + port + "/" + name


def sync_dsn(worker_id: str) -> str:
    """Worker database in the unit suite, runtime role in the RLS job."""
    if rls_job():
        return runtime_dsn()
    from sqlalchemy.engine.url import make_url

    run_id = os.environ.get("HONCHO_TEST_RUN_ID", "local")
    suffix = "" if worker_id == "master" else "_" + worker_id
    url = make_url(os.environ["DB_CONNECTION_URI"]).set(
        drivername="postgresql",
        database="test_db_" + run_id + suffix,
    )
    return url.render_as_string(hide_password=False)


def sign_broker_token(
    workspaces: list[str],
    *,
    primary: str | None = None,
    peer: str | None = None,
) -> str:
    """Sign a broker token with the job's private key."""
    raw = Path(os.environ["SENTICA_TOKEN_SIGNING_KEY"]).read_bytes()
    loaded = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise RuntimeError("broker signing key must be Ed25519")
    workspace = primary if primary is not None else workspaces[0]
    issued = int(time.time())
    claims: dict[str, Any] = {
        "iss": os.environ["SENTICA_TOKEN_ISSUER"],
        "aud": TOKEN_AUDIENCE,
        "iat": issued,
        "exp": issued + 60,
        "request_id": "m2-" + workspace + "-" + str(issued),
        "tenant_id": os.environ["SENTICA_TENANT_ID"],
        "w": workspace,
        "workspaces": workspaces,
    }
    if peer is not None:
        claims["p"] = peer
    return jwt.encode(claims, loaded, algorithm="EdDSA")


def authorize(
    client: object,
    workspaces: list[str],
    *,
    primary: str | None = None,
    peer: str | None = None,
) -> None:
    """Attach a real broker token. The unit suite leaves the client alone."""
    if not rls_job():
        return
    headers = getattr(client, "headers", None)
    if headers is None or not hasattr(headers, "__setitem__"):
        raise RuntimeError("client has no headers")
    token = sign_broker_token(workspaces, primary=primary, peer=peer)
    headers["Authorization"] = "Bearer " + token
