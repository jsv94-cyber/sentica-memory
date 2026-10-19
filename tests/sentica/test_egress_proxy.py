"""The shipped egress proxy allowlist, and a throwaway container that enforces it."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = ROOT / "docker" / "sentica" / "egress-proxy.yml"
_PROJECT = "sentica-r4fix-d760"
_NETWORK = "sentica-r4fix-d760"
_PROXY = "10.8.0.23"
_GATEWAY = "10.8.0.22"
_OTHER = "10.8.0.18"
_PUBLISH = Path("/tmp/sentica-r4fix-d760/publish.yml")
_CLIENT = (
    "python:3.13-slim-bookworm"
    + "@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641"
)
_SQUID = (
    "ubuntu/squid:6.13-25.04_beta"
    + "@sha256:3de2e64f0ca6efdac3e98557607dc0f23050037f3885016d5d5bfcf9950501b8"
)
_PROVIDER_HOSTS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
)
_HOST_LINE = re.compile(
    r"^acl provider_host dstdom_regex -i "
    + r"\^((?:[A-Za-z0-9-]+\\\.)+[A-Za-z0-9-]+)"
    + r"\\\.\?\$$"
)


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
    if "acl gateway_src src 10.8.0.22/32" not in lines:
        gaps.append("proxy does not accept only the gateway")
    if [line for line in lines if line.startswith("http_port ")] != ["http_port 8443"]:
        gaps.append("proxy listens on another port")
    return gaps


def test_egress_proxy_config_is_the_provider_allowlist() -> None:
    """The shipped config names the three hosts and denies everything else."""
    assert proxy_config_gaps(_text("docker/sentica/egress-proxy.conf")) == []
    opened = "http_access allow all\nhttp_access deny all\n"
    assert proxy_config_gaps(opened) != []


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


def test_only_the_gateway_env_sets_the_proxy() -> None:
    """Provider calls use the egress proxy. Other containers get no proxy."""
    env = _text("docker/sentica/gateway.env")
    assert "HTTPS_PROXY=http://10.8.0.23:8443" in env
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


def _teardown(env: dict[str, str]) -> None:
    _run(
        [
            "docker",
            "compose",
            "-p",
            _PROJECT,
            "-f",
            str(_COMPOSE),
            "-f",
            str(_PUBLISH),
            "down",
            "--remove-orphans",
        ],
        env,
        check=False,
    )
    _run(["docker", "network", "rm", _NETWORK], env, check=False)


def _proxy_status(
    env: dict[str, str], source: str, host: str, port: int, method: str
) -> str:
    script = (
        "import socket\n"
        f"s = socket.create_connection(({_PROXY!r}, 8443), 20)\n"
        "s.settimeout(20)\n"
        f"method = {method!r}\n"
        f"host = {host!r}\n"
        f"port = {port}\n"
        "if method == 'CONNECT':\n"
        "    req = f'CONNECT {host}:{port} HTTP/1.1\\r\\nHost: {host}:{port}\\r\\n\\r\\n'\n"
        "else:\n"
        "    req = f'GET http://{host}:{port}/ HTTP/1.1\\r\\nHost: {host}\\r\\n\\r\\n'\n"
        "s.sendall(req.encode())\n"
        "print(s.recv(1024).split(b'\\r\\n', 1)[0].decode('ascii', 'replace'))\n"
        "s.close()\n"
    )
    completed = _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            _NETWORK,
            "--ip",
            source,
            _CLIENT,
            "python",
            "-c",
            script,
        ],
        env,
        check=False,
    )
    if completed.returncode != 0:
        return completed.stderr.strip() or completed.stdout.strip()
    return completed.stdout.strip()


def test_egress_proxy_connects_an_allowed_host_and_refuses_another() -> None:
    """A throwaway proxy allows one provider host and refuses a different host.

    The client uses the gateway address. Another address on the bridge is
    refused as well. The containers publish on 127.0.0.1:38673 and use
    their own compose project.
    """
    _PUBLISH.parent.mkdir(parents=True, exist_ok=True)
    _PUBLISH.write_text(
        "services:\n  egress:\n    ports:\n      - 127.0.0.1:38673:8443\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["SENTICA_EGRESS_NETWORK"] = _NETWORK
    _teardown(env)
    try:
        created = _run(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--subnet",
                "10.8.0.16/28",
                "--gateway",
                "10.8.0.17",
                _NETWORK,
            ],
            env,
            check=False,
        )
        assert created.returncode == 0, created.stderr
        started = _run(
            [
                "docker",
                "compose",
                "-p",
                _PROJECT,
                "-f",
                str(_COMPOSE),
                "-f",
                str(_PUBLISH),
                "up",
                "-d",
            ],
            env,
            check=False,
        )
        assert started.returncode == 0, started.stderr
        allowed = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            allowed = _proxy_status(env, _GATEWAY, "api.openai.com", 443, "CONNECT")
            if allowed.startswith("HTTP/1.1 200"):
                break
            time.sleep(0.5)
        refused = _proxy_status(env, _GATEWAY, "example.com", 443, "CONNECT")
        plain = _proxy_status(env, _GATEWAY, "api.openai.com", 80, "GET")
        other = _proxy_status(env, _OTHER, "api.openai.com", 443, "CONNECT")
        logs = _run(
            [
                "docker",
                "compose",
                "-p",
                _PROJECT,
                "-f",
                str(_COMPOSE),
                "-f",
                str(_PUBLISH),
                "logs",
            ],
            env,
            check=False,
        )
        detail = logs.stdout + logs.stderr
        assert allowed.startswith("HTTP/1.1 200"), detail
        assert "403" in refused, refused
        assert "403" in plain, plain
        assert "403" in other, other
    finally:
        _teardown(env)
