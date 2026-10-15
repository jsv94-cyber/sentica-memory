"""Verify broker-signed tokens. The instance holds only the public key."""

from __future__ import annotations

import datetime
from typing import NoReturn

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from src.exceptions import AuthenticationException
from src.security import JWTParams

from .refusals import record_refusal
from .runtime import (
    TOKEN_AUDIENCE,
    SenticaInstance,
    production_enabled,
    require_instance,
)

_MAX_LIFETIME = datetime.timedelta(minutes=5)


def _refuse(reason: str, detail: str) -> NoReturn:
    record_refusal(reason)
    raise AuthenticationException(detail)


def _algorithm(key: Ed25519PublicKey | RSAPublicKey) -> str:
    """Use the key type. The token header is not trusted."""
    if isinstance(key, Ed25519PublicKey):
        return "EdDSA"
    return "RS256"


def verify_sentica_token(
    token: str, instance: SenticaInstance | None = None
) -> JWTParams:
    """Verify a broker token against this instance's tenant.

    Args:
        token: Compact JWT. Not logged.
        instance: Override for tests. Production reads the process config.

    Returns:
        Decoded Honcho scope plus the checked tenant.

    Raises:
        SenticaTokenError: The token is missing, expired, forged, or for
            another tenant or issuer.
    """
    current = instance if instance is not None else require_instance()
    if not token or not token.strip():
        _refuse("missing_token", "missing token")

    key = current.token_public_key
    try:
        decoded = jwt.decode(
            token,
            key,
            algorithms=[_algorithm(key)],
            issuer=current.token_issuer,
            audience=TOKEN_AUDIENCE,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        record_refusal("expired")
        raise AuthenticationException("expired token") from exc
    except jwt.InvalidIssuerError as exc:
        record_refusal("wrong_issuer")
        raise AuthenticationException("wrong issuer") from exc
    except Exception as exc:
        record_refusal("forged")
        raise AuthenticationException("forged token") from exc

    if decoded.get("ad"):
        _refuse("admin_disabled", "admin tokens are disabled")

    iat = decoded.get("iat")
    exp = decoded.get("exp")
    if not isinstance(iat, int | float) or not isinstance(exp, int | float):
        _refuse("forged", "forged token")
    if exp - iat > _MAX_LIFETIME.total_seconds():
        _refuse("lifetime", "token lifetime exceeds 5 minutes")

    request_id = decoded.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        _refuse("missing_request_id", "missing request id")

    tenant = decoded.get("tenant_id")
    if not isinstance(tenant, str) or not tenant.strip():
        _refuse("missing_tenant", "missing tenant")
    if tenant != current.tenant_id:
        _refuse("wrong_tenant", "wrong tenant")

    params = JWTParams(t=str(int(iat)))
    workspace = decoded.get("w")
    peer = decoded.get("p")
    session = decoded.get("s")
    params.w = workspace or None
    params.p = peer or None
    params.s = session or None
    params.ad = False
    from src.sentica.workspace_scope import allowed_from_claims

    params.workspaces = sorted(allowed_from_claims(params.w, decoded.get("workspaces")))
    return params


def production_token_required() -> bool:
    """Return whether request auth must use the broker public key."""
    return production_enabled()
