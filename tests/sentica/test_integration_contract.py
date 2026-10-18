"""The broker contract stays written down, and conclusion trust fails closed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

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
    workflow = _text(".github/workflows/unittest.yml")
    assert "--driver docker-container" in workflow
    assert "rewrite-timestamp=true" in workflow
    assert "--provenance=false" in workflow
    image = "sha256:35a6bf1f11b39428d31a9e06b81dc0279ab7ce1d5180879690e28fc59db7f055"
    assert image in notes
    assert re.search(r"sha256:[0-9a-f]{64}", notes)


def test_production_conclusion_requires_source_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.crud.document import conclusion_trust
    from src.exceptions import ValidationException

    monkeypatch.setattr("src.sentica.runtime.production_enabled", lambda: False)
    assert conclusion_trust(None) == "owner"
    assert conclusion_trust("staff") == "staff"
    monkeypatch.setattr("src.sentica.runtime.production_enabled", lambda: True)
    with pytest.raises(ValidationException, match="source_trust is required"):
        conclusion_trust(None)
    assert conclusion_trust("external") == "external"
