"""Production configuration. Missing or unknown values refuse startup."""

from __future__ import annotations

import ipaddress
import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

PRODUCTION_MODE = "production"
TEST_MODE = "test"
_ACCEPTED_MODES = frozenset({PRODUCTION_MODE, TEST_MODE})
PRODUCTION_IMAGE_MARKER = "/app/.sentica-production-image"
TOKEN_AUDIENCE = "sentica-memory"

# Names only. Values are read from the environment and never logged.
REQUIRED_SETTINGS = (
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


class _CacheMiss:
    """Sentinel for a process that has not loaded the instance yet."""


_CACHE_MISS = _CacheMiss()
_cached_fingerprint: tuple[tuple[str, str | None], ...] | None = None
_cached_instance: SenticaInstance | None | _CacheMiss = _CACHE_MISS


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
    token_public_key: Ed25519PublicKey | RSAPublicKey
    token_issuer: str
    bind_host: str

    def connection_uri(self) -> str:
        """Passwordless SQLAlchemy URI aimed at the proxy."""
        user = quote(self.db_user, safe="")
        host = self.proxy_host
        return f"postgresql+psycopg://{user}@{host}:{self.proxy_port}/{self.db_name}"


def mode() -> str:
    """Return ``production`` or ``test``. Anything else refuses to start."""
    raw = os.environ.get("SENTICA_MODE")
    if raw is None or not raw.strip():
        raise SenticaStartupError(
            "Refusing to start: SENTICA_MODE must be 'production' or 'test'"
        )
    selected = raw.strip()
    if selected not in _ACCEPTED_MODES:
        message = (
            "Refusing to start: SENTICA_MODE "
            + f"{selected!r} must be 'production' or 'test'"
        )
        raise SenticaStartupError(message)
    if selected == TEST_MODE and Path(PRODUCTION_IMAGE_MARKER).is_file():
        raise SenticaStartupError(
            "Refusing to start: production image cannot run with SENTICA_MODE=test"
        )
    return selected


def production_enabled() -> bool:
    """Return whether this process is a production single-tenant instance."""
    return mode() == PRODUCTION_MODE


def _llm_api_key_env_names() -> tuple[str, ...]:
    """Every provider key the process can see, including ones added later."""
    from src.config import settings

    names = {
        "LLM_" + field_name
        for field_name in type(settings.LLM).model_fields
        if field_name.endswith("_API_KEY")
    }
    names.update(
        name
        for name in os.environ
        if name.startswith("LLM_") and name.endswith("_API_KEY")
    )
    return tuple(sorted(names))


def _fingerprint() -> tuple[tuple[str, str | None], ...]:
    from src.sentica.gateway import gateway_env_names

    names = (
        "SENTICA_MODE",
        *gateway_env_names(),
        "PGPASSFILE",
        "HOME",
        "DB_SQL_DEBUG",
        "COLLECT_METRICS_LOCAL",
        "REASONING_TRACES_FILE",
        *_llm_api_key_env_names(),
        "METRICS_ENABLED",
        "SENTICA_DB_CALL_TIMEOUT_SECONDS",
        "SENTICA_TENANT_HASH_KEY",
        "SENTICA_TENANT_HASH_KEY_PATH",
        *REQUIRED_SETTINGS,
        *_PASSWORD_ENV,
    )
    marker = Path(PRODUCTION_IMAGE_MARKER)
    try:
        marker_state = str(marker.stat().st_mtime_ns)
    except OSError:
        marker_state = None
    env = tuple((name, os.environ.get(name)) for name in names)
    return (*env, ("production_image_marker", f"{marker}:{marker_state}"))


def tenant_hash_key() -> bytes:
    """The per-tenant HMAC key. Production refuses a missing or short key.

    SENTICA_TENANT_HASH_KEY is the secret. SENTICA_TENANT_HASH_KEY_PATH is
    the broker's key file and is used when the environment value is unset.
    Test mode uses a fixed key only when neither is set.
    """
    raw = os.environ.get("SENTICA_TENANT_HASH_KEY", "").strip().encode()
    path = os.environ.get("SENTICA_TENANT_HASH_KEY_PATH", "").strip()
    if not raw and path:
        try:
            raw = Path(path).read_bytes().strip()
        except OSError as exc:
            raise SenticaStartupError(
                "Refusing to start: SENTICA_TENANT_HASH_KEY_PATH cannot be read"
            ) from exc
    if len(raw) >= 32:
        return raw
    if production_enabled() or raw:
        raise SenticaStartupError(
            "Refusing to start: SENTICA_TENANT_HASH_KEY is missing or shorter than 32 bytes"
        )
    return b"sentica-test-tenant-hash-key-32b!!"


def _require_tenant_hash_key() -> None:
    tenant_hash_key()


def db_call_timeout_seconds() -> float:
    """Return the maintenance database call bound, or refuse a bad value.

    Unset means 120 seconds. A value above 600, or a value that is not a
    positive number, refuses startup.
    """
    raw = os.environ.get("SENTICA_DB_CALL_TIMEOUT_SECONDS", "120").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise SenticaStartupError(
            "Refusing to start: SENTICA_DB_CALL_TIMEOUT_SECONDS is not a number"
        ) from exc
    if not math.isfinite(value) or value <= 0 or value > 600:
        raise SenticaStartupError(
            "Refusing to start: SENTICA_DB_CALL_TIMEOUT_SECONDS must be "
            + "greater than 0 and at most 600"
        )
    return value


def load_instance() -> SenticaInstance | None:
    """Load production config once, or return None in test mode.

    The result is cached for this process. A change to the environment,
    which tests use, loads it again.

    Raises:
        SenticaStartupError: Mode is missing or not production or test, or a
            required production value is missing or a database password is set.
    """
    global _cached_fingerprint, _cached_instance
    current = _fingerprint()
    fingerprint_hit = _cached_fingerprint == current
    if fingerprint_hit and not isinstance(_cached_instance, _CacheMiss):
        return _cached_instance
    instance = _load_instance()
    _cached_fingerprint = current
    _cached_instance = instance
    return instance


def _load_instance() -> SenticaInstance | None:
    selected = mode()
    db_call_timeout_seconds()
    from src.sentica.gateway import refuse_invalid_gateway_env

    refuse_invalid_gateway_env()
    if selected == TEST_MODE:
        return None

    missing = [
        name for name in REQUIRED_SETTINGS if not os.environ.get(name, "").strip()
    ]
    present_passwords = _password_sources()
    if missing or present_passwords:
        parts: list[str] = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if present_passwords:
            parts.append("database password is set via " + ", ".join(present_passwords))
        raise SenticaStartupError("Refusing to start: " + "; ".join(parts))

    bind_host = _explicit_bind_host(os.environ["SENTICA_BIND_HOST"])
    _refuse_unsafe_runtime()

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

    _require_tenant_hash_key()
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
        token_public_key=_load_token_key(
            Path(os.environ["SENTICA_TOKEN_PUBLIC_KEY_PATH"].strip())
        ),
        token_issuer=os.environ["SENTICA_TOKEN_ISSUER"].strip(),
        bind_host=bind_host,
    )
    _require_files(instance)
    return instance


def _explicit_bind_host(raw: str) -> str:
    bind_host = raw.strip()
    if bind_host in _PUBLIC_BIND or bind_host == "private":
        raise SenticaStartupError(
            "Refusing to start: SENTICA_BIND_HOST must be an explicit address"
        )
    try:
        address = ipaddress.ip_address(bind_host)
    except ValueError as exc:
        raise SenticaStartupError(
            "Refusing to start: SENTICA_BIND_HOST must be an explicit address"
        ) from exc
    if address.is_unspecified:
        raise SenticaStartupError(
            "Refusing to start: SENTICA_BIND_HOST must be an explicit address"
        )
    return bind_host


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _gateway_mode_on() -> bool:
    """True only for the same on-set the gateway client accepts."""
    raw = os.environ.get("SENTICA_GATEWAY_MODE")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _refuse_unsafe_runtime() -> None:
    """Refuse settings that would write memory content or names outside Postgres."""
    from src.config import settings
    from src.telemetry.prometheus import metrics as prometheus_metrics

    if not _gateway_mode_on():
        raise SenticaStartupError(
            "Refusing to start: SENTICA_GATEWAY_MODE must be explicitly on "
            + "in production"
        )
    if _truthy("DB_SQL_DEBUG") or settings.DB.SQL_DEBUG:
        raise SenticaStartupError("Refusing to start: SQL debug is enabled")
    if _truthy("COLLECT_METRICS_LOCAL") or settings.COLLECT_METRICS_LOCAL:
        raise SenticaStartupError(
            "Refusing to start: the local metrics collector is enabled"
        )
    traces = os.environ.get("REASONING_TRACES_FILE", "").strip()
    if traces or settings.REASONING_TRACES_FILE:
        raise SenticaStartupError("Refusing to start: REASONING_TRACES_FILE is set")
    configured = os.environ.get("VECTOR_STORE_TYPE")
    if configured is None or not configured.strip():
        vector_store = str(settings.VECTOR_STORE.TYPE).strip().lower()
    else:
        vector_store = configured.strip().lower()
    if vector_store != "pgvector":
        raise SenticaStartupError(
            "Refusing to start: an external vector store is not allowed in production"
        )
    for env_name in _llm_api_key_env_names():
        field_name = env_name.removeprefix("LLM_")
        value = os.environ.get(env_name)
        if value is None:
            value = getattr(settings.LLM, field_name, None)
        if value == "sentica-local-dummy":
            raise SenticaStartupError(
                "Refusing to start: " + env_name + " is the local dummy key"
            )
    metrics_on = _truthy("METRICS_ENABLED") or settings.METRICS.ENABLED
    labeled = any(
        _workspace_labeled(metric)
        for metric in (
            prometheus_metrics.messages_created_counter,
            prometheus_metrics.dialectic_calls_counter,
            prometheus_metrics.deriver_queue_items_processed_counter,
        )
    )
    if metrics_on and labeled:
        raise SenticaStartupError(
            "Refusing to start: metrics labels include workspace_name"
        )


def _workspace_labeled(metric: object) -> bool:
    names: object = getattr(metric, "_labelnames", ())
    return isinstance(names, tuple) and "workspace_name" in names


def _password_sources() -> list[str]:
    found = [name for name in _PASSWORD_ENV if os.environ.get(name, "").strip()]
    for path in _config_paths():
        if _toml_has_database_password(path):
            found.append(str(path))
    for path in _pgpass_paths():
        if _pgpass_has_secret(path):
            found.append(str(path))
    return found


def _config_paths() -> list[Path]:
    paths = [Path("config.toml"), Path("/app/config.toml")]
    return paths


def _toml_has_database_password(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return True
    db_raw: object = data.get("db")
    if not isinstance(db_raw, dict):
        return False
    db = cast(dict[str, object], db_raw)
    uri = db.get("CONNECTION_URI", db.get("connection_uri", ""))
    if isinstance(uri, str) and _uri_has_password(uri):
        return True
    for key in ("PASSWORD", "password", "POSTGRES_PASSWORD"):
        value = db.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _uri_has_password(uri: str) -> bool:
    if "://" not in uri:
        return False
    userinfo = urlsplit(uri).username
    password = urlsplit(uri).password
    del userinfo
    return bool(password)


def _pgpass_paths() -> list[Path]:
    explicit = os.environ.get("PGPASSFILE", "").strip()
    if explicit:
        return [Path(explicit)]
    return [Path.home() / ".pgpass"]


def _pgpass_has_secret(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return True
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def _load_token_key(path: Path) -> Ed25519PublicKey | RSAPublicKey:
    if not path.is_file() or path.stat().st_size == 0:
        raise SenticaStartupError(
            "Refusing to start: SENTICA_TOKEN_PUBLIC_KEY_PATH is missing or empty"
        )
    try:
        key = load_pem_public_key(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise SenticaStartupError(
            "Refusing to start: SENTICA_TOKEN_PUBLIC_KEY_PATH is malformed"
        ) from exc
    if not isinstance(key, Ed25519PublicKey | RSAPublicKey):
        raise SenticaStartupError(
            "Refusing to start: SENTICA_TOKEN_PUBLIC_KEY_PATH is malformed"
        )
    return key


def _require_files(instance: SenticaInstance) -> None:
    for label, path in (
        ("SENTICA_CLIENT_CERT_PATH", instance.cert_path),
        ("SENTICA_CLIENT_KEY_PATH", instance.key_path),
        ("SENTICA_PROXY_CA_PATH", instance.ca_path),
    ):
        if not path.is_file() or path.stat().st_size == 0:
            raise SenticaStartupError(f"Refusing to start: {label} is missing or empty")


def require_instance() -> SenticaInstance:
    """Return the production instance or raise if this process is not one."""
    instance = load_instance()
    if instance is None:
        raise SenticaStartupError("Sentica production mode is not enabled")
    return instance


def capped_pool_recycle(configured: int) -> int:
    """Retire pooled connections at least once an hour."""
    return min(int(configured), 3600)
