"""Packaging checks that do not need the two-instance stack."""

from __future__ import annotations

import importlib.util
import ipaddress
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from src.sentica.ops_metrics import (
    decayed,
    filtered,
    note_decay,
    note_prune,
    pruned,
    reset_ops,
)
from src.sentica.safety import note_drop, reset_drops

ROOT = Path(__file__).resolve().parents[2]

_SENTICA_SETTINGS = (
    "SENTICA_MODE",
    "SENTICA_TENANT_ID",
    "SENTICA_DB_PROXY_HOST",
    "SENTICA_DB_PROXY_PORT",
    "SENTICA_DB_USER",
    "SENTICA_DB_NAME",
    "SENTICA_DB_CALL_TIMEOUT_SECONDS",
    "SENTICA_CLIENT_CERT_PATH",
    "SENTICA_CLIENT_KEY_PATH",
    "SENTICA_PROXY_CA_PATH",
    "SENTICA_TOKEN_PUBLIC_KEY_PATH",
    "SENTICA_TOKEN_ISSUER",
    "SENTICA_TOKEN_SIGNING_KEY",
    "SENTICA_TENANT_HASH_KEY",
    "SENTICA_TENANT_HASH_KEY_PATH",
    "SENTICA_BIND_HOST",
    "SENTICA_REDIS_HOST",
    "SENTICA_MIGRATION_TIMEOUT_SECONDS",
    "SENTICA_DECAY_INTERVAL_DAYS",
    "SENTICA_DECAY_FLOOR",
    "SENTICA_DECAY_RETENTION_DAYS",
    "SENTICA_SAFETY_MODEL_CHECK",
    "SENTICA_GATEWAY_MODE",
    "SENTICA_GATEWAY_BASE_URL",
    "SENTICA_GATEWAY_DERIVER_BASE_URL",
    "SENTICA_GATEWAY_SUMMARY_BASE_URL",
    "SENTICA_GATEWAY_DIALECTIC_BASE_URL",
    "SENTICA_GATEWAY_DREAM_BASE_URL",
    "SENTICA_GATEWAY_EMBED_BASE_URL",
    "SENTICA_GATEWAY_DERIVER_MODEL",
    "SENTICA_GATEWAY_SUMMARY_MODEL",
    "SENTICA_GATEWAY_DIALECTIC_MODEL",
    "SENTICA_GATEWAY_DREAM_MODEL",
    "SENTICA_GATEWAY_EMBED_MODEL",
    "SENTICA_GATEWAY_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_DERIVER_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_SUMMARY_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_DIALECTIC_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_DREAM_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_EMBED_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_REDIS_URL",
    "SENTICA_GATEWAY_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_DERIVER_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_SUMMARY_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_DIALECTIC_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_DREAM_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_EMBED_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_RETRY_ATTEMPTS",
    "SENTICA_GATEWAY_RETRY_WAIT_SECONDS",
    "SENTICA_GATEWAY_TOKENIZER",
    "SENTICA_GATEWAY_TOKENIZER_ALIASES",
    "SENTICA_API_PORT",
    "SENTICA_POSTGRES_PORT",
    "SENTICA_REDIS_PORT",
    "SENTICA_API_A_PORT",
    "SENTICA_API_B_PORT",
    "SENTICA_PROXY_A_PORT",
    "SENTICA_PROXY_B_PORT",
    "SENTICA_UID",
    "SENTICA_GID",
    "SENTICA_BAKE_PRODUCTION_MARKER",
    "SENTICA_RLS_JOB",
    "SENTICA_RLS_ADMIN_DSN",
)


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_config_lists_every_sentica_setting() -> None:
    config = _text("docs/CONFIG.md")
    missing = [name for name in _SENTICA_SETTINGS if name not in config]
    assert missing == []


def test_deploy_flags_and_internal_networks() -> None:
    deploy = _text("docs/DEPLOY.md")
    for flag in (
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        "--runtime runsc",
        "--tmpfs /tmp",
        "--user 100:100",
    ):
        assert flag in deploy
    assert "seccomp=unconfined" not in deploy
    assert "PENDING" not in deploy
    compose = _text("docker-compose.sentica.yml")
    assert "read_only: true" in compose
    assert "cap_drop:" in compose
    assert "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "internal: true" in compose
    for name in (
        "db:",
        "net-a:",
        "net-b:",
        "publish-a-api:",
        "publish-a-proxy:",
        "publish-b-api:",
        "publish-b-proxy:",
    ):
        assert name in compose
    assert 'user: "65534:65534"' in compose
    assert compose.count("8000=") == 2
    assert compose.count("6432=") == 2
    assert "publish.py" in compose
    gvisor = _text("docker-compose.gvisor.yml")
    assert "runtime: runsc" in gvisor
    assert "extra_hosts:" in gvisor
    for host_line in (
        "pgbouncer-a:172.28.10.13",
        "pgbouncer-b:172.28.11.13",
        "redis-a:172.28.10.12",
        "redis-b:172.28.11.12",
        "embeddings:172.28.10.20",
        "embeddings:172.28.11.20",
        "ipv4_address: 172.28.10.20",
        "ipv4_address: 172.28.11.20",
    ):
        assert host_line in gvisor
    for relative in (
        "docker/sentica/pgbouncer-a.ini",
        "docker/sentica/pgbouncer-b.ini",
    ):
        ini = _text(relative)
        assert "server_tls_sslmode = verify-full" in ini
        assert "server_tls_ca_file = /certs/ca.crt" in ini
    redis_entry = _text("docker/sentica/redis-entry.sh")
    assert "setpriv --reuid=redis" in redis_entry
    assert "chmod 644 /secret/password" in redis_entry
    dockerfile = _text("Dockerfile")
    assert "USER ${SENTICA_UID}:${SENTICA_GID}" in dockerfile
    assert "TIKTOKEN_CACHE_DIR=/app/tiktoken-cache" in dockerfile
    assert dockerfile.index("getent group") < dockerfile.index("addgroup")
    assert "src/sentica/eval_harness.py" in _text(".dockerignore")
    assert 'grep -R -F -q "northline violetkey"' in dockerfile
    assert "--entrypoint /app/docker/migrate-entrypoint.sh" in deploy
    assert "10.8.0.4:8000:8000" in deploy
    assert "extra_hosts" in deploy
    assert "127.0.0.11" in deploy
    assert "SENTICA_DB_PROXY_HOST" in deploy
    assert "api.openai.com" in deploy
    assert "proof-only" in deploy
    assert "65534" in deploy
    assert "docker network create" in deploy
    assert deploy.count("/run/sentica/certs/client.crt:/certs/client.crt:ro") == 3
    assert deploy.count("/run/sentica/certs/client.key:/certs/client.key:ro") == 3
    assert deploy.count("/run/sentica/certs/ca.crt:/certs/ca.crt:ro") == 4
    assert deploy.count("/run/sentica/certs/token.pub:/certs/token.pub:ro") == 3


_BROKER = ipaddress.ip_address("10.8.0.4")
_REQUIRED_CONTAINERS = (
    "sentica-memory-api",
    "sentica-memory-deriver",
    "sentica-memory-migrate",
    "sentica-memory-proxy",
    "sentica-memory-gateway",
)


def _bash_commands(deploy: str) -> list[str]:
    """Return each continued command inside a bash fence."""
    commands: list[str] = []
    for block in re.findall(r"```bash\n(.*?)```", deploy, re.DOTALL):
        pending: list[str] = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.endswith("\\"):
                pending.append(stripped[:-1].strip())
                continue
            pending.append(stripped)
            command = " ".join(part for part in pending if part)
            pending = []
            if command:
                commands.append(command)
        if pending:
            command = " ".join(part for part in pending if part)
            if command:
                commands.append(command)
    return commands


def _flag(command: str, name: str) -> str | None:
    matched = re.search(rf"(?:^|\s){re.escape(name)}(?:=|\s+)(\S+)", command)
    if matched is None:
        return None
    return matched.group(1)


def _drop_sources(command: str) -> list[ipaddress.IPv4Network]:
    """Source networks of a DROP that is not limited to one destination."""
    if not command.startswith("iptables "):
        return []
    tokens = command.split()
    if "DROP" not in tokens or "-s" not in tokens or "-d" in tokens:
        return []
    source = tokens[tokens.index("-s") + 1]
    try:
        network = ipaddress.ip_network(source, strict=False)
    except ValueError:
        return []
    if not isinstance(network, ipaddress.IPv4Network):
        return []
    return [network]


def firewall_gaps(deploy: str) -> list[str]:
    """Container addresses the DROP rules miss, and a subnet on the broker."""
    gaps: list[str] = []
    commands = _bash_commands(deploy)
    subnets: list[ipaddress.IPv4Network] = []
    for command in commands:
        if not command.startswith("docker network create"):
            continue
        raw = _flag(command, "--subnet")
        if raw is None:
            gaps.append("docker network create has no --subnet")
            continue
        try:
            subnet = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            gaps.append("docker network create subnet is not an address")
            continue
        if not isinstance(subnet, ipaddress.IPv4Network):
            gaps.append("docker network create subnet is not IPv4")
            continue
        subnets.append(subnet)
        if _BROKER in subnet:
            gaps.append("subnet " + raw + " overlaps the broker 10.8.0.4")
    drops: list[ipaddress.IPv4Network] = []
    for command in commands:
        drops.extend(_drop_sources(command))
    names: set[str] = set()
    for command in commands:
        if not command.startswith("docker run "):
            continue
        name = _flag(command, "--name")
        if name is not None:
            names.add(name)
        raw_ip = _flag(command, "--ip")
        if raw_ip is None:
            gaps.append((name or "docker run") + " has no --ip")
            continue
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError:
            gaps.append(raw_ip + " is not an address")
            continue
        if not isinstance(address, ipaddress.IPv4Address):
            gaps.append(raw_ip + " is not an IPv4 address")
            continue
        if subnets and not any(address in subnet for subnet in subnets):
            gaps.append(raw_ip + " is outside the bridge subnet")
        if not any(address in network for network in drops):
            gaps.append(raw_ip + " is not covered by a DROP rule")
    for name in _REQUIRED_CONTAINERS:
        if name not in names:
            gaps.append(name + " has no run command")
    return gaps


def test_deploy_firewall_covers_every_container_ip() -> None:
    """Every run-command address is dropped, and the bridge excludes the broker.

    A substring check for ``-j DROP`` still passed when Docker assigned an
    address the rules do not name, and when the bridge included ``10.8.0.4``.
    """
    assert firewall_gaps(_text("docs/DEPLOY.md")) == []
    open_egress = """```bash
docker network create --driver bridge --subnet 10.8.0.0/24 sentica-instance
```
```bash
docker run --name sentica-memory-api --ip 10.8.0.10 --network sentica-instance img
docker run --name sentica-memory-deriver --ip 10.8.0.11 img
docker run --name sentica-memory-migrate --network sentica-instance img
docker run --name sentica-memory-proxy --ip 10.8.0.13 img
docker run --name sentica-memory-gateway --ip 10.8.0.14 img
```
```bash
iptables -A DOCKER-USER -s 10.8.0.10/32 -j DROP
iptables -A DOCKER-USER -s 10.8.0.11/32 -d 10.8.0.13/32 -j DROP
```
"""
    gaps = firewall_gaps(open_egress)
    assert any("overlaps the broker" in item for item in gaps)
    assert any("10.8.0.13" in item for item in gaps)
    assert any("10.8.0.14" in item for item in gaps)
    assert any("sentica-memory-migrate has no --ip" in item for item in gaps)
    limited = firewall_gaps(
        """```bash
docker network create --subnet 10.8.0.8/29 sentica-instance
```
```bash
docker run --name sentica-memory-api --ip 10.8.0.10 img
docker run --name sentica-memory-deriver --ip 10.8.0.11 img
docker run --name sentica-memory-migrate --ip 10.8.0.12 img
docker run --name sentica-memory-proxy --ip 10.8.0.13 img
docker run --name sentica-memory-gateway --ip 10.8.0.14 img
```
```bash
iptables -A DOCKER-USER -s 10.8.0.10/32 -d 10.8.0.13/32 -j DROP
iptables -A DOCKER-USER -s 10.8.0.11/32 -j DROP
iptables -A DOCKER-USER -s 10.8.0.12/32 -j DROP
iptables -A DOCKER-USER -s 10.8.0.13/32 -j DROP
iptables -A DOCKER-USER -s 10.8.0.14/32 -j DROP
```
"""
    )
    assert any("10.8.0.10" in item and "DROP" in item for item in limited)


def test_dockerignore_excludes_bytecode() -> None:
    """A bare ``__pycache__`` name does not exclude nested bytecode."""
    text = _text(".dockerignore")
    assert "**/__pycache__" in text
    assert "**/*.pyc" in text


def test_fork_changes_lists_the_r3_files() -> None:
    text = _text("FORK_CHANGES.md")
    for path in (
        "src/llm/tool_loop.py",
        "migrations/versions/d4a8c1e27b90_documents_hnsw_current.py",
        "src/sentica/vector_query.py",
        "src/sentica/ready.py",
        "src/sentica/tokenizer.py",
    ):
        assert path in text, path


def test_upstream_sync_and_vulnerability_report() -> None:
    sync = _text("docs/UPSTREAM_SYNC.md")
    assert "cherry-pick" in sync
    assert "Do not merge" in sync
    security = _text("SECURITY.md")
    assert "jsv94-cyber/sentica-memory" in security
    assert "Do not open a public issue" in security
    assert "v0.1.0-sentica" in _text("docs/RELEASES.md")


def test_config_documents_simulated_streaming_and_primary_workspace() -> None:
    config = _text("docs/CONFIG.md")
    assert "simulated in v1" in config
    assert "full completion, then chunked" in config
    assert "primary workspace" in config
    assert "SENTICA_GATEWAY_TOKENIZER" in config


def test_live_eval_workflow_is_dispatch_only() -> None:
    text = _text(".github/workflows/live-eval.yml")
    assert "workflow_dispatch:" in text
    assert "pull_request" not in text
    assert (
        "\n  push:" not in text
        and "push:" not in text.split("on:", 1)[1].split("jobs:", 1)[0]
    )
    assert "secrets.SENTICA_LIVE_EVAL_GATEWAY_URL" in text
    assert "tests/sentica/live_eval_gateway.py" in text


def test_publish_forwards_exactly_one_port() -> None:
    spec = importlib.util.spec_from_file_location(
        "sentica_publish", ROOT / "docker" / "sentica" / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit):
        module.main([])
    with pytest.raises(SystemExit):
        module.main(["8000=127.0.0.1:1", "6432=127.0.0.1:2"])


def test_ready_reuses_a_cached_database_check(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two probes open the database once, and the body stays status-only."""
    from src.dependencies import tracked_db
    from src.sentica.ready import reset_ready_cache

    reset_ready_cache()
    calls = {"n": 0}

    @asynccontextmanager
    async def _counting(name: str):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        async with tracked_db(name) as db:
            yield db

    monkeypatch.setattr("src.dependencies.tracked_db", _counting)
    first = client.get("/ready")
    second = client.get("/ready")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"status": "ok"}
    assert second.json() == {"status": "ok"}
    assert set(first.json()) == {"status"}
    assert calls["n"] == 1
    reset_ready_cache()


def test_ready_answers_when_the_database_does(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    body = cast(dict[str, Any], response.json())
    assert body["status"] == "ok"


def test_filter_decay_and_prune_counters() -> None:
    reset_drops()
    reset_ops()
    note_drop("health")
    note_decay(3)
    note_prune(2)
    assert filtered("health") == 1
    assert decayed() == 3
    assert pruned() == 2
    reset_drops()
    reset_ops()
