"""Compose Redis host ports, ``.env.template``, and ``docs/CONFIG.md``.

A tracked compose file must not publish Redis on host port 6379. The
container port stays 6379, where Redis listens inside the container.
``.env.template`` must not point a Redis URL at port 6379.
``docs/CONFIG.md`` must not document ``SENTICA_REDIS_PORT`` as 6379.
An empty listing, no compose file, or a missing template or
``docs/CONFIG.md`` fails the test.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]

REDIS_HOST_PORT = 6379

_SENTICA_REDIS_PORT = re.compile(r"\b6379\b")


def _tracked_paths() -> list[str]:
    """Return tracked paths. An empty or failed listing is a hard failure."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError("git ls-files failed")
    paths = [item for item in result.stdout.decode("utf-8").split("\0") if item]
    if not paths:
        raise AssertionError("git ls-files returned no tracked files")
    return paths


def _is_compose(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return "compose" in name and name.endswith(
        (".yml", ".yaml", ".yml.example", ".yaml.example")
    )


def _split_port(text: str) -> list[str]:
    """Split a compose port spec on colons outside ``${}`` and ``[]``."""
    parts: list[str] = []
    buf: list[str] = []
    brace = 0
    bracket = 0
    for char in text:
        if char == "{":
            brace += 1
        elif char == "}":
            brace = max(0, brace - 1)
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        if char == ":" and brace == 0 and bracket == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    parts.append("".join(buf))
    return parts


def _host_number(host: str) -> int | None:
    """Return a baked host port. ``None`` means the file sets no default."""
    match = re.fullmatch(
        r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-(\d+))?\}",
        host,
    )
    if match is not None:
        default = match.group(1)
        if default is None:
            return None
        return int(default)
    if host.isdigit():
        return int(host)
    raise ValueError(host)


def _published_host(spec: str) -> int | None:
    """Return the host port from a short compose port mapping."""
    text = spec.strip()
    if text.endswith("/tcp") or text.endswith("/udp"):
        text = text.rsplit("/", 1)[0]
    parts = _split_port(text)
    if len(parts) == 1:
        if not parts[0].isdigit():
            raise ValueError(spec)
        # A single port publishes the container port on a random host port.
        return None
    if len(parts) == 2:
        host = parts[0]
        container = parts[1]
    elif len(parts) == 3:
        host = parts[1]
        container = parts[2]
    else:
        raise ValueError(spec)
    if not container.isdigit():
        raise ValueError(spec)
    return _host_number(host)


def _published_from_mapping(entry: dict[str, Any]) -> int | None:
    """Return the host port from a long compose port mapping."""
    if "published" not in entry:
        raise ValueError("missing published")
    published: Any = entry["published"]
    if isinstance(published, int):
        return published
    if not isinstance(published, str):
        raise ValueError("published is not a port")
    parts = _split_port(published.strip())
    if len(parts) == 1:
        return _host_number(parts[0])
    if len(parts) == 2:
        return _host_number(parts[1])
    raise ValueError(published)


def _redis_host_ports(path: Path) -> list[int]:
    """Return baked Redis host ports. Unreadable mappings fail closed."""
    loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("compose file is not a mapping")
    document = cast(dict[str, Any], loaded)
    services_raw: Any = document.get("services")
    if services_raw is None:
        return []
    if not isinstance(services_raw, dict):
        raise ValueError("services is not a mapping")
    services = cast(dict[str, Any], services_raw)
    found: list[int] = []
    for name, service_raw in services.items():
        if "redis" not in str(name):
            continue
        if not isinstance(service_raw, dict):
            raise ValueError(name + " is not a mapping")
        service = cast(dict[str, Any], service_raw)
        ports_raw: Any = service.get("ports")
        if ports_raw is None:
            continue
        if not isinstance(ports_raw, list):
            raise ValueError(name + " ports is not a list")
        ports = cast(list[Any], ports_raw)
        for entry in ports:
            if isinstance(entry, str):
                host = _published_host(entry)
            elif isinstance(entry, dict):
                host = _published_from_mapping(cast(dict[str, Any], entry))
            elif isinstance(entry, int):
                host = None
            else:
                raise ValueError(name + " has an unreadable port")
            if host is not None:
                found.append(host)
    return found


def _redis_url_port(line: str) -> int | None:
    """Return the port of a Redis URL on ``line``, if the line has one."""
    match = re.search(r"redis://([^\"'\s]+)", line)
    if match is None:
        return None
    body = match.group(1).split("?", 1)[0].split("/", 1)[0]
    if "@" in body:
        body = body.rsplit("@", 1)[1]
    if body.startswith("["):
        end = body.find("]")
        if end < 0 or not body.startswith("]:", end):
            return None
        port_text = body[end + 2 :]
    elif ":" in body:
        port_text = body.rsplit(":", 1)[1]
    else:
        return None
    if not port_text.isdigit():
        return None
    return int(port_text)


def _violations(paths: list[str]) -> list[str]:
    violations: list[str] = []
    compose_paths = [relative for relative in paths if _is_compose(relative)]
    if not compose_paths:
        violations.append("no tracked compose file")
    if ".env.template" not in paths:
        violations.append(".env.template is not tracked")
    if "docs/CONFIG.md" not in paths:
        violations.append("docs/CONFIG.md is not tracked")

    for relative in compose_paths:
        path = ROOT / relative
        if not path.is_file():
            violations.append(relative + ": tracked path is not a file")
            continue
        try:
            ports = _redis_host_ports(path)
        except (OSError, UnicodeError, yaml.YAMLError, ValueError):
            violations.append(relative + ": unreadable compose port mapping")
            continue
        for port in ports:
            if port == REDIS_HOST_PORT:
                violations.append(relative + ": published Redis host port " + str(port))

    if ".env.template" in paths:
        template = ROOT / ".env.template"
        if not template.is_file():
            violations.append(".env.template: tracked path is not a file")
        else:
            for line_no, line in enumerate(
                template.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if _redis_url_port(line) == REDIS_HOST_PORT:
                    violations.append(
                        ".env.template:"
                        + str(line_no)
                        + ": Redis URL uses host port "
                        + str(REDIS_HOST_PORT)
                    )

    if "docs/CONFIG.md" in paths:
        config_doc = ROOT / "docs" / "CONFIG.md"
        if not config_doc.is_file():
            violations.append("docs/CONFIG.md: tracked path is not a file")
        else:
            for line_no, line in enumerate(
                config_doc.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if "SENTICA_REDIS_PORT" in line and _SENTICA_REDIS_PORT.search(line):
                    violations.append(
                        "docs/CONFIG.md:"
                        + str(line_no)
                        + ": SENTICA_REDIS_PORT defaults to "
                        + str(REDIS_HOST_PORT)
                    )
    return violations


def test_compose_redis_host_port_is_not_6379() -> None:
    """Fail when compose, .env.template, or docs/CONFIG.md defaults Redis to 6379."""
    violations = _violations(_tracked_paths())
    assert not violations, "\n".join(violations)
