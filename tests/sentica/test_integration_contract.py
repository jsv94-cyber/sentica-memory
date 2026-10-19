"""The broker contract stays written down, and conclusion trust fails closed."""

from __future__ import annotations

import re
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
    assert "--driver docker-container" in workflow
    assert "rewrite-timestamp=true" in workflow
    assert "--provenance=false" in workflow
    assert "git log -1 --format=%ct -- Dockerfile" in workflow
    assert "fetch-depth: 0" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert _BUILDKIT in workflow
    assert workflow.count(_BUILD) == 2
    assert "containerimage.digest" in workflow
    assert "image digest drift" in workflow
    image = "sha256:8f604726eddf4315f8f1ae19cb4ae10155b417cdbc09a7ca9e1994ea8ccd557f"
    assert image in notes
    assert re.search(r"sha256:[0-9a-f]{64}", notes)


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
