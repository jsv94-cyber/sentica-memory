"""Manual live eval. Not collected by the default pytest suite."""

from __future__ import annotations

import os

import httpx


def test_live_gateway_url_answers() -> None:
    """The dispatch job supplies SENTICA_LIVE_EVAL_GATEWAY_URL."""
    url = os.environ.get("SENTICA_LIVE_EVAL_GATEWAY_URL", "").strip()
    assert url.startswith(("http://", "https://"))
    response = httpx.get(url, timeout=30.0)
    assert response.status_code < 500
