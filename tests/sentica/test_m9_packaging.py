"""Packaging checks that do not need the two-instance stack."""

from __future__ import annotations

import importlib.util
import ipaddress
import re
import shlex
import stat
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NamedTuple, cast

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
    "sentica-memory-egress",
    "sentica-memory-postgres",
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


class _FirewallRule(NamedTuple):
    source: ipaddress.IPv4Network | None
    dest: ipaddress.IPv4Network | None
    hostname: bool
    dport: int | None
    protocol: str | None
    established: bool
    target: str


class _Tables:
    """``DOCKER-USER`` starts empty, which is how Docker 29 creates it."""

    def __init__(self) -> None:
        self.docker_user: list[_FirewallRule] = []
        self.sentica: list[_FirewallRule] | None = None
        self.extra: dict[str, list[_FirewallRule]] = {}

    def named(self, chain: str) -> list[_FirewallRule] | None:
        if chain == "DOCKER-USER":
            return self.docker_user
        if chain == "SENTICA":
            return self.sentica
        return self.extra.get(chain)

    def chains(self) -> list[list[_FirewallRule]]:
        found = [self.docker_user]
        if self.sentica is not None:
            found.append(self.sentica)
        found.extend(self.extra.values())
        return found


def _token_after(tokens: list[str], flag: str) -> str | None:
    if flag not in tokens:
        return None
    index = tokens.index(flag)
    if index + 1 >= len(tokens):
        return None
    return tokens[index + 1]


def _ipv4_network(raw: str | None) -> ipaddress.IPv4Network | None:
    if raw is None:
        return None
    try:
        network = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        return None
    if not isinstance(network, ipaddress.IPv4Network):
        return None
    return network


def _iptables_parts(command: str) -> list[str]:
    """Split ``cmd || cmd`` and drop shell redirects."""
    parts: list[str] = []
    for part in command.split("||"):
        cleaned = re.sub(r"\s*2>/dev/null\b", "", part).strip()
        if cleaned:
            parts.append(cleaned)
    return parts


def _chain_op(tokens: list[str]) -> tuple[str, str, list[str]] | None:
    for op in ("-A", "-I", "-C", "-D", "-F", "-X", "-E", "-N"):
        if op not in tokens:
            continue
        index = tokens.index(op)
        if index + 1 >= len(tokens):
            return None
        return op, tokens[index + 1], tokens[index + 2 :]
    return None


def _rule_from_rest(rest: list[str]) -> _FirewallRule | None:
    if "-j" not in rest:
        return None
    dest_token = _token_after(rest, "-d")
    dest = _ipv4_network(dest_token)
    dport_token = _token_after(rest, "--dport")
    dport = int(dport_token) if dport_token and dport_token.isdigit() else None
    protocol = _token_after(rest, "-p")
    ctstate = _token_after(rest, "--ctstate")
    return _FirewallRule(
        source=_ipv4_network(_token_after(rest, "-s")),
        dest=dest,
        hostname=dest_token is not None and dest is None,
        dport=dport,
        protocol=protocol,
        established=ctstate is not None and "ESTABLISHED" in ctstate,
        target=rest[rest.index("-j") + 1],
    )


def _parse_iptables(
    command: str,
) -> tuple[str, str, int | None, _FirewallRule | None] | None:
    """Return op, chain, insert position, and match for one iptables command."""
    tokens = command.split()
    if not tokens or tokens[0] != "iptables":
        return None
    parsed = _chain_op(tokens)
    if parsed is None:
        return None
    op, chain, rest = parsed
    position: int | None = None
    if op == "-I" and rest and rest[0].isdigit():
        position = int(rest[0])
        rest = rest[1:]
    if op in {"-N", "-F", "-X"}:
        return op, chain, position, None
    if op == "-E":
        if not rest:
            return None
        return (
            op,
            chain,
            None,
            _FirewallRule(None, None, False, None, None, False, rest[0]),
        )
    return op, chain, position, _rule_from_rest(rest)


def _apply_iptables(tables: _Tables, command: str) -> bool:
    """Apply one command. A failed check or a duplicate create returns false."""
    parsed = _parse_iptables(command)
    if parsed is None:
        return command == "true"
    op, chain, position, rule = parsed
    if op == "-N":
        if tables.named(chain) is not None:
            return False
        if chain == "SENTICA":
            tables.sentica = []
            return True
        if chain == "DOCKER-USER":
            return False
        tables.extra[chain] = []
        return True
    if op == "-X":
        if chain == "DOCKER-USER":
            return False
        current = tables.named(chain)
        referenced = any(
            item.target == chain for rules in tables.chains() for item in rules
        )
        if current is None or current or referenced:
            return False
        if chain == "SENTICA":
            tables.sentica = None
        else:
            del tables.extra[chain]
        return True
    if op == "-E":
        if rule is None or tables.named(chain) is None:
            return False
        new_name = rule.target
        if (
            new_name == chain
            or chain == "DOCKER-USER"
            or new_name == "DOCKER-USER"
            or tables.named(new_name) is not None
        ):
            return False
        moved = tables.named(chain)
        assert moved is not None
        if chain == "SENTICA":
            tables.sentica = None
        else:
            del tables.extra[chain]
        if new_name == "SENTICA":
            tables.sentica = moved
        else:
            tables.extra[new_name] = moved
        for rules in tables.chains():
            for index, item in enumerate(rules):
                if item.target == chain:
                    rules[index] = item._replace(target=new_name)
        return True
    current = tables.named(chain)
    if current is None:
        return False
    if op == "-F":
        current.clear()
        return True
    if rule is None:
        return False
    if op == "-C":
        return rule in current
    if op == "-D":
        try:
            current.remove(rule)
        except ValueError:
            return False
        return True
    if op == "-A":
        current.append(rule)
        return True
    insert_at = 0 if position is None else position - 1
    if insert_at < 0:
        insert_at = 0
    if insert_at > len(current):
        insert_at = len(current)
    current.insert(insert_at, rule)
    return True


def _apply_line(tables: _Tables, command: str) -> None:
    parts = _iptables_parts(command)
    if not parts:
        return
    if _apply_iptables(tables, parts[0]):
        return
    for part in parts[1:]:
        if _apply_iptables(tables, part):
            return


def _apply_tables(commands: list[str], tables: _Tables | None = None) -> _Tables:
    """Run the documented commands. Docker 29 starts ``DOCKER-USER`` empty."""
    state = tables if tables is not None else _Tables()
    for command in commands:
        _apply_line(state, command)
    return state


def _clone_tables(tables: _Tables) -> _Tables:
    clone = _Tables()
    clone.docker_user = list(tables.docker_user)
    if tables.sentica is not None:
        clone.sentica = list(tables.sentica)
    clone.extra = {name: list(rules) for name, rules in tables.extra.items()}
    return clone


def idempotent_gaps(commands: list[str]) -> list[str]:
    """A second run must leave the same rules.

    ``-I`` at a fixed position inserts another copy. Flushing ``SENTICA``
    and checking the jump with ``-C`` before ``-I`` does not.
    """
    once = _apply_tables(commands)
    twice = _apply_tables(commands, _clone_tables(once))
    if (
        once.docker_user != twice.docker_user
        or once.sentica != twice.sentica
        or once.extra != twice.extra
    ):
        return ["firewall rules are not idempotent"]
    return []


def _named_container_ips(
    commands: list[str],
) -> dict[str, ipaddress.IPv4Address]:
    found: dict[str, ipaddress.IPv4Address] = {}
    for command in commands:
        if not command.startswith("docker run "):
            continue
        name = _flag(command, "--name")
        raw = _flag(command, "--ip")
        address = _ipv4_network(raw)
        if name is None or address is None or address.prefixlen != 32:
            continue
        found[name] = address.network_address
    return found


def _rule_matches(
    rule: _FirewallRule,
    src: ipaddress.IPv4Address,
    dst: ipaddress.IPv4Address,
    dport: int,
    *,
    established: bool,
    protocol: str,
) -> bool:
    if rule.established and not established:
        return False
    if rule.protocol is not None and rule.protocol != protocol:
        return False
    if rule.source is not None and src not in rule.source:
        return False
    if rule.hostname:
        return False
    if rule.dest is not None and dst not in rule.dest:
        return False
    return rule.dport is None or rule.dport == dport


def _first_target(
    chain: list[_FirewallRule],
    src: ipaddress.IPv4Address,
    dst: ipaddress.IPv4Address,
    dport: int,
    protocol: str,
) -> str | None:
    for rule in chain:
        if _rule_matches(rule, src, dst, dport, established=False, protocol=protocol):
            return rule.target
    return None


def _forward_verdict(
    tables: _Tables,
    src: ipaddress.IPv4Address,
    dst: ipaddress.IPv4Address,
    dport: int,
    protocol: str = "tcp",
) -> str:
    """Verdict for a forwarded packet.

    Docker 29 leaves ``DOCKER-USER`` empty, so a packet that matches nothing
    returns to ``FORWARD`` and Docker accepts it. A ``RETURN`` inside
    ``SENTICA`` comes back to ``DOCKER-USER``. ``DROP`` stops the packet.
    """
    allowed = False
    for rule in tables.docker_user:
        if not _rule_matches(
            rule, src, dst, dport, established=False, protocol=protocol
        ):
            continue
        if rule.target == "SENTICA" and tables.sentica is not None:
            inner = _first_target(tables.sentica, src, dst, dport, protocol)
            if inner == "DROP":
                return "DROP"
            if inner == "RETURN":
                allowed = True
            continue
        return rule.target
    if allowed:
        return "RETURN"
    return "FORWARD"


_ALLOWS = (
    ("sentica-memory-api", "sentica-memory-proxy", 6432),
    ("sentica-memory-deriver", "sentica-memory-proxy", 6432),
    ("sentica-memory-migrate", "sentica-memory-proxy", 6432),
    ("sentica-memory-api", "sentica-memory-gateway", 443),
    ("sentica-memory-deriver", "sentica-memory-gateway", 443),
)


def docker_user_order_gaps(deploy: str) -> list[str]:
    """Forwarded traffic the rules do not drop, and an allow that does not match.

    Docker 29 creates ``DOCKER-USER`` empty. A packet that matches nothing
    returns to ``FORWARD``, which Docker accepts. ``-I`` without a position
    inserts at the head and reverses an allow-then-drop list. A missing
    container name is a gap. Two containers on one bridge are not forwarded,
    so an allow between them is the bridge rather than an iptables rule.
    """
    commands = _bash_commands(deploy)
    tables = _apply_tables(commands)
    gaps: list[str] = []
    public = ipaddress.IPv4Address("1.2.3.4")
    ips = _named_container_ips(commands)
    topology = _topology(commands)
    seen: set[tuple[str, ipaddress.IPv4Address]] = set()
    for name, address in ips.items():
        seen.add((name, address))
    for attachment in topology.attachments:
        seen.add((attachment.container, attachment.address))
    for name, address in seen:
        port = 80 if name == "sentica-memory-egress" else 443
        if _forward_verdict(tables, address, public, port) != "DROP":
            gaps.append(str(address) + " is not dropped")
    for source_name, dest_name, port in _ALLOWS:
        source = ips.get(source_name)
        dest = ips.get(dest_name)
        if source is None:
            gaps.append(source_name + " has no container address")
        if dest is None:
            gaps.append(dest_name + " has no container address")
        if source is None or dest is None:
            continue
        shared = topology.networks_of(source_name) & topology.networks_of(dest_name)
        if shared:
            continue
        if _forward_verdict(tables, source, dest, port) != "RETURN":
            gaps.append(source_name + " to " + dest_name + " is not allowed")
    return gaps


_PROVIDER_HOSTS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
)


def _allowlist_hosts(deploy: str) -> set[str]:
    """Hostnames in fences other than bash. iptables rules are not an allowlist."""
    hosts: set[str] = set()
    for lang, body in re.findall(r"```([a-z]*)\n(.*?)```", deploy, re.DOTALL):
        if lang == "bash":
            continue
        for line in body.splitlines():
            name = line.strip()
            if name:
                hosts.add(name)
    return hosts


def _host_addresses(deploy: str, commands: list[str]) -> set[ipaddress.IPv4Address]:
    """Addresses whose packets take ``INPUT`` instead of ``FORWARD``."""
    found = {ipaddress.IPv4Address("10.8.0.4")}
    for command in commands:
        if not command.startswith("docker network create"):
            continue
        raw = _flag(command, "--gateway")
        if raw is None:
            continue
        try:
            gateway = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if isinstance(gateway, ipaddress.IPv4Address):
            found.add(gateway)
    for match in re.finditer(
        r"`(\d+\.\d+\.\d+\.\d+)(?::\d+)?`([^\n]{0,160})",
        deploy,
    ):
        if "is on the host" not in match.group(2):
            continue
        found.add(ipaddress.IPv4Address(match.group(1)))
    return found


class _Network(NamedTuple):
    name: str
    subnet: ipaddress.IPv4Network
    gateway: ipaddress.IPv4Address | None
    internal: bool


class _Attachment(NamedTuple):
    container: str
    network: str
    address: ipaddress.IPv4Address


class _Topology:
    """Bridges and the container addresses attached to them."""

    def __init__(self) -> None:
        self.networks: dict[str, _Network] = {}
        self.attachments: list[_Attachment] = []

    def networks_of(self, container: str) -> set[str]:
        return {
            item.network for item in self.attachments if item.container == container
        }

    def networks_of_address(self, address: ipaddress.IPv4Address) -> set[str]:
        return {item.network for item in self.attachments if item.address == address}

    def addresses_of(self, container: str) -> list[ipaddress.IPv4Address]:
        found: list[ipaddress.IPv4Address] = []
        for item in self.attachments:
            if item.container == container and item.address not in found:
                found.append(item.address)
        return found

    def containers_on(self, network: str) -> set[str]:
        return {item.container for item in self.attachments if item.network == network}


def _ipv4_address(raw: str | None) -> ipaddress.IPv4Address | None:
    if raw is None:
        return None
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if not isinstance(parsed, ipaddress.IPv4Address):
        return None
    return parsed


def _parse_network_create(command: str) -> _Network | None:
    if not command.startswith("docker network create "):
        return None
    tokens = command.split()
    name = tokens[-1]
    if name.startswith("-"):
        return None
    raw = _flag(command, "--subnet")
    if raw is None:
        return None
    try:
        subnet = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        return None
    if not isinstance(subnet, ipaddress.IPv4Network):
        return None
    return _Network(
        name,
        subnet,
        _ipv4_address(_flag(command, "--gateway")),
        "--internal" in tokens,
    )


def _parse_run_attachment(command: str) -> _Attachment | None:
    if not command.startswith("docker run "):
        return None
    name = _flag(command, "--name")
    network = _flag(command, "--network")
    address = _ipv4_address(_flag(command, "--ip"))
    if name is None or network is None or address is None:
        return None
    return _Attachment(name, network, address)


def _parse_connect(command: str) -> _Attachment | None:
    if not command.startswith("docker network connect "):
        return None
    raw_ip: str | None = None
    positional: list[str] = []
    rest = command.split()[3:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--ip" and index + 1 < len(rest):
            raw_ip = rest[index + 1]
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        positional.append(token)
        index += 1
    address = _ipv4_address(raw_ip)
    if address is None or len(positional) < 2:
        return None
    return _Attachment(positional[1], positional[0], address)


def _topology(commands: list[str]) -> _Topology:
    state = _Topology()
    for command in commands:
        created = _parse_network_create(command)
        if created is not None:
            state.networks[created.name] = created
            continue
        attached = _parse_run_attachment(command)
        if attached is None:
            attached = _parse_connect(command)
        if attached is not None:
            state.attachments.append(attached)
    return state


def _packet_path(
    src: ipaddress.IPv4Address,
    dest: ipaddress.IPv4Address,
    topology: _Topology,
    host_addrs: set[ipaddress.IPv4Address],
) -> str:
    """Same-bridge traffic is switched and does not enter ``FORWARD``.

    Docker 29 does not load br_netfilter, so ``DOCKER-USER`` never sees
    two containers on one bridge. A destination on the host uses ``INPUT``.
    A destination on a different bridge is isolated. Any other destination
    is routed and uses ``FORWARD``.
    """
    if dest in host_addrs:
        return "INPUT"
    src_nets = topology.networks_of_address(src)
    dest_nets = topology.networks_of_address(dest)
    if src_nets and dest_nets and (src_nets & dest_nets):
        return "BRIDGE"
    if dest_nets:
        return "UNREACHABLE"
    return "FORWARD"


_PROXY_BLOCKED = (
    "sentica-memory-api",
    "sentica-memory-deriver",
    "sentica-memory-migrate",
    "sentica-memory-proxy",
)

_DB_BLOCKED = (
    "sentica-memory-api",
    "sentica-memory-deriver",
    "sentica-memory-migrate",
    "sentica-memory-gateway",
    "sentica-memory-egress",
)


def _container_path(
    topology: _Topology,
    container: str,
    dest: ipaddress.IPv4Address,
    host_addrs: set[ipaddress.IPv4Address],
) -> str:
    """How ``container`` can reach ``dest``, from any of its addresses."""
    addresses = topology.addresses_of(container)
    if not addresses:
        return "FORWARD"
    paths = [_packet_path(src, dest, topology, host_addrs) for src in addresses]
    if "BRIDGE" in paths:
        return "BRIDGE"
    if "FORWARD" in paths:
        return "FORWARD"
    if "INPUT" in paths:
        return "INPUT"
    return "UNREACHABLE"


def _egress_topology_gaps(
    topology: _Topology,
    listen: ipaddress.IPv4Address,
    host_addrs: set[ipaddress.IPv4Address],
) -> list[str]:
    """Who can open the proxy, from the bridges rather than iptables."""
    gaps: list[str] = []
    proxy_name = "sentica-memory-egress"
    destinations = topology.addresses_of(proxy_name)
    if listen not in destinations:
        destinations = [listen, *destinations]
    for name in _PROXY_BLOCKED:
        path = "UNREACHABLE"
        for dest in destinations:
            candidate = _container_path(topology, name, dest, host_addrs)
            if candidate == "BRIDGE":
                path = "BRIDGE"
                break
            if candidate == "FORWARD":
                path = "FORWARD"
        if path == "BRIDGE":
            gaps.append(
                name
                + " can open the egress proxy because same-bridge traffic"
                + " is not forwarded"
            )
        elif path == "FORWARD":
            gaps.append(name + " can route to the egress proxy")
    gateway_path = _container_path(
        topology, "sentica-memory-gateway", listen, host_addrs
    )
    if gateway_path != "BRIDGE":
        gaps.append("gateway does not share a bridge with the egress proxy")
    listen_nets = topology.networks_of_address(listen)
    if not listen_nets:
        gaps.append("egress proxy listen address is not on a bridge")
        return gaps
    for net_name in sorted(listen_nets):
        net = topology.networks.get(net_name)
        if net is None or not net.internal:
            gaps.append("egress bridge is not internal")
        members = topology.containers_on(net_name)
        if members != {"sentica-memory-gateway", proxy_name}:
            gaps.append("egress bridge is not limited to the gateway and the proxy")
    wan = [
        name
        for name in topology.networks_of(proxy_name)
        if name in topology.networks and not topology.networks[name].internal
    ]
    if not wan:
        gaps.append("egress proxy has no path to the internet")
    for name in wan:
        if topology.containers_on(name) != {proxy_name}:
            gaps.append(name + " is not limited to the egress proxy")
    return gaps


def provider_egress_gaps(deploy: str) -> list[str]:
    """Hostname iptables destinations, and a proxy other containers can open.

    ``iptables -d`` with a hostname resolves that name when the rule is
    inserted and keeps the address. Provider names belong on the egress
    proxy allowlist, which is checked on each connection. A proxy address
    on the host is reached through ``INPUT``. A proxy on the same bridge
    as another container is switched, so a ``DOCKER-USER`` rule does not
    decide who may open it.
    """
    gaps: list[str] = []
    commands = _bash_commands(deploy)
    for command in commands:
        for part in _iptables_parts(command):
            parsed = _parse_iptables(part)
            if parsed is None or parsed[3] is None:
                continue
            if parsed[3].hostname:
                gaps.append(
                    "iptables destination is resolved when the rule is inserted"
                )
    allowlist = _allowlist_hosts(deploy)
    for host in _PROVIDER_HOSTS:
        if host not in allowlist:
            gaps.append(host + " is missing from the egress proxy allowlist")
    matched = re.search(
        r"egress proxy listens on `(\d+\.\d+\.\d+\.\d+):(\d+)`",
        deploy,
    )
    if matched is None:
        gaps.append("egress proxy address is not documented")
        return gaps
    proxy = ipaddress.ip_address(matched.group(1))
    if not isinstance(proxy, ipaddress.IPv4Address):
        gaps.append("egress proxy address is not IPv4")
        return gaps
    ips = _named_container_ips(commands)
    gateway = ips.get("sentica-memory-gateway")
    egress = ips.get("sentica-memory-egress")
    if gateway is None:
        gaps.append("sentica-memory-gateway has no --ip")
    if egress is None:
        gaps.append("sentica-memory-egress has no --ip")
    host_addrs = _host_addresses(deploy, commands)
    topology = _topology(commands)
    src = gateway if gateway is not None else proxy
    if _packet_path(src, proxy, topology, host_addrs) == "INPUT":
        gaps.append(
            "egress proxy is on the host, so traffic uses INPUT and "
            + "DOCKER-USER does not apply"
        )
        return gaps
    if egress is None or gateway is None:
        return gaps
    if proxy != egress:
        gaps.append("egress proxy listen address is not the container address")
        return gaps
    gaps.extend(_egress_topology_gaps(topology, proxy, host_addrs))
    return gaps


def database_path_gaps(deploy: str) -> list[str]:
    """The certificate proxy reaches Postgres on a shared internal bridge.

    An iptables RETURN for port 5432 does not attach the proxy to that
    bridge. Docker 29 would not apply it to same-bridge packets either.
    """
    commands = _bash_commands(deploy)
    topology = _topology(commands)
    gaps: list[str] = []
    shared = topology.networks_of("sentica-memory-proxy") & topology.networks_of(
        "sentica-memory-postgres"
    )
    if not shared:
        gaps.append("certificate proxy does not share the database bridge")
    for name in sorted(shared):
        net = topology.networks.get(name)
        if net is None or not net.internal:
            gaps.append("database bridge is not internal")
        members = topology.containers_on(name)
        if members != {"sentica-memory-proxy", "sentica-memory-postgres"}:
            gaps.append(name + " is not limited to the certificate proxy and postgres")
    db_nets = topology.networks_of("sentica-memory-postgres")
    for name in _DB_BLOCKED:
        if topology.networks_of(name) & db_nets:
            gaps.append(name + " shares the database bridge")
    for command in commands:
        if "--dport 5432" in command and "RETURN" in command:
            gaps.append("postgres allow is an iptables rule on a bridged flow")
    return gaps


def certificate_bridge_gaps(deploy: str) -> list[str]:
    """The certificate proxy is not on a bridge with the gateway.

    The API, deriver, and migrate reach it on one internal bridge. The
    gateway does not join that bridge, so it has no route to the proxy.
    """
    commands = _bash_commands(deploy)
    topology = _topology(commands)
    gaps: list[str] = []
    proxy = "sentica-memory-proxy"
    gateway = "sentica-memory-gateway"
    shared = topology.networks_of(proxy) & topology.networks_of(gateway)
    if shared:
        gaps.append("certificate proxy shares a bridge with the gateway")
    clients = [name for name in topology.networks_of(proxy) if name != "sentica-db"]
    if len(clients) != 1:
        gaps.append("certificate proxy has no single client bridge")
        return gaps
    name = clients[0]
    net = topology.networks.get(name)
    if net is None or not net.internal:
        gaps.append("certificate proxy client bridge is not internal")
    members = topology.containers_on(name)
    expected = {
        "sentica-memory-api",
        "sentica-memory-deriver",
        "sentica-memory-migrate",
        "sentica-memory-proxy",
    }
    if members != expected:
        gaps.append("certificate proxy client bridge has the wrong members")
    return gaps


def _spare_address_gap(
    subnet: ipaddress.IPv4Network,
    gateway: ipaddress.IPv4Address | None,
    assigned: list[ipaddress.IPv4Address],
) -> str | None:
    """A bridge with no host left after the gateway and the containers."""
    hosts = list(subnet.hosts())
    if not hosts:
        return "bridge subnet has no spare address"
    bridge = gateway if gateway is not None else hosts[0]
    if bridge not in subnet:
        return "bridge gateway is outside the subnet"
    spare = [host for host in hosts if host != bridge and host not in assigned]
    if not spare:
        return "bridge subnet has no spare address"
    return None


def firewall_gaps(deploy: str) -> list[str]:
    """Container addresses the DROP rules miss, and a subnet on the broker."""
    gaps: list[str] = []
    commands = _bash_commands(deploy)
    subnets: list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address | None]] = []
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
        gateway: ipaddress.IPv4Address | None = None
        raw_gateway = _flag(command, "--gateway")
        if raw_gateway is not None:
            try:
                parsed_gateway = ipaddress.ip_address(raw_gateway)
            except ValueError:
                gaps.append("bridge gateway is not an address")
                parsed_gateway = None
            if isinstance(parsed_gateway, ipaddress.IPv4Address):
                gateway = parsed_gateway
        subnets.append((subnet, gateway))
        if _BROKER in subnet:
            gaps.append("subnet " + raw + " overlaps the broker 10.8.0.4")
    drops: list[ipaddress.IPv4Network] = []
    for command in commands:
        drops.extend(_drop_sources(command))
    names: set[str] = set()
    assigned: list[ipaddress.IPv4Address] = []
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
        assigned.append(address)
        if subnets and not any(address in subnet for subnet, _gateway in subnets):
            gaps.append(raw_ip + " is outside the bridge subnet")
        if not any(address in network for network in drops):
            gaps.append(raw_ip + " is not covered by a DROP rule")
    for attachment in _topology(commands).attachments:
        if attachment.address in assigned:
            continue
        assigned.append(attachment.address)
        net = None
        for subnet, _gateway in subnets:
            if attachment.address in subnet and (
                net is None or subnet.prefixlen >= net.prefixlen
            ):
                net = subnet
        created = _topology(commands).networks.get(attachment.network)
        if created is not None and attachment.address not in created.subnet:
            gaps.append(str(attachment.address) + " is outside " + attachment.network)
        elif net is None:
            gaps.append(str(attachment.address) + " is outside the bridge subnet")
        if not any(attachment.address in network for network in drops):
            gaps.append(str(attachment.address) + " is not covered by a DROP rule")
    for subnet, gateway in subnets:
        spare = _spare_address_gap(subnet, gateway, assigned)
        if spare is not None:
            gaps.append(spare)
    for name in _REQUIRED_CONTAINERS:
        if name not in names:
            gaps.append(name + " has no run command")
    return gaps


def test_deploy_firewall_covers_every_container_ip() -> None:
    """Every run-command address is dropped, and the bridge excludes the broker.

    A substring check for ``-j DROP`` still passed when Docker assigned an
    address the rules do not name, and when the bridge included ``10.8.0.4``.
    Docker 29 creates ``DOCKER-USER`` empty, so a routed packet that matches
    no rule is forwarded. A proxy on a host address uses ``INPUT``. Two
    containers on one bridge are switched, so a ``DROP`` between them does
    not apply.
    """
    deploy = _text("docs/DEPLOY.md")
    commands = _bash_commands(deploy)
    assert firewall_gaps(deploy) == []
    assert docker_user_order_gaps(deploy) == []
    assert provider_egress_gaps(deploy) == []
    assert database_path_gaps(deploy) == []
    assert certificate_bridge_gaps(deploy) == []
    assert idempotent_gaps(commands) == []
    sentica = [
        command
        for command in commands
        if command.startswith("iptables -A SENTICA_NEW ")
    ]
    routed = "iptables -A SENTICA_NEW -s 10.8.0.85/32 -j DROP"
    assert sentica[11] == routed
    assert not any("--dport 5432" in command for command in sentica)
    assert not any("10.8.0.23" in command for command in sentica)
    topology = _topology(commands)
    assert (
        _packet_path(
            ipaddress.IPv4Address("10.8.0.34"),
            ipaddress.IPv4Address("10.8.0.35"),
            topology,
            set(),
        )
        == "BRIDGE"
    )
    assert (
        _packet_path(
            ipaddress.IPv4Address("10.8.0.18"),
            ipaddress.IPv4Address("10.8.0.35"),
            topology,
            set(),
        )
        == "UNREACHABLE"
    )
    assert (
        _packet_path(
            ipaddress.IPv4Address("10.8.0.18"),
            ipaddress.IPv4Address("10.8.0.22"),
            topology,
            set(),
        )
        == "BRIDGE"
    )
    assert (
        _packet_path(
            ipaddress.IPv4Address("10.8.0.22"),
            ipaddress.IPv4Address("10.8.0.85"),
            topology,
            set(),
        )
        == "UNREACHABLE"
    )
    assert (
        _packet_path(
            ipaddress.IPv4Address("10.8.0.82"),
            ipaddress.IPv4Address("10.8.0.85"),
            topology,
            set(),
        )
        == "BRIDGE"
    )
    hostname_rule = """```bash
iptables -I DOCKER-USER 1 -s 10.8.0.14/32 -p tcp -d api.openai.com --dport 443 -j RETURN
```
"""
    assert any(
        "resolved when the rule is inserted" in item
        for item in provider_egress_gaps(hostname_rule)
    )
    appended = """```bash
docker run --name sentica-memory-api --ip 10.8.0.10 img
docker run --name sentica-memory-deriver --ip 10.8.0.11 img
docker run --name sentica-memory-migrate --ip 10.8.0.12 img
docker run --name sentica-memory-proxy --ip 10.8.0.13 img
docker run --name sentica-memory-gateway --ip 10.8.0.14 img
```
```bash
iptables -A DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A DOCKER-USER -s 10.8.0.10/32 -d 10.8.0.13/32 -p tcp --dport 6432 -j RETURN
iptables -A DOCKER-USER -s 10.8.0.11/32 -d 10.8.0.13/32 -p tcp --dport 6432 -j RETURN
iptables -A DOCKER-USER -s 10.8.0.12/32 -d 10.8.0.13/32 -p tcp --dport 6432 -j RETURN
iptables -A DOCKER-USER -s 10.8.0.10/32 -d 10.8.0.14/32 -p tcp --dport 443 -j RETURN
iptables -A DOCKER-USER -s 10.8.0.11/32 -d 10.8.0.14/32 -p tcp --dport 443 -j RETURN
iptables -A DOCKER-USER -s 10.8.0.10/32 -j DROP
iptables -A DOCKER-USER -s 10.8.0.11/32 -j DROP
iptables -A DOCKER-USER -s 10.8.0.12/32 -j DROP
iptables -A DOCKER-USER -s 10.8.0.13/32 -j DROP
iptables -A DOCKER-USER -s 10.8.0.14/32 -j DROP
```
"""
    assert docker_user_order_gaps(appended) == []
    hidden = appended.replace(
        "iptables -A DOCKER-USER -m conntrack",
        "iptables -I DOCKER-USER 1 -j RETURN\niptables -A DOCKER-USER -m conntrack",
    )
    assert any("is not dropped" in item for item in docker_user_order_gaps(hidden))
    missing_api = appended.replace(
        "docker run --name sentica-memory-api --ip 10.8.0.10 img\n",
        "",
    )
    assert any(
        "sentica-memory-api has no container address" in item
        for item in docker_user_order_gaps(missing_api)
    )
    old_host_proxy = """The egress proxy listens on `10.8.0.5:8443`. That address is on the host.
```
api.openai.com
api.anthropic.com
generativelanguage.googleapis.com
```
```bash
docker network create --driver bridge --subnet 10.8.0.16/28 --gateway 10.8.0.17 sentica-instance
docker run --name sentica-memory-gateway --ip 10.8.0.22 img
```
```bash
iptables -I DOCKER-USER 1 -s 10.8.0.22/32 -d 10.8.0.5/32 -p tcp --dport 8443 -j RETURN
iptables -I DOCKER-USER 2 -s 10.8.0.22/32 -j DROP
```
"""
    assert any("INPUT" in item for item in provider_egress_gaps(old_host_proxy))
    same_bridge = """The egress proxy listens on `10.8.0.23:8443`.
```
api.openai.com
api.anthropic.com
generativelanguage.googleapis.com
```
```bash
docker network create --driver bridge --subnet 10.8.0.16/28 --gateway 10.8.0.17 sentica-instance
docker run --name sentica-memory-api --network sentica-instance --ip 10.8.0.18 img
docker run --name sentica-memory-deriver --network sentica-instance --ip 10.8.0.19 img
docker run --name sentica-memory-migrate --network sentica-instance --ip 10.8.0.20 img
docker run --name sentica-memory-proxy --network sentica-instance --ip 10.8.0.21 img
docker run --name sentica-memory-gateway --network sentica-instance --ip 10.8.0.22 img
docker run --name sentica-memory-egress --network sentica-instance --ip 10.8.0.23 img
```
```bash
iptables -A SENTICA -s 10.8.0.22/32 -d 10.8.0.23/32 -p tcp --dport 8443 -j RETURN
iptables -A SENTICA -s 10.8.0.18/32 -j DROP
iptables -A SENTICA -s 10.8.0.19/32 -j DROP
iptables -A SENTICA -s 10.8.0.20/32 -j DROP
iptables -A SENTICA -s 10.8.0.21/32 -j DROP
iptables -A SENTICA -s 10.8.0.22/32 -j DROP
iptables -A SENTICA -s 10.8.0.23/32 -j DROP
```
"""
    assert any("is not forwarded" in item for item in provider_egress_gaps(same_bridge))
    ruled_only = """```bash
docker network create --internal --subnet 10.8.0.64/28 --gateway 10.8.0.65 sentica-db
docker run --name sentica-memory-proxy --network sentica-instance --ip 10.8.0.21 img
docker network connect --ip 10.8.0.66 sentica-db sentica-memory-postgres
iptables -A SENTICA -s 10.8.0.21/32 -d 10.8.0.6/32 -p tcp --dport 5432 -j RETURN
```
"""
    assert any("database bridge" in item for item in database_path_gaps(ruled_only))
    repeated_insert = """```bash
iptables -I DOCKER-USER 1 -s 10.8.0.10/32 -j DROP
iptables -I DOCKER-USER 2 -s 10.8.0.11/32 -j DROP
```
"""
    assert idempotent_gaps(_bash_commands(repeated_insert)) != []
    reversed_insert = appended.replace("-A DOCKER-USER", "-I DOCKER-USER")
    assert any(
        "is not allowed" in item for item in docker_user_order_gaps(reversed_insert)
    )
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
    assert any("no spare address" in item for item in limited)
    shared_proxy = """```bash
docker network create --driver bridge --subnet 10.8.0.16/28 --gateway 10.8.0.17 sentica-instance
docker run --name sentica-memory-proxy --network sentica-instance --ip 10.8.0.21 img
docker run --name sentica-memory-gateway --network sentica-instance --ip 10.8.0.22 img
```
"""
    assert any(
        "shares a bridge" in item for item in certificate_bridge_gaps(shared_proxy)
    )


_POSTGRES_IMAGE = (
    "pgvector/pgvector:pg15"
    + "@sha256:47af0b65960b14f1c6945aa4ccae37836c632e9a6fe0eea9160936e616e5b2e9"
)
_PGBOUNCER_IMAGE = (
    "edoburu/pgbouncer:v1.24.1-p1"
    + "@sha256:3db3d7223e93af52b4116f642951a1a5fa44702a88c2a59cf7562cac19320c9e"
)


def _hba_rules(text: str) -> list[str]:
    rules: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            rules.append(line)
    return rules


def _postgres_run(deploy: str) -> str:
    for command in _bash_commands(deploy):
        if (
            command.startswith("docker run ")
            and "--name sentica-memory-postgres" in command
        ):
            return command
    return ""


def postgres_hardening_gaps(command: str) -> list[str]:
    """Flags on the Postgres ``docker run``, not on the other containers."""
    gaps: list[str] = []
    if not command.startswith("docker run "):
        gaps.append("postgres command is missing")
        return gaps
    if "--read-only" not in command.split():
        gaps.append("postgres command is not read-only")
    if "--cap-drop ALL" not in command:
        gaps.append("postgres command keeps capabilities")
    if "--auth-local=trust" in command or "--auth-host=trust" in command:
        gaps.append("postgres initdb uses trust")
    if "--auth-local=peer --auth-host=scram-sha-256" not in command:
        gaps.append("postgres initdb auth is not peer and scram")
    return gaps


_HBA_RULES = [
    "local all sentica peer map=sentica",
    "host all all 10.8.0.67/32 scram-sha-256",
    "host all all 0.0.0.0/0 reject",
    "host all all ::/0 reject",
]
_BUSYBOX = (
    "busybox:1.36"
    + "@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)
_TOPOLOGY_NODEID = (
    "tests/sentica/test_egress_proxy.py::test_topology_blocks_the_proxy_and_postgres"
)


def test_deploy_postgres_pins_a_digest_and_rejects_trust() -> None:
    """The Postgres command stays hardened and does not use trust auth."""
    deploy = " ".join(_text("docs/DEPLOY.md").split())
    assert _POSTGRES_IMAGE in deploy
    assert _PGBOUNCER_IMAGE in deploy
    assert "POSTGRES_PASSWORD_FILE=/run/secrets/password" in deploy
    assert "POSTGRES_HOST_AUTH_METHOD=" not in deploy
    assert "does not set `POSTGRES_PASSWORD` or `POSTGRES_HOST_AUTH_METHOD`" in deploy
    assert "-e POSTGRES_PASSWORD=" not in deploy
    assert "/var/lib/sentica/postgres:/var/lib/postgresql/data" in deploy
    assert "--user 999:999" in deploy
    assert "ident_file=/etc/sentica/pg_ident.conf" in deploy
    command = _postgres_run(_text("docs/DEPLOY.md"))
    assert postgres_hardening_gaps(command) == []
    assert postgres_hardening_gaps(command.replace(" --read-only", "", 1)) != []
    assert postgres_hardening_gaps(command.replace(" --cap-drop ALL", "", 1)) != []
    hba = _text("docker/sentica/instance-pg_hba.conf")
    assert "trust" not in hba.lower()
    assert _hba_rules(hba) == _HBA_RULES
    weaker = "host all all 0.0.0.0/0 md5\n" + hba
    assert _hba_rules(weaker) != _HBA_RULES
    ident = _text("docker/sentica/instance-pg_ident.conf").lower()
    assert "trust" not in ident
    assert "sentica postgres sentica" in ident
    assert "does not send a password" not in ident


def _firewall_rule_index(guide: list[str], prefix: str) -> int:
    return next(index for index, line in enumerate(guide) if line.startswith(prefix))


def test_firewall_script_matches_the_guide() -> None:
    """The unit runs the same rules the guide shows, after Docker."""
    guide = [
        command
        for command in _bash_commands(_text("docs/DEPLOY.md"))
        if command.startswith("iptables ")
    ]
    script_lines = [
        line.strip()
        for line in _text("docker/sentica/sentica-firewall.sh").splitlines()
        if line.strip().startswith("iptables ")
    ]
    assert script_lines == guide
    created = _firewall_rule_index(guide, "iptables -N SENTICA_NEW")
    filled = _firewall_rule_index(guide, "iptables -A SENTICA_NEW ")
    inserted = _firewall_rule_index(guide, "iptables -I DOCKER-USER 1 -j SENTICA_NEW")
    flushed = _firewall_rule_index(guide, "iptables -F SENTICA ")
    renamed = _firewall_rule_index(guide, "iptables -E SENTICA_NEW SENTICA")
    assert created < filled < inserted < flushed < renamed
    for line in guide[:inserted]:
        assert line.split()[1:3] != ["-F", "SENTICA"]
    unit = _text("docker/sentica/sentica-firewall.service")
    assert "After=docker.service" in unit
    assert "Requires=docker.service" in unit
    assert "Type=oneshot" in unit
    assert "ExecStart=/usr/local/sbin/sentica-firewall.sh" in unit
    assert "WantedBy=multi-user.target" in unit
    deploy = " ".join(_text("docs/DEPLOY.md").split())
    assert "After=docker.service" in deploy
    assert "iptables-persistent" in deploy
    assert "never forwards without drops" in deploy


def _keyword_selects(expression: str, nodeid: str) -> bool:
    from _pytest.mark.expression import Expression

    compiled = Expression.compile(expression)
    folded = nodeid.lower()
    return compiled.evaluate(lambda name: name.lower() in folded)


def firewall_ci_gaps(command: str) -> list[str]:
    """The main pytest command must collect the topology test."""
    gaps: list[str] = []
    if "test_egress_proxy.py" in command:
        gaps.append("firewall topology test is ignored")
    tokens = shlex.split(command)
    if "-k" in tokens:
        expression = tokens[tokens.index("-k") + 1]
        if not _keyword_selects(expression, _TOPOLOGY_NODEID):
            gaps.append("firewall topology test is excluded by -k")
    return gaps


def test_ci_collects_the_firewall_topology_test() -> None:
    """The main CI job runs the test that applies the firewall."""
    workflow = _text(".github/workflows/unittest.yml")
    commands = re.findall(r"uv run pytest[^\n]*", workflow)
    main = [
        command
        for command in commands
        if "--ignore=tests/sentica/test_m1_stack.py" in command
    ]
    assert main == ["uv run pytest -x --ignore=tests/sentica/test_m1_stack.py"]
    assert firewall_ci_gaps(main[0]) == []
    excluded = main[0] + " -k 'not topology'"
    assert firewall_ci_gaps(excluded) != []
    assert any("excluded by -k" in item for item in firewall_ci_gaps(excluded))


def test_deploy_password_file_steps_create_the_file(tmp_path: Path) -> None:
    """The guide's install creates the password file before anything writes it."""
    deploy = _text("docs/DEPLOY.md")
    blocks = re.findall(r"```bash\n(.*?)```", deploy, re.DOTALL)
    fence = next(block for block in blocks if "sentica-memory-postgres" in block)
    installs = [
        command
        for command in _bash_commands("```bash\n" + fence + "```")
        if command.startswith("install ")
    ]
    password = next(command for command in installs if "postgres.password" in command)
    assert password.split()[-2:] == ["/dev/null", "/run/sentica/postgres.password"]
    assert any(command.startswith("install -d ") for command in installs)
    root = tmp_path / "scratch"
    root.mkdir()
    lines = ["set -eu"]
    for command in installs:
        lines.append(
            command.replace("/run/sentica", "/work/run").replace(
                "/var/lib/sentica", "/work/lib"
            )
        )
    lines.append("stat -c '%u %g %a %s' /work/run/postgres.password")
    lines.append("printf '%s\\n' one-line > /work/run/postgres.password")
    lines.append("stat -c '%u %g %a' /work/run/postgres.password")
    lines.append("cat /work/run/postgres.password")
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "root",
            "-v",
            str(root) + ":/work",
            "--entrypoint",
            "sh",
            _BUSYBOX,
            "-c",
            "\n".join(lines),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    empty, written, content = completed.stdout.splitlines()
    assert empty == "999 999 400 0"
    assert written == "999 999 400"
    assert content == "one-line"
    host_file = root / "run" / "postgres.password"
    host_stat = host_file.stat()
    assert host_stat.st_uid == 999
    assert host_stat.st_gid == 999
    assert stat.S_IMODE(host_stat.st_mode) == 0o400
    data = root / "lib" / "postgres"
    assert data.is_dir()
    assert data.stat().st_uid == 999
    assert stat.S_IMODE(data.stat().st_mode) == 0o700


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
