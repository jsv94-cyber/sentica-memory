"""Allow-list for workspaces.internal_metadata.

Dream digests and deletion records live in their own tables. A workspace
row may only keep the keys named here. Anything else is refused.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import event

from src.exceptions import ValidationException

# Configuration keys that may live on the workspace row. Digests and
# deletion records are not configuration and are not in this set.
ALLOWED_WORKSPACE_INTERNAL_METADATA_KEYS: frozenset[str] = frozenset()


def check_workspace_metadata(metadata: object) -> None:
    """Refuse a workspace metadata document that carries an unknown key."""
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        raise ValidationException("workspaces.internal_metadata must be an object")
    raw = cast(dict[object, object], metadata)
    unknown = sorted(
        key
        for key in raw
        if isinstance(key, str) and key not in ALLOWED_WORKSPACE_INTERNAL_METADATA_KEYS
    )
    if unknown:
        raise ValidationException(
            "workspaces.internal_metadata has a key that is not allowed: "
            + ", ".join(unknown)
        )


def install_workspace_metadata_guard(workspace_cls: type[Any]) -> None:
    """Reject unknown keys on insert and update."""

    @event.listens_for(workspace_cls, "before_insert")
    @event.listens_for(workspace_cls, "before_update")
    def _check(  # pyright: ignore[reportUnusedFunction]
        _mapper: object, _connection: object, target: object
    ) -> None:
        check_workspace_metadata(getattr(target, "internal_metadata", None))
