"""M1 carry-over checks that must keep failing if the behavior is removed."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

# Current Node 24 majors confirmed on 2026-10-13:
# actions/checkout v7.0.1, actions/setup-python v7.0.0, astral-sh/setup-uv v10.3.0.
PINNED_MAJORS = {
    "actions/checkout": 7,
    "actions/setup-python": 7,
    "astral-sh/setup-uv": 10,
}
_USES = re.compile(
    r"uses:\s*(actions/checkout|actions/setup-python|astral-sh/setup-uv)@v(\d+)"
)


def test_workflows_pin_ubuntu_2404_and_node24_actions() -> None:
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files
    seen = {name: False for name in PINNED_MAJORS}
    for path in files:
        text = path.read_text()
        assert "ubuntu-latest" not in text, path.name
        for match in _USES.finditer(text):
            action = match.group(1)
            major = int(match.group(2))
            assert major >= PINNED_MAJORS[action], path.name
            seen[action] = True
    assert all(seen.values())
