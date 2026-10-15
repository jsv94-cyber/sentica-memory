"""Daily and weekly dreams, one workspace at a time."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any, cast

from nanoid import generate as generate_nanoid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from src import models
from src.config import settings
from src.dependencies import tracked_db
from src.llm.types import LLMTelemetryContext
from src.sentica.safety import banned_category, note_drop
from src.sentica.workspace_scope import reset_workspace, set_workspace

logger = logging.getLogger(__name__)

_TOKENS: dict[str, int] = {}


def _tenant() -> str:
    raw = os.environ.get("SENTICA_TENANT_ID", "").strip()
    return raw or "unset"


def note_tokens(count: int) -> None:
    """Count model tokens for this dream run, per tenant."""
    if count <= 0:
        return
    tenant = _tenant()
    _TOKENS[tenant] = _TOKENS.get(tenant, 0) + count


def tokens_used(tenant: str | None = None) -> int:
    """Tokens this process has counted for dream runs."""
    key = tenant if tenant is not None else _tenant()
    return _TOKENS.get(key, 0)


def reset_tokens() -> None:
    """Clear the dream token counter. Tests call this between cases."""
    _TOKENS.clear()


def synthesis_enabled(configuration: dict[str, Any] | None) -> bool:
    """Synthesis is on unless this workspace turned it off."""
    if not configuration:
        return True
    return configuration.get("sentica_synthesis") is not False


def _records(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw = metadata.get("dreams")
    if not isinstance(raw, list):
        return []
    records: list[dict[str, Any]] = []
    for item in cast("list[object]", raw):
        if isinstance(item, dict):
            records.append(cast("dict[str, Any]", item))
    return records


async def set_synthesis_enabled(
    db: AsyncSession, workspace_name: str, enabled: bool
) -> None:
    """Turn synthesis on or off. Earlier dreams stay."""
    workspace = await db.scalar(
        select(models.Workspace).where(models.Workspace.name == workspace_name)
    )
    if workspace is None:
        from src.exceptions import ResourceNotFoundException

        raise ResourceNotFoundException("Workspace not found")
    configuration = dict(workspace.configuration or {})
    configuration["sentica_synthesis"] = enabled
    workspace.configuration = configuration
    flag_modified(workspace, "configuration")
    await db.commit()


async def _complete(workspace_name: str, purpose: str, prompt: str) -> tuple[str, int]:
    from src.llm.api import honcho_llm_call

    response = await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt=prompt,
        max_tokens=400,
        telemetry=LLMTelemetryContext(
            workspace_name=workspace_name,
            call_purpose=purpose,
            parent_category="dream",
        ),
    )
    note_tokens(response.input_tokens + response.output_tokens)
    return str(response.content), response.input_tokens + response.output_tokens


async def _save(
    workspace_name: str,
    *,
    kind: str,
    period: str,
    content: str,
    source_ids: list[str],
    tokens: int,
    now: datetime,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": generate_nanoid(),
        "kind": kind,
        "period": period,
        "content": content,
        "source_ids": source_ids,
        "workspace_name": workspace_name,
        "tokens": tokens,
        "created_at": now.isoformat(),
    }
    async with tracked_db("dreams.save") as db:
        workspace = await db.scalar(
            select(models.Workspace).where(models.Workspace.name == workspace_name)
        )
        if workspace is None:
            return record
        metadata = dict(workspace.internal_metadata or {})
        dreams = _records(metadata)
        dreams.append(record)
        metadata["dreams"] = dreams
        workspace.internal_metadata = metadata
        flag_modified(workspace, "internal_metadata")
        await db.commit()
    return record


async def run_dreams(
    workspace_name: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Write today's digest, then a weekly insight from digests only.

    The model call happens with no database session open. A workspace with
    synthesis off is left unchanged.
    """
    clock = now or datetime.now(UTC)
    daily_period = clock.date().isoformat()
    year, week, _day = clock.isocalendar()
    weekly_period = f"{year}-W{week:02d}"

    async with tracked_db("dreams.read", read_only=True) as db:
        workspace = await db.scalar(
            select(models.Workspace).where(models.Workspace.name == workspace_name)
        )
        if workspace is None or not synthesis_enabled(workspace.configuration):
            return []
        existing = _records(dict(workspace.internal_metadata or {}))
        rows = (
            await db.execute(
                select(models.Document.id, models.Document.content)
                .where(models.Document.workspace_name == workspace_name)
                .where(models.Document.deleted_at.is_(None))
                .where(models.Document.superseded_by.is_(None))
                .order_by(models.Document.created_at.desc())
                .limit(40)
            )
        ).all()
    conclusions = [(str(row.id), str(row.content)) for row in rows]
    created: list[dict[str, Any]] = []
    digests = [item for item in existing if item.get("kind") == "daily"]

    if conclusions and not any(item.get("period") == daily_period for item in digests):
        lines = "\n".join(f"- {doc_id}: {text}" for doc_id, text in conclusions)
        prompt = (
            "Write a daily digest for this workspace only.\n"
            + "Do not infer health, religion, politics, sex, immigration, "
            + "crime, or pay.\n"
            + lines
        )
        text, spent = await _complete(workspace_name, "dream.daily", prompt)
        if banned_category(text) is not None:
            logger.info("Dropped a conclusion category=%s", banned_category(text))
            note_drop()
        elif text:
            record = await _save(
                workspace_name,
                kind="daily",
                period=daily_period,
                content=text,
                source_ids=[doc_id for doc_id, _text in conclusions],
                tokens=spent,
                now=clock,
            )
            digests.append(record)
            created.append(record)

    if digests and not any(
        item.get("kind") == "weekly" and item.get("period") == weekly_period
        for item in existing
    ):
        lines = "\n".join(
            "- " + str(item.get("id")) + ": " + str(item.get("content"))
            for item in digests
        )
        prompt = (
            "Write a weekly insight from these daily digests only.\n"
            + "Do not use raw messages.\n"
            + lines
        )
        text, spent = await _complete(workspace_name, "dream.weekly", prompt)
        if banned_category(text) is not None:
            logger.info("Dropped a conclusion category=%s", banned_category(text))
            note_drop()
        elif text:
            record = await _save(
                workspace_name,
                kind="weekly",
                period=weekly_period,
                content=text,
                source_ids=[str(item.get("id")) for item in digests],
                tokens=spent,
                now=clock,
            )
            created.append(record)
    return created


async def dreams_due() -> None:
    """Run the dream pass for each workspace. Failures stay in this process."""
    try:
        async with tracked_db("dreams.workspaces", read_only=True) as db:
            names = [
                str(name)
                for name in (
                    await db.scalars(
                        select(models.Workspace.name).order_by(models.Workspace.name)
                    )
                ).all()
            ]
        for name in names:
            token = set_workspace(name)
            try:
                await run_dreams(name)
            except Exception:
                logger.exception("dream pass failed for workspace %s", name)
            finally:
                reset_workspace(token)
    except Exception:
        logger.exception("dream pass failed")
