"""Production configuration. Missing or unknown values refuse startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

PRODUCTION_MODE = "production"

# Names only. Values are read from the environment and never logged.
_REQUIRED = (
    "SENTICA_TENANT_ID",
    "SENTICA_DB_PROXY_HOST",
    "SENTICA_DB_PROXY_PORT",
    "SENTICA_DB_USER",
    "SENTICA_DB_NAME",
    "SENTICA_CLIENT_CERT_PATH",
    "SENTICA_CLIENT_KEY_PATH",
    "SENTICA_PROXY_CA_PATH",
    "SENTICA_TOKEN_PUBLIC_KEY_PATH",
    "SENTICA_TOKEN_ISSUER",
    "SENTICA_BIND_HOST",
)

# A password in any of these means the instance is holding one.
_PASSWORD_ENV = (
    "DB_CONNECTION_URI",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "DB_PASSWORD",
    "PGPASSWORD",
)

_PUBLIC_BIND = frozenset({"0.0.0.0", "::", ""})


class SenticaStartupError(RuntimeError):
    """The process must exit. The message names settings, not secret values."""


@dataclass(frozen=True, slots=True)
class SenticaInstance:
    """The one tenant this process is allowed to serve."""

    tenant_id: str
    proxy_host: str
    proxy_port: int
    db_user: str
    db_name: str
    cert_path: Path
    key_path: Path
    ca_path: Path
    token_public_key_path: Path
    token_issuer: str
    bind_host: str

    def connection_uri(self) -> str:
        """Passwordless SQLAlchemy URI aimed at the proxy."""
        user = quote(self.db_user, safe="")
        host = self.proxy_host
        return f"postgresql+psycopg://{user}@{host}:{self.proxy_port}/{self.db_name}"


def mode() -> str | None:
    """Return the sentica mode, or None when this process is the upstream suite."""
    raw = os.environ.get("SENTICA_MODE", "").strip()
    return raw or None


def production_enabled() -> bool:
    """Return whether this process is a production single-tenant instance."""
    return mode() == PRODUCTION_MODE


def load_instance() -> SenticaInstance | None:
    """Load production config, or return None when sentica mode is unset.

    Raises:
        SenticaStartupError: Mode is set but not production, or a required
            value is missing, unknown, or a database password is present.
    """
    selected = mode()
    if selected is None:
        return None
    if selected != PRODUCTION_MODE:
        message = (
            f"Unknown SENTICA_MODE {selected!r}. "
            "The only accepted value is 'production'."
        )
        raise SenticaStartupError(message)

    missing = [name for name in _REQUIRED if not os.environ.get(name, "").strip()]
    present_passwords = [
        name for name in _PASSWORD_ENV if os.environ.get(name, "").strip()
    ]
    if missing or present_passwords:
        parts: list[str] = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if present_passwords:
            parts.append("database password is set via " + ", ".join(present_passwords))
        raise SenticaStartupError("Refusing to start: " + "; ".join(parts))

    bind_host = os.environ["SENTICA_BIND_HOST"].strip()
    if bind_host in _PUBLIC_BIND:
        raise SenticaStartupError(
            "Refusing to start: SENTICA_BIND_HOST must be a private interface"
        )

    try:
        proxy_port = int(os.environ["SENTICA_DB_PROXY_PORT"].strip())
    except ValueError as exc:
        raise SenticaStartupError(
            "Refusing to start: SENTICA_DB_PROXY_PORT is not an integer"
        ) from exc
    if proxy_port <= 0 or proxy_port > 65535:
        raise SenticaStartupError(
            "Refusing to start: SENTICA_DB_PROXY_PORT is out of range"
        )

    instance = SenticaInstance(
        tenant_id=os.environ["SENTICA_TENANT_ID"].strip(),
        proxy_host=os.environ["SENTICA_DB_PROXY_HOST"].strip(),
        proxy_port=proxy_port,
        db_user=os.environ["SENTICA_DB_USER"].strip(),
        db_name=os.environ["SENTICA_DB_NAME"].strip(),
        cert_path=Path(os.environ["SENTICA_CLIENT_CERT_PATH"].strip()),
        key_path=Path(os.environ["SENTICA_CLIENT_KEY_PATH"].strip()),
        ca_path=Path(os.environ["SENTICA_PROXY_CA_PATH"].strip()),
        token_public_key_path=Path(os.environ["SENTICA_TOKEN_PUBLIC_KEY_PATH"].strip()),
        token_issuer=os.environ["SENTICA_TOKEN_ISSUER"].strip(),
        bind_host=bind_host,
    )
    _require_files(instance)
    return instance


def _require_files(instance: SenticaInstance) -> None:
    for label, path in (
        ("SENTICA_CLIENT_CERT_PATH", instance.cert_path),
        ("SENTICA_CLIENT_KEY_PATH", instance.key_path),
        ("SENTICA_PROXY_CA_PATH", instance.ca_path),
        ("SENTICA_TOKEN_PUBLIC_KEY_PATH", instance.token_public_key_path),
    ):
        if not path.is_file() or path.stat().st_size == 0:
            raise SenticaStartupError(f"Refusing to start: {label} is missing or empty")


def require_instance() -> SenticaInstance:
    """Return the production instance or raise if this process is not one."""
    instance = load_instance()
    if instance is None:
        raise SenticaStartupError("Sentica production mode is not enabled")
    return instance
