"""The shipped egress proxy allowlist, and throwaway containers that enforce it."""

from __future__ import annotations

import ipaddress
import os
import random
import re
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = ROOT / "docker" / "sentica" / "egress-proxy.yml"
_CLIENT = (
    "python:3.13-slim-bookworm"
    + "@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641"
)
_SQUID = (
    "ubuntu/squid:6.6-24.04_edge"
    + "@sha256:8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c"
)
_PROVIDER_HOSTS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
)
_HOST_LINE = re.compile(
    r"^acl provider_host dstdom_regex -n -i "
    + r"\^((?:[A-Za-z0-9-]+\\\.)+[A-Za-z0-9-]+)"
    + r"\\\.\?\$$"
)
_CONNECT_TIMEOUT = "2"


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _config_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def proxy_config_gaps(text: str) -> list[str]:
    """Exact hostnames, port 443, CONNECT only, and a final deny."""
    lines = _config_lines(text)
    gaps: list[str] = []
    hosts: list[str] = []
    for line in lines:
        if not line.startswith("acl provider_host "):
            continue
        if not line.startswith("acl provider_host dstdom_regex -n "):
            gaps.append("provider host resolves an IP literal")
            continue
        matched = _HOST_LINE.match(line)
        if matched is None:
            gaps.append("provider host is not an exact hostname")
            continue
        hosts.append(matched.group(1).replace("\\.", "."))
    if set(hosts) != set(_PROVIDER_HOSTS) or len(hosts) != len(_PROVIDER_HOSTS):
        gaps.append("allowlist is not the three provider hostnames")
    allows = [line for line in lines if line.startswith("http_access allow")]
    if allows != ["http_access allow gateway_src CONNECT SSL_ports provider_host"]:
        gaps.append("CONNECT allow is not limited to the gateway and the hosts")
    access = [line for line in lines if line.startswith("http_access ")]
    if not access or access[-1] != "http_access deny all":
        gaps.append("proxy does not default-deny")
    if [line for line in lines if line.startswith("acl SSL_ports ")] != [
        "acl SSL_ports port 443"
    ]:
        gaps.append("CONNECT is not limited to port 443")
    if "acl gateway_src src 10.8.0.34/32" not in lines:
        gaps.append("proxy does not accept only the gateway")
    if [line for line in lines if line.startswith("http_port ")] != [
        "http_port 10.8.0.35:8443"
    ]:
        gaps.append("proxy listens on another port")
    return gaps


def test_egress_proxy_config_is_the_provider_allowlist() -> None:
    """The shipped config names the three hosts and denies everything else."""
    shipped = _text("docker/sentica/egress-proxy.conf")
    assert proxy_config_gaps(shipped) == []
    opened = "http_access allow all\nhttp_access deny all\n"
    assert proxy_config_gaps(opened) != []
    literal = shipped.replace("dstdom_regex -n -i", "dstdom_regex -i")
    assert any("IP literal" in item for item in proxy_config_gaps(literal))


def test_proxy_image_and_base_images_are_pinned() -> None:
    """Both Dockerfile stages and the proxy image carry a digest."""
    dockerfile = _text("Dockerfile")
    pins = re.findall(
        r"^FROM python:3.13-slim-bookworm@sha256:[0-9a-f]{64} AS ",
        dockerfile,
        re.MULTILINE,
    )
    assert len(pins) == 2
    digest = _SQUID.split("@", 1)[1]
    compose = _text("docker/sentica/egress-proxy.yml")
    deploy = _text("docs/DEPLOY.md")
    assert digest in compose
    assert digest in deploy
    assert _SQUID in compose.replace("\n", "")
    assert "25.04" not in compose
    assert "ports:" not in compose


def test_only_the_gateway_env_sets_the_proxy() -> None:
    """Provider calls use the egress proxy. Other containers get no proxy."""
    env = _text("docker/sentica/gateway.env")
    assert "HTTPS_PROXY=http://10.8.0.35:8443" in env
    assert env.startswith("HTTPS_PROXY=")
    assert "NO_PROXY=" in env
    assert "HTTP_PROXY=" not in env
    assert "ALL_PROXY=" not in env
    for host in _PROVIDER_HOSTS:
        assert host not in env
    for path in (ROOT / "docker" / "sentica").glob("*.env"):
        if path.name == "gateway.env":
            continue
        body = path.read_text(encoding="utf-8")
        assert "HTTPS_PROXY" not in body
        assert "HTTP_PROXY" not in body
        assert "ALL_PROXY" not in body
    deploy = " ".join(_text("docs/DEPLOY.md").split())
    assert "do not set `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`" in deploy


def _run(
    args: list[str], env: dict[str, str], *, check: bool
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _occupied_subnets(env: dict[str, str]) -> list[ipaddress.IPv4Network]:
    found: list[ipaddress.IPv4Network] = []
    listed = _run(["docker", "network", "ls", "-q"], env, check=False)
    ids = [line for line in listed.stdout.split() if line]
    if ids and listed.returncode == 0:
        inspected = _run(["docker", "network", "inspect", *ids], env, check=False)
        for match in re.finditer(
            r'"Subnet": "(\d+\.\d+\.\d+\.\d+/\d+)"', inspected.stdout
        ):
            found.append(ipaddress.IPv4Network(match.group(1)))
    routes = _run(["ip", "-4", "route"], env, check=False)
    for line in routes.stdout.splitlines():
        token = line.split()[0] if line.split() else ""
        if "/" not in token:
            continue
        try:
            parsed = ipaddress.ip_network(token, strict=False)
        except ValueError:
            continue
        if isinstance(parsed, ipaddress.IPv4Network):
            found.append(parsed)
    return found


def _free_slash24(env: dict[str, str]) -> ipaddress.IPv4Network:
    """A /24 that does not overlap a docker network or a host route."""
    occupied = _occupied_subnets(env)
    rng = random.SystemRandom()
    for _ in range(64):
        second = rng.randrange(128, 255)
        third = rng.randrange(0, 256)
        block = ipaddress.IPv4Network(f"10.{second}.{third}.0/24")
        if any(block.overlaps(item) for item in occupied):
            continue
        return block
    raise AssertionError("no free subnet")


def _carve(block: ipaddress.IPv4Network, index: int) -> ipaddress.IPv4Network:
    base = int(block.network_address) + (index * 16)
    return ipaddress.IPv4Network((base, 28))


def _host(subnet: ipaddress.IPv4Network, offset: int) -> str:
    return str(subnet.network_address + offset)


def _create_network(
    env: dict[str, str],
    name: str,
    subnet: ipaddress.IPv4Network,
    *,
    internal: bool,
) -> None:
    args = ["docker", "network", "create", "--driver", "bridge"]
    if internal:
        args.append("--internal")
    args.extend(["--subnet", str(subnet), "--gateway", _host(subnet, 1), name])
    created = _run(args, env, check=False)
    assert created.returncode == 0, created.stderr


def _remove_network(env: dict[str, str], name: str) -> None:
    _run(["docker", "network", "rm", name], env, check=False)


def _compose(
    env: dict[str, str], project: str, *args: str
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["docker", "compose", "-p", project, "-f", str(_COMPOSE), *args],
        env,
        check=False,
    )


def _proxy_script(proxy: str, host: str, port: int, method: str) -> str:
    return (
        "import socket\n"
        f"target = {host!r}\n"
        f"s = socket.create_connection(({proxy!r}, 8443), 20)\n"
        "s.settimeout(20)\n"
        f"method = {method!r}\n"
        f"port = {port}\n"
        "if method == 'CONNECT':\n"
        "    req = f'CONNECT {target}:{port} HTTP/1.1\\r\\nHost: {target}:{port}\\r\\n\\r\\n'\n"
        "else:\n"
        "    req = f'GET http://{target}:{port}/ HTTP/1.1\\r\\nHost: {target}\\r\\n\\r\\n'\n"
        "s.sendall(req.encode())\n"
        "print(s.recv(1024).split(b'\\r\\n', 1)[0].decode('ascii', 'replace'))\n"
        "s.close()\n"
    )


def _proxy_status(
    env: dict[str, str],
    network: str,
    source: str,
    proxy: str,
    host: str,
    port: int,
    method: str,
) -> str:
    completed = _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--ip",
            source,
            _CLIENT,
            "python",
            "-c",
            _proxy_script(proxy, host, port, method),
        ],
        env,
        check=False,
    )
    if completed.returncode != 0:
        return completed.stderr.strip() or completed.stdout.strip()
    return completed.stdout.strip()


def _rewritten_config(gateway: str, proxy: str) -> str:
    shipped = _text("docker/sentica/egress-proxy.conf")
    return shipped.replace("10.8.0.34", gateway).replace("10.8.0.35", proxy)


def test_egress_proxy_connects_an_allowed_host_and_refuses_another() -> None:
    """A throwaway proxy allows one provider host and refuses a different host.

    The client uses the gateway address on a randomly chosen free subnet.
    Another address on that bridge is refused. An IP-literal CONNECT is
    refused as well. Nothing is published on the host.
    """
    env = os.environ.copy()
    block = _free_slash24(env)
    egress_net = _carve(block, 0)
    wan_net = _carve(block, 1)
    proxy_ip = _host(egress_net, 2)
    gateway_ip = _host(egress_net, 3)
    other_ip = _host(egress_net, 4)
    wan_ip = _host(wan_net, 2)
    project = "sentica-r4acl-0fe5"
    egress_name = project + "-egress"
    wan_name = project + "-wan"
    with tempfile.TemporaryDirectory(prefix=project + "-") as tmp:
        conf = Path(tmp) / "squid.conf"
        conf.write_text(_rewritten_config(gateway_ip, proxy_ip), encoding="utf-8")
        env["SENTICA_EGRESS_NETWORK"] = egress_name
        env["SENTICA_EGRESS_WAN_NETWORK"] = wan_name
        env["SENTICA_EGRESS_IP"] = proxy_ip
        env["SENTICA_EGRESS_WAN_IP"] = wan_ip
        env["SENTICA_EGRESS_CONF"] = str(conf)
        _compose(env, project, "down", "--remove-orphans")
        _remove_network(env, egress_name)
        _remove_network(env, wan_name)
        try:
            _create_network(env, egress_name, egress_net, internal=True)
            _create_network(env, wan_name, wan_net, internal=False)
            started = _compose(env, project, "up", "-d")
            assert started.returncode == 0, started.stderr
            allowed = ""
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                allowed = _proxy_status(
                    env,
                    egress_name,
                    gateway_ip,
                    proxy_ip,
                    "api.openai.com",
                    443,
                    "CONNECT",
                )
                if allowed.startswith("HTTP/1.1 200"):
                    break
                time.sleep(0.5)
            refused = _proxy_status(
                env, egress_name, gateway_ip, proxy_ip, "example.com", 443, "CONNECT"
            )
            plain = _proxy_status(
                env, egress_name, gateway_ip, proxy_ip, "api.openai.com", 80, "GET"
            )
            other = _proxy_status(
                env, egress_name, other_ip, proxy_ip, "api.openai.com", 443, "CONNECT"
            )
            resolved = socket.getaddrinfo("api.openai.com", 443, socket.AF_INET)[0][4][
                0
            ]
            literal_ip = resolved if isinstance(resolved, str) else str(resolved)
            literal = _proxy_status(
                env, egress_name, gateway_ip, proxy_ip, literal_ip, 443, "CONNECT"
            )
            logs = _compose(env, project, "logs")
            detail = logs.stdout + logs.stderr
            assert allowed.startswith("HTTP/1.1 200"), detail
            assert "403" in refused, refused
            assert "403" in plain, plain
            assert "403" in other, other
            assert "403" in literal, literal
            assert not literal.startswith("HTTP/1.1 200")
        finally:
            _compose(env, project, "down", "--remove-orphans")
            _remove_network(env, egress_name)
            _remove_network(env, wan_name)


def _open_script(host: str, port: int) -> str:
    return (
        "import socket\n"
        f"s = socket.socket(); s.settimeout({_CONNECT_TIMEOUT})\n"
        "try:\n"
        f"    s.connect(({host!r}, {port}))\n"
        "    print('OPEN')\n"
        "except Exception as exc:\n"
        "    print('FAIL', type(exc).__name__)\n"
        "finally:\n"
        "    s.close()\n"
    )


def _container_open(env: dict[str, str], container: str, host: str, port: int) -> str:
    completed = _run(
        [
            "docker",
            "exec",
            container,
            "python",
            "-c",
            _open_script(host, port),
        ],
        env,
        check=False,
    )
    return (completed.stdout or completed.stderr).strip()


def _exec_proxy_status(
    env: dict[str, str],
    container: str,
    proxy: str,
    host: str,
    port: int,
    method: str,
) -> str:
    completed = _run(
        [
            "docker",
            "exec",
            container,
            "python",
            "-c",
            _proxy_script(proxy, host, port, method),
        ],
        env,
        check=False,
    )
    if completed.returncode != 0:
        return completed.stderr.strip() or completed.stdout.strip()
    return completed.stdout.strip()


def _bash_open(env: dict[str, str], container: str, host: str, port: int) -> str:
    completed = _run(
        [
            "docker",
            "exec",
            container,
            "timeout",
            _CONNECT_TIMEOUT,
            "bash",
            "-c",
            f"echo >/dev/tcp/{host}/{port}",
        ],
        env,
        check=False,
    )
    if completed.returncode == 0:
        return "OPEN"
    return "FAIL " + str(completed.returncode)


def test_topology_blocks_the_proxy_and_postgres() -> None:
    """Gateway reaches a provider. Other roles cannot open the proxy or Postgres.

    The certificate proxy is the container that reaches Postgres. Networks
    are throwaway bridges on a free subnet. The test does not change the
    host iptables and does not publish a host port.
    """
    env = os.environ.copy()
    block = _free_slash24(env)
    inst = _carve(block, 0)
    egress = _carve(block, 1)
    wan = _carve(block, 2)
    db = _carve(block, 3)
    api_ip = _host(inst, 2)
    deriver_ip = _host(inst, 3)
    migrate_ip = _host(inst, 4)
    cert_ip = _host(inst, 5)
    gateway_inst = _host(inst, 6)
    gateway_egress = _host(egress, 2)
    proxy_ip = _host(egress, 3)
    wan_ip = _host(wan, 2)
    postgres_ip = _host(db, 2)
    cert_db = _host(db, 3)
    project = "sentica-r4topo-0fe5"
    names = {
        "inst": project + "-inst",
        "egress": project + "-egress",
        "wan": project + "-wan",
        "db": project + "-db",
    }
    roles = {
        "pg": project + "-pg",
        "gw": project + "-gw",
        "api": project + "-api",
        "deriver": project + "-deriver",
        "migrate": project + "-migrate",
        "cert": project + "-cert",
    }
    listen = (
        "import socket, threading, time\n"
        "def serve(port, msg):\n"
        "    s = socket.socket()\n"
        "    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "    s.bind(('0.0.0.0', port))\n"
        "    s.listen(8)\n"
        "    while True:\n"
        "        c, _addr = s.accept()\n"
        "        c.sendall(msg)\n"
        "        c.close()\n"
        "threading.Thread(target=serve, args=(5432, b'pg\\n'), daemon=True).start()\n"
        "time.sleep(3600)\n"
    )
    with tempfile.TemporaryDirectory(prefix=project + "-") as tmp:
        conf = Path(tmp) / "squid.conf"
        conf.write_text(_rewritten_config(gateway_egress, proxy_ip), encoding="utf-8")
        env["SENTICA_EGRESS_NETWORK"] = names["egress"]
        env["SENTICA_EGRESS_WAN_NETWORK"] = names["wan"]
        env["SENTICA_EGRESS_IP"] = proxy_ip
        env["SENTICA_EGRESS_WAN_IP"] = wan_ip
        env["SENTICA_EGRESS_CONF"] = str(conf)
        _compose(env, project, "down", "--remove-orphans")
        for name in roles.values():
            _run(["docker", "rm", "-f", name], env, check=False)
        for name in names.values():
            _remove_network(env, name)
        try:
            _create_network(env, names["inst"], inst, internal=False)
            _create_network(env, names["egress"], egress, internal=True)
            _create_network(env, names["wan"], wan, internal=False)
            _create_network(env, names["db"], db, internal=True)
            pg = _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    roles["pg"],
                    "--network",
                    names["db"],
                    "--ip",
                    postgres_ip,
                    _CLIENT,
                    "python",
                    "-c",
                    listen,
                ],
                env,
                check=False,
            )
            assert pg.returncode == 0, pg.stderr
            started = _compose(env, project, "up", "-d")
            assert started.returncode == 0, started.stderr
            for role, ip, network in (
                ("gw", gateway_inst, names["inst"]),
                ("api", api_ip, names["inst"]),
                ("deriver", deriver_ip, names["inst"]),
                ("migrate", migrate_ip, names["inst"]),
                ("cert", cert_ip, names["inst"]),
            ):
                launched = _run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        roles[role],
                        "--network",
                        network,
                        "--ip",
                        ip,
                        _CLIENT,
                        "sleep",
                        "3600",
                    ],
                    env,
                    check=False,
                )
                assert launched.returncode == 0, launched.stderr
            joined_gw = _run(
                [
                    "docker",
                    "network",
                    "connect",
                    "--ip",
                    gateway_egress,
                    names["egress"],
                    roles["gw"],
                ],
                env,
                check=False,
            )
            assert joined_gw.returncode == 0, joined_gw.stderr
            joined_cert = _run(
                [
                    "docker",
                    "network",
                    "connect",
                    "--ip",
                    cert_db,
                    names["db"],
                    roles["cert"],
                ],
                env,
                check=False,
            )
            assert joined_cert.returncode == 0, joined_cert.stderr
            allowed = ""
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                allowed = _exec_proxy_status(
                    env,
                    roles["gw"],
                    proxy_ip,
                    "api.openai.com",
                    443,
                    "CONNECT",
                )
                if allowed.startswith("HTTP/1.1 200"):
                    break
                time.sleep(0.5)
            squid = _run(
                ["docker", "compose", "-p", project, "-f", str(_COMPOSE), "ps", "-q"],
                env,
                check=False,
            )
            squid_id = (
                squid.stdout.strip().splitlines()[0] if squid.stdout.strip() else ""
            )
            checks: list[tuple[str, str, str, int]] = []
            for role in ("api", "deriver", "migrate", "cert"):
                checks.append((role, roles[role], proxy_ip, 8443))
                checks.append((role, roles[role], wan_ip, 8443))
            for role in ("api", "deriver", "migrate", "gw"):
                checks.append((role + "-pg", roles[role], postgres_ip, 5432))

            def _probe(item: tuple[str, str, str, int]) -> tuple[str, str]:
                label, container, host, port = item
                return label + "->" + host, _container_open(env, container, host, port)

            with ThreadPoolExecutor(max_workers=8) as pool:
                blocked = dict(pool.map(_probe, checks))
            cert_pg = _container_open(env, roles["cert"], postgres_ip, 5432)
            proxy_pg = "no-squid"
            if squid_id:
                proxy_pg = _bash_open(env, squid_id, postgres_ip, 5432)
            logs = _compose(env, project, "logs")
            detail = logs.stdout + logs.stderr
            assert allowed.startswith("HTTP/1.1 200"), detail
            for label, result in blocked.items():
                assert result.startswith("FAIL TimeoutError"), label + " " + result
            assert cert_pg.startswith("OPEN"), cert_pg
            assert proxy_pg == "FAIL 124", proxy_pg
        finally:
            for name in roles.values():
                _run(["docker", "rm", "-f", name], env, check=False)
            _compose(env, project, "down", "--remove-orphans")
            for name in names.values():
                _remove_network(env, name)
