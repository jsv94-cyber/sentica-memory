"""M0 acceptance: pin, map citations, local compose, and CI triggers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]

# file, line, snippet that line must contain. The map must cite file:line.
REQUIRED_CITATIONS: tuple[tuple[str, int, str], ...] = (
    ("src/db.py", 75, "create_async_engine"),
    ("src/db.py", 347, "SessionLocal"),
    ("src/db.py", 369, "ReadSessionLocal"),
    ("src/dependencies.py", 68, "tracked_db"),
    ("src/security.py", 31, "class JWTParams"),
    ("src/security.py", 106, "def verify_jwt"),
    ("src/security.py", 216, "USE_AUTH"),
    ("src/models.py", 105, "class Workspace"),
    ("src/models.py", 138, "class Peer"),
    ("src/models.py", 175, "class Session"),
    ("src/models.py", 214, "class Message"),
    ("src/models.py", 358, "class Collection"),
    ("src/models.py", 406, "class Document"),
    ("src/models.py", 655, "class QueueItem"),
    ("src/utils/scopes.py", 33, "SCOPE_PEER_PREFIX"),
    ("src/routers/conclusions.py", 22, "APIRouter"),
    ("src/crud/representation.py", 84, "class RepresentationManager"),
    ("src/crud/peer_card.py", 22, "get_peer_card"),
    ("src/utils/summarizer.py", 96, "SUMMARIES_KEY"),
    ("src/dreamer/orchestrator.py", 108, "def run_dream"),
    ("src/deriver/queue_manager.py", 549, "def polling_loop"),
    ("src/deriver/consumer.py", 47, "def process_item"),
    ("src/llm/api.py", 147, "def honcho_llm_call"),
    ("src/deriver/deriver.py", 162, "honcho_llm_call"),
    ("src/dialectic/core.py", 594, "honcho_llm_call"),
    ("src/utils/summarizer.py", 289, "honcho_llm_call"),
    ("src/dreamer/specialists.py", 416, "honcho_llm_call"),
    ("src/embedding_client.py", 352, "embeddings.create"),
    ("src/deriver/prompts.py", 47, "minimal_deriver_prompt"),
    ("src/dialectic/prompts.py", 119, "agent_system_prompt"),
    ("src/dialectic/prompts.py", 350, "workspace_agent_system_prompt"),
    ("src/utils/summarizer.py", 149, "short_summary_prompt"),
    ("src/utils/summarizer.py", 189, "long_summary_prompt"),
    ("src/dreamer/specialists.py", 637, "deductive reasoning"),
    ("src/dreamer/specialists.py", 771, "inductive reasoning"),
    ("src/utils/queue_payload.py", 28, "content: str"),
)

CITATION_RE = re.compile(
    r"(?P<path>(?:src|docker|scripts|migrations|database)/[A-Za-z0-9_./-]+\.(?:py|sh|sql)|alembic\.ini):(?P<line>\d+)"
)

PIN_TAG = "v3.3.0"
PIN_COMMIT = "608aec8a928da13c540262ed980c569448fb53ab"


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _load_yaml(relative: str) -> dict[str, Any]:
    loaded = yaml.safe_load(_text(relative))
    assert isinstance(loaded, dict)
    return cast(dict[str, Any], loaded)


def _on_block(document: dict[str, Any]) -> dict[str, Any]:
    # PyYAML 1.1 parses the bare key `on` as boolean true.
    raw: Any = document.get("on")
    if raw is None:
        raw = cast(dict[Any, Any], document).get(True)
    assert isinstance(raw, dict)
    return cast(dict[str, Any], raw)


def test_pin_is_recorded() -> None:
    progress = _text("PROGRESS.md")
    assert PIN_TAG in progress
    assert PIN_COMMIT in progress
    assert "upstream" in progress
    notice = _text("NOTICE")
    assert "Affero" in notice
    assert "plastic-labs/honcho" in notice
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in _text("LICENSE")
    assert "FORK_CHANGES.md" in _text("FORK_CHANGES.md")
    assert "docker-compose.yml" in _text("FORK_CHANGES.md")


def test_progress_records_the_local_hindsight_branch() -> None:
    progress = _text("PROGRESS.md")
    assert "No `spike-hindsight` branch was created." not in progress
    assert "A local-only `spike-hindsight` branch exists." in progress
    assert "The bake-off was cancelled 2026-10-17." in progress


def test_new_docs_have_no_em_dash() -> None:
    for relative in (
        "docs/MAP.md",
        "docs/INSTANCE.md",
        "NOTICE",
        "FORK_CHANGES.md",
        "PROGRESS.md",
        "docs/CONFIG.md",
        "eval/BASELINE.md",
        "docs/DEPLOY.md",
        "docs/UPSTREAM_SYNC.md",
        "docs/RELEASES.md",
        "docs/image-digest.txt",
        "docs/INTEGRATION.md",
        "docs/RELEASE-v0.2.0-sentica.md",
        ".review/M2-summary.md",
        ".review/R3-summary.md",
        "docker-compose.yml",
        "docker-compose.sentica.yml",
        ".review/M1-summary.md",
    ):
        assert "\u2014" not in _text(relative), relative


def test_map_citations_resolve() -> None:
    map_text = _text("docs/MAP.md")
    found = CITATION_RE.findall(map_text)
    assert found, "docs/MAP.md has no file:line citations"
    for relative, line_text in found:
        path = ROOT / relative
        assert path.is_file(), relative
        line_no = int(line_text)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 1 <= line_no <= len(lines), f"{relative}:{line_no} out of range"

    for relative, line_no, snippet in REQUIRED_CITATIONS:
        needle = f"{relative}:{line_no}"
        assert needle in map_text, needle
        source = (ROOT / relative).read_text(encoding="utf-8").splitlines()
        assert snippet in source[line_no - 1]


def test_map_covers_required_topics() -> None:
    map_text = _text("docs/MAP.md").lower()
    for topic in (
        "workspace",
        "peer",
        "session",
        "scope",
        "conclusion",
        "representation",
        "summar",
        "dream",
        "queue",
        "prompt",
        "auth",
        "embedding",
    ):
        assert topic in map_text


def test_compose_starts_required_services() -> None:
    document = _load_yaml("docker-compose.yml")
    services = cast(dict[str, Any], document["services"])
    for name in ("api", "deriver", "database", "redis"):
        assert name in services
    database = cast(dict[str, Any], services["database"])
    assert database["image"] == "pgvector/pgvector:pg15"
    redis = cast(dict[str, Any], services["redis"])
    command = cast(list[str], redis["command"])
    assert "--appendonly" in command
    assert "no" in command
    assert "--save" in command
    assert "volumes" not in redis
    assert redis["tmpfs"] == ["/data"]
    for name in ("api", "deriver"):
        service = cast(dict[str, Any], services[name])
        tmpfs = cast(list[str], service["tmpfs"])
        assert "/tmp" in tmpfs
        environment = cast(list[str], service["environment"])
        assert "SENTICA_MODE=test" in environment
    assert "redis-data" not in _text("docker-compose.yml")
    ignored = set(_text(".gitignore").splitlines())
    assert "/docker-compose.yml" not in ignored
    assert "/compose.yml" not in ignored


def test_ci_runs_the_suite_on_every_pull_request() -> None:
    workflow = _load_yaml(".github/workflows/unittest.yml")
    on_block = _on_block(workflow)
    pull_request = cast(dict[str, Any], on_block["pull_request"])
    push = cast(dict[str, Any], on_block["push"])
    assert "paths" not in pull_request
    assert "paths" not in push
    jobs = cast(dict[str, Any], workflow["jobs"])
    test_python = cast(dict[str, Any], jobs["test-python"])
    assert "if" not in test_python
    rendered = _text(".github/workflows/unittest.yml")
    assert "uv run pytest" in rendered
    assert "SENTICA_MODE: test" in rendered


def test_external_deploy_workflows_do_not_run_automatically() -> None:
    for relative in (
        ".github/workflows/push-gcp-registry-prod.yml",
        ".github/workflows/push-gcp-registry-staging.yml",
        ".github/workflows/unified-tests.yml",
        ".github/workflows/live-llm-tests.yml",
    ):
        document = _load_yaml(relative)
        on_block = _on_block(document)
        assert "push" not in on_block, relative
        assert "pull_request" not in on_block, relative
        assert "workflow_dispatch" in on_block
    for relative in (
        ".github/workflows/push-gcp-registry-prod.yml",
        ".github/workflows/push-gcp-registry-staging.yml",
        ".github/workflows/start-fly-runner.yml",
        ".github/workflows/unified-tests.yml",
        ".github/workflows/live-llm-tests.yml",
    ):
        assert "if: false" in _text(relative), relative
