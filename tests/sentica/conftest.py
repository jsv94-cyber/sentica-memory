"""Keep the two-instance proof out of the default suite.

The file is not collected unless SENTICA_M1_STACK=1. It is not marked skip.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    del config
    if collection_path.name != "test_m1_stack.py":
        return None
    if os.environ.get("SENTICA_M1_STACK") == "1":
        return None
    return True
