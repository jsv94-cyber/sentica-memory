"""Allow-list for workspaces.internal_metadata.

Dream digests and deletion records live in their own tables. A workspace
row may only keep the keys named here. Anything else is refused.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from src.exceptions import ValidationException

# The broker sets this in ensurePartition. Docs: docs/INTEGRATION.md.
CEILING_KEY = "ceiling"
CEILING_VALUES: frozenset[str] = frozenset(
    {"general", "internal", "restricted", "personal"}
)
_RANK: dict[str, int] = {
    "general": 1,
    "internal": 2,
    "restricted": 3,
    "personal": 4,
}

# Configuration keys that may live on the workspace row. Digests and
# deletion records are not configuration and are not in this set.
ALLOWED_WORKSPACE_INTERNAL_METADATA_KEYS: frozenset[str] = frozenset({CEILING_KEY})


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


def workspace_ceiling(metadata: object) -> str:
    """Return the ceiling. Missing, unknown, and malformed values are general."""
    if not isinstance(metadata, dict):
        return "general"
    raw = cast(dict[object, object], metadata)
    if CEILING_KEY not in raw:
        return "general"
    value = raw[CEILING_KEY]
    if isinstance(value, str) and value in CEILING_VALUES:
        return value
    return "general"


def ceiling_holds_restricted(ceiling: str) -> bool:
    """True when this ceiling may store a Restricted pattern."""
    return _RANK.get(ceiling, 0) >= _RANK["restricted"]


async def ceiling_for_workspace(
    workspace_name: str, db: AsyncSession | None = None
) -> str:
    """Read one workspace ceiling. A missing row is general."""
    from sqlalchemy import select

    from src import models

    async def _read(session: AsyncSession) -> str:
        stored = await session.scalar(
            select(models.Workspace.internal_metadata).where(
                models.Workspace.name == workspace_name
            )
        )
        return workspace_ceiling(stored)

    if db is not None:
        return await _read(db)
    from src.dependencies import tracked_db

    async with tracked_db("workspace.ceiling", read_only=True) as session:
        return await _read(session)
