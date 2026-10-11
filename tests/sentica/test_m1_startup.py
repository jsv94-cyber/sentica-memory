"""M1: production mode refuses to start when configuration is incomplete."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.sentica.runtime import SenticaStartupError, load_instance

ROOT = Path(__file__).resolve().parents[2]

_PASSWORD_KEYS = (
    "DB_CONNECTION_URI",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "DB_PASSWORD",
    "PGPASSWORD",
)


def _production_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    ca = tmp_path / "ca.crt"
    public = tmp_path / "token.pub"
    for path in (cert, key, ca, public):
        path.write_text("not-used-by-this-test\n", encoding="utf-8")
    env = {
        "SENTICA_MODE": "production",
        "SENTICA_TENANT_ID": "tenant-a",
        "SENTICA_DB_PROXY_HOST": "pgbouncer",
        "SENTICA_DB_PROXY_PORT": "6432",
        "SENTICA_DB_USER": "tenant_a",
        "SENTICA_DB_NAME": "tenant_a",
        "SENTICA_CLIENT_CERT_PATH": str(cert),
        "SENTICA_CLIENT_KEY_PATH": str(key),
        "SENTICA_PROXY_CA_PATH": str(ca),
        "SENTICA_TOKEN_PUBLIC_KEY_PATH": str(public),
        "SENTICA_TOKEN_ISSUER": "sentica-broker",
        "SENTICA_BIND_HOST": "127.0.0.1",
    }
    env.update(overrides)
    return env


def test_unset_mode_is_not_a_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTICA_MODE", raising=False)
    assert load_instance() is None


def test_unknown_mode_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTICA_MODE", "staging")
    with pytest.raises(SenticaStartupError, match="Unknown SENTICA_MODE"):
        load_instance()


def test_missing_tenant_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _production_env(tmp_path)
    env.pop("SENTICA_TENANT_ID")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SENTICA_TENANT_ID", raising=False)
    for key in _PASSWORD_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SenticaStartupError, match="SENTICA_TENANT_ID"):
        load_instance()


def test_missing_certificate_file_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _production_env(tmp_path)
    missing = tmp_path / "gone.crt"
    env["SENTICA_CLIENT_CERT_PATH"] = str(missing)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in _PASSWORD_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SenticaStartupError, match="SENTICA_CLIENT_CERT_PATH"):
        load_instance()


def test_database_password_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _production_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    with pytest.raises(SenticaStartupError, match="database password"):
        load_instance()


def test_public_bind_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = _production_env(tmp_path, SENTICA_BIND_HOST="0.0.0.0")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in _PASSWORD_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SenticaStartupError, match="private interface"):
        load_instance()


def test_import_refuses_without_tenant() -> None:
    env = os.environ.copy()
    for key in _PASSWORD_KEYS:
        env.pop(key, None)
    env["SENTICA_MODE"] = "production"
    completed = subprocess.run(
        [sys.executable, "-c", "import src.db"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "SENTICA_TENANT_ID" in completed.stderr + completed.stdout
