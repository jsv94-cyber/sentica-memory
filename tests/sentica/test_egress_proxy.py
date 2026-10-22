"""The shipped egress proxy allowlist, and throwaway containers that enforce it."""

from __future__ import annotations

import ipaddress
import os
import random
import re
import secrets
import shutil
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
_OLDER_SQUID = "sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029"
_POSTGRES = (
    "pgvector/pgvector:pg15"
    + "@sha256:47af0b65960b14f1c6945aa4ccae37836c632e9a6fe0eea9160936e616e5b2e9"
)
_PGBOUNCER = (
    "edoburu/pgbouncer:v1.24.1-p1"
    + "@sha256:3db3d7223e93af52b4116f642951a1a5fa44702a88c2a59cf7562cac19320c9e"
)
_BUSYBOX = (
    "busybox:1.36"
    + "@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
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
    """Every Python stage and the proxy image carry a digest."""
    dockerfile = _text("Dockerfile")
    stages = re.findall(
        r"^FROM python:3\.13-slim-bookworm(?:@sha256:[0-9a-f]{64})? AS (\S+)",
        dockerfile,
        re.MULTILINE,
    )
    pinned = re.findall(
        r"^FROM python:3\.13-slim-bookworm@sha256:[0-9a-f]{64} AS (\S+)",
        dockerfile,
        re.MULTILINE,
    )
    assert stages == ["builder", "sources", "runtime"]
    assert pinned == stages
    digest = _SQUID.split("@", 1)[1]
    compose = _text("docker/sentica/egress-proxy.yml")
    deploy = _text("docs/DEPLOY.md")
    assert digest in compose
    assert digest in deploy
    assert _SQUID in compose.replace("\n", "")
    assert _SQUID in deploy
    assert _POSTGRES in deploy
    assert _PGBOUNCER in deploy
    assert "ubuntu/squid:6.6-24.04_beta" not in compose
    assert "ubuntu/squid:6.6-24.04_beta" not in deploy
    assert _OLDER_SQUID not in compose
    assert _OLDER_SQUID not in deploy
    prose = " ".join(deploy.split())
    assert "pinned by digest" in prose
    assert "6.14-0ubuntu0.24.04.2" in prose
    assert "3.0.13-0ubuntu3.9" in prose
    assert "does not enable ICP" in prose
    assert "does not terminate TLS" in prose
    config = _text("docker/sentica/egress-proxy.conf")
    assert "icp_port" not in config
    assert "https_port" not in config
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
        "    err = getattr(exc, 'errno', None)\n"
        "    if err is None:\n"
        "        print('FAIL', type(exc).__name__)\n"
        "    else:\n"
        "        print('FAIL', type(exc).__name__, 'errno=' + str(err))\n"
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


def _route_table(env: dict[str, str], container: str) -> str:
    completed = _run(
        [
            "docker",
            "exec",
            container,
            "python",
            "-c",
            "print(open('/proc/net/route').read(), end='')",
        ],
        env,
        check=False,
    )
    text = (completed.stdout or completed.stderr).strip()
    assert completed.returncode == 0, text
    return text


def _has_default_route(table: str) -> bool:
    """True when the route table has a 0.0.0.0 destination."""
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == "00000000":
            return True
    return False


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


def _chain_name() -> str:
    return "SNT" + str(os.getpid())[-6:]


def _host_iptables(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run iptables in the host namespaces. The chain is removed afterwards."""
    return _run(
        [
            "docker",
            "run",
            "--rm",
            "--privileged",
            "--pid=host",
            "--network",
            "host",
            _BUSYBOX,
            "nsenter",
            "-t",
            "1",
            "-m",
            "-n",
            "--",
            "iptables",
            *args,
        ],
        env,
        check=False,
    )


def _clear_chain(env: dict[str, str], chain: str) -> None:
    for name in (chain, chain + "_NEW", chain + "_KEEP", chain + "_OLD"):
        while _host_iptables(env, "-D", "DOCKER-USER", "-j", name).returncode == 0:
            continue
        _host_iptables(env, "-F", name)
        _host_iptables(env, "-X", name)


def _apply_firewall_script(
    env: dict[str, str], chain: str, replacements: list[tuple[str, str]]
) -> None:
    """Run the shipped script with this test's addresses and chain name."""
    text = _text("docker/sentica/sentica-firewall.sh")
    for source, dest in replacements:
        text = text.replace(source + "/32", dest + "/32")
    text = text.replace("SENTICA", chain)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(text)
        path = handle.name
    os.chmod(path, 0o755)
    try:
        completed = _run(
            [
                "docker",
                "run",
                "--rm",
                "--privileged",
                "--pid=host",
                "--network",
                "host",
                "-v",
                path + ":/script:ro",
                "-v",
                "/tmp:/host-tmp",
                _BUSYBOX,
                "sh",
                "-c",
                "name=snt-fw-$$.sh; cp /script /host-tmp/$name; "
                "nsenter -t 1 -m -n -- sh /tmp/$name; rc=$?; "
                "rm -f /host-tmp/$name; exit $rc",
            ],
            env,
            check=False,
        )
    finally:
        os.unlink(path)
    assert completed.returncode == 0, completed.stderr


_IPTABLES_WRAPPER = """#!/bin/sh
real=/usr/sbin/iptables
trace=${SNT_TRACE:?}
miss=${SNT_MISS:?}
work=${SNT_WORK:?}

chain_drops() {
  chain=$1
  addr=$2
  file=$work/rules-$chain
  "$real" -S "$chain" >"$file" 2>/dev/null || return 1
  while IFS= read -r line; do
    case $line in
      "-A $chain -j RETURN"|"-A $chain -j ACCEPT")
        return 1
        ;;
      "-A $chain -s $addr/32 -j DROP")
        return 0
        ;;
      "-A $chain -s $addr/32 -j RETURN"|"-A $chain -s $addr/32 -j ACCEPT")
        return 1
        ;;
      "-A $chain -j "*)
        target=${line##* }
        if chain_drops "$target" "$addr"; then
          return 0
        fi
        ;;
    esac
  done <"$file"
  return 1
}

reaches() {
  addr=$1
  file=$work/docker-user
  "$real" -S DOCKER-USER >"$file" 2>/dev/null || return 1
  while IFS= read -r line; do
    case $line in
      "-A DOCKER-USER -j RETURN"|"-A DOCKER-USER -j ACCEPT")
        return 1
        ;;
      "-A DOCKER-USER -s $addr/32 -j DROP")
        return 0
        ;;
      "-A DOCKER-USER -j "*)
        target=${line##* }
        if chain_drops "$target" "$addr"; then
          return 0
        fi
        ;;
    esac
  done <"$file"
  return 1
}

"$real" "$@"
rc=$?
printf '%s %s\\n' "$rc" "$*" >>"$trace"
if ! reaches 10.8.0.18 || ! reaches 10.8.0.67; then
  {
    echo "MISS rc=$rc iptables $*"
    "$real" -S
  } >>"$miss"
  exit 99
fi
exit "$rc"
"""

_FIREWALL_NETNS_RUNNER = """#!/bin/sh
set -eu
umask 022
real=/usr/sbin/iptables
export PATH="$SNT_BIN:$PATH"
: >"$SNT_TRACE"
: >"$SNT_MISS"

"$real" -N DOCKER-USER 2>/dev/null || true

reset_chains() {
  for name in SENTICA SENTICA_NEW SENTICA_KEEP SENTICA_OLD; do
    while "$real" -D DOCKER-USER -j "$name" 2>/dev/null; do :; done
    "$real" -F "$name" 2>/dev/null || true
    "$real" -X "$name" 2>/dev/null || true
  done
  "$real" -F DOCKER-USER
}

drop_chain() {
  "$real" -N "$1"
  "$real" -A "$1" -s 10.8.0.18/32 -j DROP
  "$real" -A "$1" -s 10.8.0.67/32 -j DROP
}

run_script() {
  set +e
  sh "$SNT_SCRIPT"
  rc=$?
  set -e
  if [ -s "$SNT_MISS" ] || [ "$rc" -ne 0 ]; then
    echo "script rc=$rc" >&2
    cat "$SNT_MISS" >&2
    exit 99
  fi
}

reset_chains
drop_chain SENTICA
"$real" -A DOCKER-USER -j SENTICA
"$real" -A DOCKER-USER -j SENTICA
drop_chain SENTICA_NEW
"$real" -A DOCKER-USER -j SENTICA_NEW
run_script
run_script

reset_chains
"$real" -N SENTICA
"$real" -A DOCKER-USER -j SENTICA
drop_chain SENTICA_NEW
"$real" -I DOCKER-USER 1 -j SENTICA_NEW
run_script

reset_chains
drop_chain SENTICA_NEW
"$real" -A DOCKER-USER -j SENTICA_NEW
run_script

echo "checks=$(wc -l < "$SNT_TRACE" | tr -d ' ')"
deleted_ok=$(grep -c '^0 -D DOCKER-USER -j SENTICA$' "$SNT_TRACE" || true)
deleted_miss=$(grep -c '^1 -D DOCKER-USER -j SENTICA$' "$SNT_TRACE" || true)
echo "deleted_ok=$deleted_ok"
echo "deleted_miss=$deleted_miss"
echo RULES
"$real" -S
"""


def test_firewall_swap_keeps_a_drop_after_every_command() -> None:
    """Duplicate and leftover jumps never leave a step that does not drop.

    The check runs after every iptables command, in a private network
    namespace, for two runs from a duplicate jump plus a leftover chain.
    A later run starts from an empty referenced chain, and one starts
    from SENTICA_NEW alone.
    """
    env = os.environ.copy()
    name = "snt-ns-" + str(os.getpid())
    work = Path(tempfile.mkdtemp(prefix="snt-fw-"))
    bindir = work / "bin"
    bindir.mkdir()
    wrapper = bindir / "iptables"
    wrapper.write_text(_IPTABLES_WRAPPER, encoding="utf-8")
    wrapper.chmod(0o755)
    script = work / "sentica-firewall.sh"
    script.write_text(
        _text("docker/sentica/sentica-firewall.sh"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = work / "runner.sh"
    runner.write_text(_FIREWALL_NETNS_RUNNER, encoding="utf-8")
    runner.chmod(0o755)
    started = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "bridge",
            _BUSYBOX,
            "sleep",
            "300",
        ],
        env,
        check=False,
    )
    try:
        assert started.returncode == 0, started.stderr
        inspected = _run(
            ["docker", "inspect", "-f", "{{.State.Pid}}", name],
            env,
            check=False,
        )
        assert inspected.returncode == 0, inspected.stderr
        pid = inspected.stdout.strip()
        assert pid not in {"", "0"}
        completed = _run(
            [
                "docker",
                "run",
                "--rm",
                "--privileged",
                "--pid=host",
                "-e",
                "SNT_BIN=" + str(bindir),
                "-e",
                "SNT_TRACE=" + str(work / "trace"),
                "-e",
                "SNT_MISS=" + str(work / "miss"),
                "-e",
                "SNT_WORK=" + str(work),
                "-e",
                "SNT_SCRIPT=" + str(script),
                _BUSYBOX,
                "nsenter",
                "-t",
                "1",
                "-m",
                "--",
                "nsenter",
                "-t",
                pid,
                "-n",
                "--",
                "sh",
                str(runner),
            ],
            env,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr + completed.stdout
        report, rules = completed.stdout.split("RULES", 1)
        counts = {}
        for line in report.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            counts[key] = int(value)
        assert counts["checks"] >= 80
        assert counts["deleted_ok"] >= 2
        assert counts["deleted_miss"] >= 1
        jumps = [
            line for line in rules.splitlines() if line.startswith("-A DOCKER-USER -j ")
        ]
        assert jumps == ["-A DOCKER-USER -j SENTICA"]
        assert "-A SENTICA -s 10.8.0.18/32 -j DROP" in rules
        assert "-A SENTICA -s 10.8.0.67/32 -j DROP" in rules
        assert "SENTICA_NEW" not in rules
        assert "SENTICA_KEEP" not in rules
        assert "SENTICA_OLD" not in rules
    finally:
        _run(["docker", "rm", "-f", name], env, check=False)
        if work.exists():
            _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    "root",
                    "-v",
                    str(work) + ":/work",
                    "--entrypoint",
                    "sh",
                    _BUSYBOX,
                    "-c",
                    "rm -rf /work",
                ],
                env,
                check=False,
            )
        shutil.rmtree(work, ignore_errors=True)


def _packet_count(text: str, source: str, target: str, extra: str = "") -> int:
    for line in text.splitlines():
        if source not in line or extra not in line:
            continue
        parts = line.split()
        if target not in parts or not parts or not parts[0].isdigit():
            continue
        return int(parts[0])
    return -1


def test_topology_blocks_the_proxy_and_postgres() -> None:
    """Gateway reaches a provider. Other roles cannot open the proxy or Postgres.

    The certificate proxy is the container that reaches Postgres, on its own
    internal bridge. The gateway is not on that bridge. A source with a
    default route must time out. That proxy has no default route, and its
    failure must be errno 101. Each routed source must move its DROP
    counter. A throwaway chain with the guide's rule shape drops routed
    packets from the instance addresses. The test removes that chain and
    does not publish a host port.
    """
    env = os.environ.copy()
    block = _free_slash24(env)
    inst = _carve(block, 0)
    egress = _carve(block, 1)
    wan = _carve(block, 2)
    db = _carve(block, 3)
    clients = _carve(block, 4)
    api_ip = _host(inst, 2)
    deriver_ip = _host(inst, 3)
    migrate_ip = _host(inst, 4)
    gateway_inst = _host(inst, 5)
    api_clients = _host(clients, 2)
    deriver_clients = _host(clients, 3)
    migrate_clients = _host(clients, 4)
    cert_ip = _host(clients, 5)
    gateway_egress = _host(egress, 2)
    proxy_ip = _host(egress, 3)
    wan_ip = _host(wan, 2)
    postgres_ip = _host(db, 2)
    cert_db = _host(db, 3)
    project = "sentica-r5topo-0fe5"
    chain = _chain_name()
    names = {
        "inst": project + "-inst",
        "egress": project + "-egress",
        "wan": project + "-wan",
        "db": project + "-db",
        "clients": project + "-clients",
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
    listen_proxy = (
        "import socket, threading, time\n"
        "def serve():\n"
        "    s = socket.socket()\n"
        "    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "    s.bind(('0.0.0.0', 6432))\n"
        "    s.listen(8)\n"
        "    while True:\n"
        "        c, _addr = s.accept()\n"
        "        c.sendall(b'px\\n')\n"
        "        c.close()\n"
        "threading.Thread(target=serve, daemon=True).start()\n"
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
            _create_network(env, names["clients"], clients, internal=True)
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
                ("cert", cert_ip, names["clients"]),
            ):
                command = ["sleep", "3600"]
                if role == "cert":
                    command = ["python", "-c", listen_proxy]
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
                        *command,
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
            for container, address in (
                (roles["api"], api_clients),
                (roles["deriver"], deriver_clients),
                (roles["migrate"], migrate_clients),
            ):
                joined = _run(
                    [
                        "docker",
                        "network",
                        "connect",
                        "--ip",
                        address,
                        names["clients"],
                        container,
                    ],
                    env,
                    check=False,
                )
                assert joined.returncode == 0, joined.stderr
            _apply_firewall_script(
                env,
                chain,
                [
                    ("10.8.0.18", api_ip),
                    ("10.8.0.19", deriver_ip),
                    ("10.8.0.20", migrate_ip),
                    ("10.8.0.22", gateway_inst),
                    ("10.8.0.82", api_clients),
                    ("10.8.0.83", deriver_clients),
                    ("10.8.0.84", migrate_clients),
                    ("10.8.0.85", cert_ip),
                    ("10.8.0.34", gateway_egress),
                    ("10.8.0.35", proxy_ip),
                    ("10.8.0.50", wan_ip),
                    ("10.8.0.66", postgres_ip),
                    ("10.8.0.67", cert_db),
                ],
            )
            zeroed = _host_iptables(env, "-Z", chain)
            assert zeroed.returncode == 0, zeroed.stderr
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
            checks.append(("gw-cert", roles["gw"], cert_ip, 6432))

            def _probe(item: tuple[str, str, str, int]) -> tuple[str, str]:
                label, container, host, port = item
                return label + "->" + host, _container_open(env, container, host, port)

            with ThreadPoolExecutor(max_workers=8) as pool:
                blocked = dict(pool.map(_probe, checks))
            cert_pg = _container_open(env, roles["cert"], postgres_ip, 5432)
            api_proxy = _container_open(env, roles["api"], cert_ip, 6432)
            counted = _host_iptables(env, "-L", chain, "-n", "-v", "-x")
            assert counted.returncode == 0, counted.stderr
            opened = counted.stdout
            for source in (deriver_ip, migrate_ip, gateway_inst):
                assert _packet_count(opened, source, "DROP") > 0, source + "\n" + opened
            zeroed_again = _host_iptables(env, "-Z", chain)
            assert zeroed_again.returncode == 0, zeroed_again.stderr
            api_out = _container_open(env, roles["api"], "1.1.1.1", 443)
            proxy_pg = "no-squid"
            wan_out = "no-squid"
            if squid_id:
                proxy_pg = _bash_open(env, squid_id, postgres_ip, 5432)
                wan_out = _bash_open(env, squid_id, "1.1.1.1", 443)
            listed = _host_iptables(env, "-L", chain, "-n", "-v", "-x")
            logs = _compose(env, project, "logs")
            detail = logs.stdout + logs.stderr
            assert allowed.startswith("HTTP/1.1 200"), detail
            route_tables = {
                roles[role]: _route_table(env, roles[role])
                for role in ("api", "deriver", "migrate", "gw", "cert")
            }
            cert_routes = route_tables[roles["cert"]]
            assert not _has_default_route(cert_routes), (
                "certificate proxy has a default route\n" + cert_routes
            )
            for role in ("api", "deriver", "migrate", "gw"):
                routed = route_tables[roles[role]]
                assert _has_default_route(routed), role + "\n" + routed
            container_by_label: dict[str, str] = {}
            for label, container, host, _port in checks:
                container_by_label[label + "->" + host] = container
            for label, result in blocked.items():
                container = container_by_label[label]
                if _has_default_route(route_tables[container]):
                    assert result == "FAIL TimeoutError", label + " " + result
                else:
                    assert result == "FAIL OSError errno=101", label + " " + result
            assert cert_pg.startswith("OPEN"), cert_pg
            assert api_proxy.startswith("OPEN"), api_proxy
            assert proxy_pg == "FAIL 124", proxy_pg
            assert api_out.startswith("FAIL"), api_out
            assert listed.returncode == 0, listed.stderr
            table = listed.stdout
            assert _packet_count(table, api_ip, "DROP") > 0, table
            assert _packet_count(table, wan_ip, "RETURN", "dpt:443") > 0, (
                wan_out + table
            )
        finally:
            _clear_chain(env, chain)
            for name in roles.values():
                _run(["docker", "rm", "-f", name], env, check=False)
            _compose(env, project, "down", "--remove-orphans")
            for name in names.values():
                _remove_network(env, name)


def _psql(
    env: dict[str, str],
    network: str,
    source: str,
    host: str,
    password_file: str,
    mode: str,
    query: str = "select 1",
) -> subprocess.CompletedProcess[str]:
    if mode == "file":
        prelude = 'export PGPASSWORD="$(cat /run/secrets/password)"\n'
    elif mode == "wrong":
        prelude = "export PGPASSWORD=wrong\n"
    else:
        prelude = "unset PGPASSWORD\n"
    script = (
        prelude
        + 'psql "host='
        + host
        + " port=5432 user=sentica dbname=sentica"
        + ' sslmode=disable connect_timeout=3" -c '
        + "'"
        + query
        + "'\n"
    )
    return _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--ip",
            source,
            "--user",
            "999:999",
            "--entrypoint",
            "bash",
            "-v",
            password_file + ":/run/secrets/password:ro",
            _POSTGRES,
            "-c",
            script,
        ],
        env,
        check=False,
    )


def _secret_absent(blob: str, secret: str) -> bool:
    return secret not in blob


def test_deploy_postgres_accepts_only_the_password() -> None:
    """A read-only Postgres with no published port stays up and checks the password.

    The hba is the shipped file with the throwaway client address in place of
    the certificate proxy. A second address is rejected. The password is not
    in the logs or in docker inspect.
    """
    env = os.environ.copy()
    block = _free_slash24(env)
    subnet = _carve(block, 0)
    pg_ip = _host(subnet, 2)
    client_ip = _host(subnet, 3)
    other_ip = _host(subnet, 4)
    project = "sentica-r5pg-" + secrets.token_hex(3)
    network = project + "-net"
    name = project + "-pg"
    volume = project + "-data"
    password = secrets.token_hex(24)
    with tempfile.TemporaryDirectory(prefix=project + "-") as tmp:
        secret = Path(tmp) / "password"
        secret.write_text(password + "\n", encoding="utf-8")
        hba_path = Path(tmp) / "pg_hba.conf"
        hba_path.write_text(
            _text("docker/sentica/instance-pg_hba.conf").replace(
                "10.8.0.67", client_ip
            ),
            encoding="utf-8",
        )
        hba_path.chmod(0o644)
        ident_path = Path(tmp) / "pg_ident.conf"
        ident_path.write_text(
            _text("docker/sentica/instance-pg_ident.conf"),
            encoding="utf-8",
        )
        ident_path.chmod(0o644)
        _run(["docker", "rm", "-f", name], env, check=False)
        _run(["docker", "volume", "rm", "-f", volume], env, check=False)
        _remove_network(env, network)
        try:
            created = _run(["docker", "volume", "create", volume], env, check=False)
            assert created.returncode == 0, created.stderr
            chowned = _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    "root",
                    "--entrypoint",
                    "chown",
                    "-v",
                    volume + ":/var/lib/postgresql/data",
                    _POSTGRES,
                    "999:999",
                    "/var/lib/postgresql/data",
                ],
                env,
                check=False,
            )
            assert chowned.returncode == 0, chowned.stderr
            owned = _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    "root",
                    "--entrypoint",
                    "chown",
                    "-v",
                    str(secret) + ":/password",
                    _POSTGRES,
                    "999:999",
                    "/password",
                ],
                env,
                check=False,
            )
            assert owned.returncode == 0, owned.stderr
            mode = _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    "root",
                    "--entrypoint",
                    "chmod",
                    "-v",
                    str(secret) + ":/password",
                    _POSTGRES,
                    "400",
                    "/password",
                ],
                env,
                check=False,
            )
            assert mode.returncode == 0, mode.stderr
            _create_network(env, network, subnet, internal=True)
            started = _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--user",
                    "999:999",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=64m,uid=999,gid=999",
                    "--tmpfs",
                    "/var/run/postgresql:rw,noexec,nosuid,size=16m,uid=999,gid=999",
                    "-v",
                    volume + ":/var/lib/postgresql/data",
                    "-v",
                    str(secret) + ":/run/secrets/password:ro",
                    "-v",
                    str(hba_path) + ":/etc/sentica/pg_hba.conf:ro",
                    "-v",
                    str(ident_path) + ":/etc/sentica/pg_ident.conf:ro",
                    "-e",
                    "POSTGRES_PASSWORD_FILE=/run/secrets/password",
                    "-e",
                    "POSTGRES_USER=sentica",
                    "-e",
                    "POSTGRES_DB=sentica",
                    "-e",
                    "PGDATA=/var/lib/postgresql/data",
                    "-e",
                    "POSTGRES_INITDB_ARGS=--auth-local=peer --auth-host=scram-sha-256",
                    "--network",
                    network,
                    "--ip",
                    pg_ip,
                    _POSTGRES,
                    "-c",
                    "hba_file=/etc/sentica/pg_hba.conf",
                    "-c",
                    "ident_file=/etc/sentica/pg_ident.conf",
                ],
                env,
                check=False,
            )
            assert started.returncode == 0, started.stderr
            ready = False
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                probe = _run(
                    [
                        "docker",
                        "exec",
                        "-u",
                        "999:999",
                        name,
                        "pg_isready",
                        "-q",
                        "-U",
                        "sentica",
                    ],
                    env,
                    check=False,
                )
                if probe.returncode == 0:
                    ready = True
                    break
                time.sleep(0.5)
            if not ready:
                logs = _run(["docker", "logs", name], env, check=False)
                state = _run(
                    [
                        "docker",
                        "inspect",
                        "-f",
                        "{{.State.Status}} {{.State.ExitCode}}",
                        name,
                    ],
                    env,
                    check=False,
                )
                detail = (
                    logs.stdout + logs.stderr + state.stdout + state.stderr
                ).replace(password, "[redacted]")
                assert ready, detail
            accepted = _psql(env, network, client_ip, pg_ip, str(secret), "file")
            shown = _psql(
                env,
                network,
                client_ip,
                pg_ip,
                str(secret),
                "file",
                "SHOW hba_file",
            )
            missing = _psql(env, network, client_ip, pg_ip, str(secret), "none")
            wrong = _psql(env, network, client_ip, pg_ip, str(secret), "wrong")
            stranger = _psql(env, network, other_ip, pg_ip, str(secret), "file")
            logs = _run(["docker", "logs", name], env, check=False)
            inspected = _run(["docker", "inspect", name], env, check=False)
            state = _run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Running}} {{.State.ExitCode}}",
                    name,
                ],
                env,
                check=False,
            )
            ports = _run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{json .HostConfig.PortBindings}}",
                    name,
                ],
                env,
                check=False,
            )
            blob = logs.stdout + logs.stderr + inspected.stdout
            assert accepted.returncode == 0, accepted.stderr
            assert shown.returncode == 0, shown.stderr
            assert "/etc/sentica/pg_hba.conf" in shown.stdout
            assert missing.returncode != 0
            assert wrong.returncode != 0
            assert stranger.returncode != 0
            assert _secret_absent(blob, password)
            assert state.stdout.strip() == "true 0"
            assert ports.stdout.strip() in {"null", "{}", "[]"}
            assert "POSTGRES_HOST_AUTH_METHOD" not in inspected.stdout
            assert (
                "POSTGRES_INITDB_ARGS=--auth-local=peer --auth-host=scram-sha-256"
                in inspected.stdout
            )
            assert "--auth-host=trust" not in inspected.stdout
        finally:
            _run(["docker", "rm", "-f", name], env, check=False)
            _run(["docker", "volume", "rm", "-f", volume], env, check=False)
            _remove_network(env, network)
