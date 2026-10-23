"""Tracked files must not publish Redis or Postgres on their well-known ports.

The host side of a port mapping is the collision. Container ports stay on
6379 and 5432 because that is where those servers listen inside the
container. ``SENTICA_REDIS_PORT`` and ``SENTICA_POSTGRES_PORT`` still
override the host side.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]

# Well-known host ports. A published default of either one fails the build.
REDIS_HOST_PORT = 6379
POSTGRES_HOST_PORT = 5432
_FORBIDDEN_HOST_PORTS = frozenset({REDIS_HOST_PORT, POSTGRES_HOST_PORT})

_LOCAL_HOST_PORT = re.compile(r"(?i)(?:localhost|127\.0\.0\.1):(?P<port>6379|5432)\b")
_PUBLISHED_PAIR = re.compile(r"(?<![\w.])(?P<port>6379|5432):(?P=port)\b")
_SHELL_DEFAULT = re.compile(r":-(?P<port>6379|5432)\b")
_PROSE_DEFAULT = re.compile(
    "(?i)(?:"
    + r"\bdefault(?:s|ed)?\s+(?:to\s+)?(?:8000,\s*)?(?P<port>6379|5432)\b"
    + "|8000/5432/6379"
    + r"|\(port (?P<paren>6379|5432)\)"
    + r"|,\s*port (?P<comma>6379|5432)\b"
    + ")"
)
_PORT_ASSIGNMENT = re.compile(
    r"(?i)^[ \t#]*[A-Za-z0-9_]*(?:REDIS_PORT|POSTGRES_PORT|DB_PORT)\s*=\s*"
    + r"(?P<port>6379|5432)\b"
)


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


def _kind(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1].lower()
    if "compose" in name and name.endswith(
        (".yml", ".yaml", ".yml.example", ".yaml.example")
    ):
        return "compose"
    if name in {
        ".env.template",
        ".env.example",
        "config.toml.example",
    } or name.endswith(
        (".env.template", ".env.example", ".env.sample", ".toml.example")
    ):
        return "env"
    if name.endswith((".md", ".mdx")):
        return "doc"
    return None


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


def _compose_host_ports(path: Path) -> list[int]:
    """Return every baked host port in ``path``. Unreadable mappings fail."""
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


def _line_violations(relative: str, text: str) -> list[str]:
    violations: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if (
            _LOCAL_HOST_PORT.search(line)
            or _PUBLISHED_PAIR.search(line)
            or _SHELL_DEFAULT.search(line)
            or _PROSE_DEFAULT.search(line)
            or _PORT_ASSIGNMENT.search(line)
        ):
            violations.append(
                relative
                + ":"
                + str(line_no)
                + ": defaults Redis to "
                + str(REDIS_HOST_PORT)
                + " or Postgres to "
                + str(POSTGRES_HOST_PORT)
            )
    return violations


def _violations(paths: list[str]) -> list[str]:
    violations: list[str] = []
    saw_compose = False
    saw_env = False
    saw_doc = False
    for relative in paths:
        kind = _kind(relative)
        if kind is None:
            continue
        path = ROOT / relative
        if not path.is_file():
            violations.append(f"{relative}: tracked path is not a file")
            continue
        text = path.read_text(encoding="utf-8")
        if kind == "compose":
            saw_compose = True
            try:
                ports = _compose_host_ports(path)
            except (OSError, UnicodeError, yaml.YAMLError, ValueError):
                violations.append(f"{relative}: unreadable compose port mapping")
                continue
            for port in ports:
                if port in _FORBIDDEN_HOST_PORTS:
                    violations.append(f"{relative}: published host port {port}")
        elif kind == "env":
            saw_env = True
        else:
            saw_doc = True
        violations.extend(_line_violations(relative, text))
    if not saw_compose or not saw_env or not saw_doc:
        violations.append(
            "scanner did not see a compose file, an env example, and a doc"
        )
    return violations


def test_tracked_files_do_not_default_host_ports_to_6379_or_5432() -> None:
    """Fail when a compose file, env example, or doc publishes 6379 or 5432."""
    violations = _violations(_tracked_paths())
    assert not violations, "\n".join(violations)
