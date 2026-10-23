"""The broker contract stays written down, and conclusion trust fails closed."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src import schemas
from src.exceptions import ValidationException

ROOT = Path(__file__).resolve().parents[2]

_BUILD = (
    'docker buildx build --build-arg SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" '
    "--provenance=false --sbom=false --no-cache "
    "--metadata-file image-metadata.json "
    "-o type=docker,dest=image.tar,rewrite-timestamp=true ."
)
_BUILDKIT = "moby/buildkit@sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea"

_PHRASES = (
    "POST /v3/workspaces/{workspace_id}/peers/{peer_id}/brief",
    "POST /v3/workspaces/{workspace_id}/peers/{peer_id}/search_memory",
    "POST /v3/workspaces/{workspace_id}/conclusions",
    "POST /v3/workspaces/{workspace_id}/sessions/{session_id}/messages",
    "sentica-memory",
    "5 minutes",
    "memory unavailable",
    "durable",
    "without memory context",
    "source_trust is required",
    "workspace_id",
)


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_integration_doc_names_the_broker_contract() -> None:
    text = _text("docs/INTEGRATION.md")
    for phrase in _PHRASES:
        assert phrase in text, phrase
    assert "\u2014" not in text


def test_release_notes_pin_the_base_images() -> None:
    notes = _text("docs/RELEASE-v0.2.0-sentica.md")
    dockerfile = _text("Dockerfile")
    digest = "sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641"
    uv_digest = (
        "sha256:816fdce3387ed2142e37d2e56e1b1b97ccc1ea87731ba199dc8a25c04e4997c5"
    )
    assert digest in dockerfile
    assert uv_digest in dockerfile
    assert dockerfile.count("@sha256:") >= 3
    assert digest in notes
    assert uv_digest in notes
    assert "SOURCE_DATE_EPOCH" in dockerfile
    assert "rewrite-timestamp" in notes
    assert "docker-container" in notes
    assert "IMAGE_DIGEST_PENDING" not in notes
    assert "git log -1 --format=%ct -- Dockerfile" in notes
    assert _BUILDKIT in notes
    assert _BUILD in notes
    workflow = _text(".github/workflows/unittest.yml")
    checker = _text("tests/sentica/check_image_digest.py")
    assert "--driver docker-container" in workflow
    assert "rewrite-timestamp=true" in workflow
    assert "--provenance=false" in workflow
    assert "git log -1 --format=%ct -- Dockerfile" in workflow
    assert "fetch-depth: 0" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert _BUILDKIT in workflow
    assert workflow.count(_BUILD) == 2
    assert "docs/image-digest.txt" in workflow
    assert "tests/sentica/check_image_digest.py" in workflow
    assert "containerimage.digest" in checker
    assert 'raise SystemExit("image digest drift")' in checker
    assert "docs/RELEASE-v0.2.0-sentica.md" not in checker
    image = "sha256:522b48cbd15d6ee59b99f0df376fc1e4cdaffd635e8aba66fd45114c6c35ae6d"
    current = "sha256:b55df6433b02d465b335351478a15aa8688af3c0d5bf374002b8591f4b271003"
    tagged = "sha256:65bfe6725581aae41a242f7a14f7f9a4b6deeba2d01bf85577d3286765c615e8"
    assert image in notes
    assert current not in notes
    assert re.search(r"sha256:[0-9a-f]{64}", notes)
    released = _text("docs/RELEASE-v0.2.1-sentica.md")
    assert "v0.2.1-sentica" in released
    assert "merge commit" in released
    assert tagged in released
    assert image in released
    assert "pin.mjs" in released
    assert "lines 6-9" in released
    assert "line 2" in released
    assert "docs/RELEASE-v0.2.1-sentica.md:35" in released
    assert "SENTICA_MEMORY_TAG" in released
    assert "SENTICA_MEMORY_DIGEST" in released
    assert "no migrate" in released
    assert "d4a8c1e27b90" in released
    assert "tool-call arguments" in released
    assert "HNSW index" in released
    assert "issue 18" in released
    assert "*.md" in released
    assert ".dockerignore" in released
    ignored = _text(".dockerignore")
    assert "*.md" in ignored
    assert "docs/**" in ignored
    digest_file = _text("docs/image-digest.txt")
    assert current in digest_file
    assert image not in digest_file
    assert "docs/RELEASE-v0.2.1-sentica.md" in digest_file
    assert "docs/RELEASE-v0.2.0-sentica.md" not in digest_file
    releases = _text("docs/RELEASES.md")
    unreleased = releases.split("## v0.2.1-sentica", 1)[0]
    assert current in unreleased
    assert tagged not in unreleased
    assert (
        "sha256:2c8bbf29429766bcf1e502d75d03f347b57ad050e48594ca18cd4b9d6895fa97"
        in unreleased
    )
    assert (
        "sha256:df87aff25b1da10125bfa9fe4ad12aa3a26531a9b30806c9912926b0bc6c890f"
        in unreleased
    )
    assert "## v0.2.1-sentica" in releases
    assert "v0.2.0-sentica" in releases
    assert "v0.1.0-sentica" in releases
    assert tagged in releases
    assert "no migrate" in releases
    assert "recorded at tag time" in releases
    assert "current expected digest" not in releases


def _metadata(digest: str) -> str:
    return json.dumps({"containerimage.digest": digest})


def _run_digest_check(
    tmp_path: Path, expected: str, first: str, second: str
) -> subprocess.CompletedProcess[str]:
    expected_path = tmp_path / "image-digest.txt"
    expected_path.write_text(
        "# Tagged versions record their digest in docs/RELEASE-v0.2.0-sentica.md.\n"
        + expected
        + "\n",
        encoding="utf-8",
    )
    first_path = tmp_path / "a.json"
    second_path = tmp_path / "b.json"
    first_path.write_text(_metadata(first), encoding="utf-8")
    second_path.write_text(_metadata(second), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests" / "sentica" / "check_image_digest.py"),
            str(expected_path),
            str(first_path),
            str(second_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_image_digest_check_fails_when_the_file_disagrees(tmp_path: Path) -> None:
    """Two identical exports still fail when the tracked digest differs."""
    built = "sha256:" + ("ab" * 32)
    recorded = "sha256:" + ("cd" * 32)
    result = _run_digest_check(tmp_path, recorded, built, built)
    assert result.returncode != 0
    assert "image digest drift" in result.stderr


def test_image_digest_check_fails_when_the_builds_differ(tmp_path: Path) -> None:
    """Reproducibility fails closed even if one export matches the file."""
    expected = "sha256:" + ("ab" * 32)
    other = "sha256:" + ("cd" * 32)
    result = _run_digest_check(tmp_path, expected, expected, other)
    assert result.returncode != 0
    assert "image digest drift" in result.stderr


def test_image_digest_check_accepts_two_matching_builds(tmp_path: Path) -> None:
    digest = "sha256:" + ("ab" * 32)
    result = _run_digest_check(tmp_path, digest, digest, digest)
    assert result.returncode == 0
    assert result.stderr == ""


async def test_missing_conclusion_trust_is_refused_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production raises, and removing that raise fails this test."""
    from src.crud.document import conclusion_trust, create_observations

    monkeypatch.setattr("src.sentica.runtime.production_enabled", lambda: False)
    assert conclusion_trust(None) == "owner"
    assert conclusion_trust("staff") == "staff"
    monkeypatch.setattr("src.sentica.runtime.production_enabled", lambda: True)
    with pytest.raises(ValidationException, match="source_trust is required"):
        conclusion_trust(None)
    assert conclusion_trust("external") == "external"

    embedded = False

    async def _embed(*_args: object, **_kwargs: object) -> list[list[float]]:
        nonlocal embedded
        embedded = True
        return []

    async def _ok(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("src.crud.document.embedding_client.simple_batch_embed", _embed)
    monkeypatch.setattr("src.crud.document.get_session", _ok)
    monkeypatch.setattr("src.crud.document.get_peer", _ok)
    monkeypatch.setattr("src.crud.document.reject_scope_observed", _ok)
    monkeypatch.setattr("src.crud.document.get_or_create_collection", _ok)
    with pytest.raises(ValidationException, match="source_trust is required"):
        await create_observations(
            cast(AsyncSession, object()),
            [
                schemas.ConclusionCreate(
                    content="likes tea",
                    observer_id="p1",
                    observed_id="p1",
                    session_id="s1",
                )
            ],
            "ws",
            already_checked=True,
        )
    assert embedded is False
