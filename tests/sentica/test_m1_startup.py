"""M1: production mode refuses to start when configuration is incomplete."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from src.sentica.runtime import REQUIRED_SETTINGS, SenticaStartupError, load_instance

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_secret_files(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PGPASSFILE", raising=False)
    monkeypatch.delenv("SENTICA_PRODUCTION_IMAGE_MARKER", raising=False)


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
    for path in (cert, key, ca):
        path.write_text("not-used-by-this-test\n", encoding="utf-8")
    token_key = ed25519.Ed25519PrivateKey.generate()
    public.write_bytes(
        token_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
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


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "staging", "prod", "Production", "TEST", "Test"],
)
def test_rejected_modes_refuse(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("SENTICA_MODE", raising=False)
    else:
        monkeypatch.setenv("SENTICA_MODE", value)
    with pytest.raises(SenticaStartupError, match="production"):
        load_instance()


def test_test_mode_does_not_load_a_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTICA_MODE", "test")
    assert load_instance() is None


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


@pytest.mark.parametrize("name", REQUIRED_SETTINGS)
def test_each_required_setting_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    env = _production_env(tmp_path)
    env.pop(name)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(name, raising=False)
    for key in _PASSWORD_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(SenticaStartupError, match=name):
        load_instance()


def test_public_bind_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = _production_env(tmp_path, SENTICA_BIND_HOST="0.0.0.0")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in _PASSWORD_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(SenticaStartupError, match="explicit address"):
        load_instance()


@pytest.mark.parametrize("value", ["private", "localhost"])
def test_bind_host_must_be_an_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    env = _production_env(tmp_path, SENTICA_BIND_HOST=value or "127.0.0.1")
    if value == "":
        env["SENTICA_BIND_HOST"] = "   "
    else:
        env["SENTICA_BIND_HOST"] = value
    for key, item in env.items():
        monkeypatch.setenv(key, item)
    for key in _PASSWORD_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(SenticaStartupError, match="explicit address"):
        load_instance()


@pytest.mark.parametrize(
    "value",
    [None, "", "staging", "Production"],
)
def test_import_refuses_bad_mode(value: str | None) -> None:
    env = os.environ.copy()
    if value is None:
        env.pop("SENTICA_MODE", None)
    else:
        env["SENTICA_MODE"] = value
    completed = subprocess.run(
        [sys.executable, "-c", "import src.db"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    combined = completed.stderr + completed.stdout
    assert "SENTICA_MODE" in combined
    assert "production" in combined


def test_dockerfile_defaults_to_production() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV SENTICA_MODE=production" in text
    assert 'ENTRYPOINT ["/app/docker/entrypoint.sh"]' in text
    assert "SENTICA_BAKE_PRODUCTION_MARKER=true" in text
    assert not any(line.startswith("CMD ") for line in text.splitlines())
    assert "config.toml" in (ROOT / ".dockerignore").read_text(encoding="utf-8")


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


def _apply(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in _PASSWORD_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_production_image_refuses_test_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "production-image"
    marker.write_text("production\n", encoding="utf-8")
    monkeypatch.setenv("SENTICA_PRODUCTION_IMAGE_MARKER", str(marker))
    monkeypatch.setenv("SENTICA_MODE", "test")
    with pytest.raises(SenticaStartupError, match="production image"):
        load_instance()


def test_config_toml_password_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _apply(monkeypatch, _production_env(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[db]\nCONNECTION_URI = "postgresql+psycopg://postgres:secret@localhost/db"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SenticaStartupError, match="config.toml"):
        load_instance()


def test_pgpass_file_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _apply(monkeypatch, _production_env(tmp_path))
    pgpass = tmp_path / "pgpass"
    pgpass.write_text("*:*:*:*:secret\n", encoding="utf-8")
    monkeypatch.setenv("PGPASSFILE", str(pgpass))
    with pytest.raises(SenticaStartupError, match="pgpass"):
        load_instance()


def test_home_pgpass_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".pgpass").write_text("*:*:*:*:secret\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    _apply(monkeypatch, _production_env(tmp_path))
    with pytest.raises(SenticaStartupError, match="pgpass"):
        load_instance()


def test_malformed_token_key_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _production_env(tmp_path)
    Path(env["SENTICA_TOKEN_PUBLIC_KEY_PATH"]).write_text(
        "not a key\n", encoding="utf-8"
    )
    _apply(monkeypatch, env)
    with pytest.raises(SenticaStartupError, match="malformed"):
        load_instance()


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("DB_SQL_DEBUG", "true", "SQL debug"),
        ("COLLECT_METRICS_LOCAL", "true", "local metrics collector"),
        ("REASONING_TRACES_FILE", "/tmp/traces.jsonl", "REASONING_TRACES_FILE"),
        ("METRICS_ENABLED", "true", "workspace_name"),
    ],
)
def test_unsafe_production_settings_refuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    match: str,
) -> None:
    _apply(monkeypatch, _production_env(tmp_path))
    monkeypatch.setenv(name, value)
    with pytest.raises(SenticaStartupError, match=match):
        load_instance()
