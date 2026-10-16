"""Run-level taint. A run that saw an external or unstamped input taints every output."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import or_, select

_RUN_TRUST: ContextVar[str | None] = ContextVar("sentica_run_trust", default=None)


def trust_is_external(value: str | None) -> bool:
    """Missing trust is external. Only an explicit non-external label is internal."""
    return value is None or value == "external"


def current_run_trust() -> str | None:
    """The trust label for conclusions created by the active deriver or specialist run."""
    return _RUN_TRUST.get()


@contextmanager
def bind_run_trust(label: str) -> Generator[None]:
    """Mark every conclusion saved inside this block with `label`."""
    token = _RUN_TRUST.set(label)
    try:
        yield
    finally:
        _RUN_TRUST.reset(token)


def messages_run_trust(trusts: list[str | None]) -> str:
    """One external or missing message taints the whole batch."""
    if any(trust_is_external(value) for value in trusts):
        return "external"
    for value in trusts:
        if isinstance(value, str) and value:
            return value
    return "owner"


async def collection_run_trust(
    workspace_name: str, *, observer: str, observed: str
) -> str:
    """External when this run's collection or messages include a tainted input."""
    from src import models
    from src.dependencies import tracked_db

    trust = models.Document.internal_metadata["source_trust"].astext
    async with tracked_db("taint.context", read_only=True) as db:
        tainted_document = await db.scalar(
            select(models.Document.id)
            .where(models.Document.workspace_name == workspace_name)
            .where(models.Document.observer == observer)
            .where(models.Document.observed == observed)
            .where(models.Document.deleted_at.is_(None))
            .where(or_(trust.is_(None), trust == "external"))
            .limit(1)
        )
        if tainted_document is not None:
            return "external"
        tainted_message = await db.scalar(
            select(models.Message.id)
            .where(models.Message.workspace_name == workspace_name)
            .where(
                or_(
                    models.Message.source_trust.is_(None),
                    models.Message.source_trust == "external",
                )
            )
            .limit(1)
        )
    if tainted_message is not None:
        return "external"
    return "owner"
