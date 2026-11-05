"""The ``subject_ref`` a message can carry in its metadata.

The rule is the one the subject-changed route applies to a conclusion's
``subject_ref``: a string with something in it. A message that carries a
malformed value is refused. It is never stored without one.
"""

from __future__ import annotations

from typing import Any

KEY = "subject_ref"


def validate_subject_ref(value: object) -> str:
    """Return ``value`` unchanged when it is a non-blank string, else raise."""
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError("subject_ref must be a non-empty string")
    return value


def check_metadata_subject_ref(metadata: dict[str, Any] | None) -> None:
    """Refuse a malformed ``subject_ref`` key. An absent key passes."""
    if metadata is not None and KEY in metadata:
        validate_subject_ref(metadata[KEY])
