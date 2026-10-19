#!/usr/bin/env python3
"""Fail unless two clean image exports match the tracked manifest digest."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import cast

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def expected_digest(path: Path) -> str:
    """Return the single manifest digest in ``path``.

    Blank lines and ``#`` comments are ignored. A tagged release keeps its
    digest in the release notes. This file is only the current expected value.
    """
    found: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            found.append(line)
    if len(found) != 1 or _DIGEST.fullmatch(found[0]) is None:
        raise SystemExit("image digest file must contain one manifest digest")
    return found[0]


def manifest_digest(path: Path) -> str:
    """Return ``containerimage.digest`` from a BuildKit metadata file."""
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("image metadata is not an object")
    digest = cast(dict[str, object], payload).get("containerimage.digest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise SystemExit("image metadata has no manifest digest")
    return digest


def main(argv: list[str]) -> None:
    """Compare two export metadata files with the tracked digest."""
    if len(argv) != 4:
        raise SystemExit("usage: check_image_digest.py EXPECTED METADATA_A METADATA_B")
    expected = expected_digest(Path(argv[1]))
    first = manifest_digest(Path(argv[2]))
    second = manifest_digest(Path(argv[3]))
    print(first)
    print(second)
    print(expected)
    if first != second or first != expected:
        raise SystemExit("image digest drift")


if __name__ == "__main__":
    main(sys.argv)
