"""M1: broker tokens are bound to one tenant and a short lifetime."""

from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from src.exceptions import AuthenticationException
from src.sentica.queue import SenticaTenantRefused, pop_and_check_tenant
from src.sentica.refusals import refusal_count, reset_refusals
from src.sentica.runtime import TOKEN_AUDIENCE, SenticaInstance
from src.sentica.tokens import verify_sentica_token

ISSUER = "sentica-broker"


def _instance(
    tmp_path: Path, public_pem: bytes, tenant: str = "tenant-a"
) -> SenticaInstance:
    public_path = tmp_path / "token.pub"
    public_path.write_bytes(public_pem)
    loaded = load_pem_public_key(public_pem)
    assert isinstance(loaded, ed25519.Ed25519PublicKey)
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    ca = tmp_path / "ca.crt"
    for path in (cert, key, ca):
        path.write_text("x\n", encoding="utf-8")
    return SenticaInstance(
        tenant_id=tenant,
        proxy_host="pgbouncer",
        proxy_port=6432,
        db_user="tenant_a",
        db_name="tenant_a",
        cert_path=cert,
        key_path=key,
        ca_path=ca,
        token_public_key_path=public_path,
        token_public_key=loaded,
        token_issuer=ISSUER,
        bind_host="127.0.0.1",
    )


def _keys() -> tuple[ed25519.Ed25519PrivateKey, bytes]:
    private = ed25519.Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public_pem


def _token(
    private: ed25519.Ed25519PrivateKey,
    *,
    tenant: str = "tenant-a",
    issuer: str = ISSUER,
    lifetime: int = 60,
    request_id: str = "req-1",
    extra: dict[str, object] | None = None,
) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": issuer,
        "iat": now,
        "exp": now + lifetime,
        "tenant_id": tenant,
        "request_id": request_id,
        "aud": TOKEN_AUDIENCE,
        "w": "general",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, private, algorithm="EdDSA")


def test_valid_token(tmp_path: Path) -> None:
    reset_refusals()
    private, public_pem = _keys()
    instance = _instance(tmp_path, public_pem)
    params = verify_sentica_token(_token(private), instance)
    assert params.w == "general"
    assert params.ad is False


def test_other_tenant_is_refused(tmp_path: Path) -> None:
    reset_refusals()
    private, public_pem = _keys()
    instance = _instance(tmp_path, public_pem, tenant="tenant-a")
    with pytest.raises(AuthenticationException, match="wrong tenant"):
        verify_sentica_token(_token(private, tenant="tenant-b"), instance)
    assert refusal_count("wrong_tenant") == 1


def test_expired_forged_and_wrong_issuer(tmp_path: Path) -> None:
    reset_refusals()
    private, public_pem = _keys()
    instance = _instance(tmp_path, public_pem)
    with pytest.raises(AuthenticationException, match="expired"):
        verify_sentica_token(_token(private, lifetime=-10), instance)
    other, _other_pem = _keys()
    with pytest.raises(AuthenticationException, match="forged"):
        verify_sentica_token(_token(other), instance)
    with pytest.raises(AuthenticationException, match="wrong issuer"):
        verify_sentica_token(_token(private, issuer="someone-else"), instance)
    with pytest.raises(AuthenticationException, match="5 minutes"):
        verify_sentica_token(_token(private, lifetime=301), instance)
    with pytest.raises(AuthenticationException, match="admin"):
        verify_sentica_token(_token(private, extra={"ad": True}), instance)
    assert refusal_count("expired") == 1
    assert refusal_count("forged") == 1
    assert refusal_count("wrong_issuer") == 1
    assert refusal_count("lifetime") == 1
    assert refusal_count("admin_disabled") == 1


def test_deriver_task_for_other_tenant_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    public_pem = _keys()[1]
    instance = _instance(tmp_path, public_pem)
    reset_refusals()
    monkeypatch.setenv("SENTICA_MODE", "production")
    monkeypatch.setattr("src.sentica.queue.require_instance", lambda: instance)
    payload = {
        "task_type": "representation",
        "content": "hidden",
        "tenant_id": "tenant-b",
    }
    with pytest.raises(SenticaTenantRefused):
        pop_and_check_tenant(payload)
    assert "content" in payload
    assert "tenant_id" not in payload
    assert refusal_count("wrong_tenant") == 1


def test_deriver_task_without_tenant_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reset_refusals()
    public_pem = _keys()[1]
    instance = _instance(tmp_path, public_pem)
    monkeypatch.setenv("SENTICA_MODE", "production")
    monkeypatch.setattr("src.sentica.queue.require_instance", lambda: instance)
    with pytest.raises(SenticaTenantRefused):
        pop_and_check_tenant({"task_type": "dream"})
    assert refusal_count("missing_tenant") == 1


def _none_token(payload: dict[str, object]) -> str:
    import base64
    import json

    def _part(value: dict[str, object]) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        return raw.rstrip("=")

    return _part({"alg": "none", "typ": "JWT"}) + "." + _part(payload) + "."


def test_none_hs256_and_algorithm_mismatch_are_forged(tmp_path: Path) -> None:
    reset_refusals()
    private, public_pem = _keys()
    del private
    instance = _instance(tmp_path, public_pem)
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": ISSUER,
        "iat": now,
        "exp": now + 60,
        "tenant_id": "tenant-a",
        "request_id": "req-alg",
        "aud": TOKEN_AUDIENCE,
    }
    hs256 = jwt.encode(payload, "secret", algorithm="HS256")
    assert isinstance(hs256, str)
    with pytest.raises(AuthenticationException, match="forged"):
        verify_sentica_token(hs256, instance)
    with pytest.raises(AuthenticationException, match="forged"):
        verify_sentica_token(_none_token(payload), instance)
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    mismatched = jwt.encode(payload, rsa_key, algorithm="RS256")
    assert isinstance(mismatched, str)
    with pytest.raises(AuthenticationException, match="forged"):
        verify_sentica_token(mismatched, instance)
    missing_aud = dict(payload)
    missing_aud.pop("aud")
    ed = _keys()[0]
    without_aud = jwt.encode(missing_aud, ed, algorithm="EdDSA")
    assert isinstance(without_aud, str)
    with pytest.raises(AuthenticationException, match="forged"):
        verify_sentica_token(without_aud, instance)
    assert refusal_count("forged") == 4
