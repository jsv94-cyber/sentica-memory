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


_WEEKLY_WINDOW = 7
_DAILY_HISTORY = 28
_WEEKLY_HISTORY = 12


def inference_enabled(configuration: dict[str, Any] | None) -> bool:
    """Synthesis off stops dreams, including the upstream dreamer.

    A missing workspace configuration is off. An empty configuration is on.
    """
    if configuration is None:
        return False
    if not synthesis_enabled(configuration):
        return False
    if not configuration:
        return True
    dream: Any = configuration.get("dream")
    if not isinstance(dream, dict):
        return True
    enabled: Any = cast(dict[str, Any], dream).get("enabled")
    return enabled is not False


async def set_synthesis_enabled(
    db: AsyncSession, workspace_name: str, enabled: bool
) -> None:
    """Turn synthesis on or off, including the upstream dreamer."""
    workspace = await db.scalar(
        select(models.Workspace).where(models.Workspace.name == workspace_name)
    )
    if workspace is None:
        from src.exceptions import ResourceNotFoundException

        raise ResourceNotFoundException("Workspace not found")
    stored: Any = workspace.configuration or {}
    configuration: dict[str, Any] = (
        dict(cast(dict[str, Any], stored)) if isinstance(stored, dict) else {}
    )
    configuration["sentica_synthesis"] = enabled
    dream_raw: Any = configuration.get("dream")
    dream: dict[str, Any] = (
        dict(cast(dict[str, Any], dream_raw)) if isinstance(dream_raw, dict) else {}
    )
    dream["enabled"] = enabled
    configuration["dream"] = dream
    workspace.configuration = configuration
    flag_modified(workspace, "configuration")
    await db.commit()
    from src.cache.client import safe_cache_delete
    from src.crud.workspace import workspace_cache_key

    await safe_cache_delete(workspace_cache_key(workspace_name))


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


def _as_record(row: models.DreamDigest) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "period": row.period,
        "content": row.content,
        "source_ids": list(row.source_ids or []),
        "workspace_name": row.workspace_name,
        "tokens": row.tokens,
        "external": bool(row.external),
        "skipped": bool(row.skipped),
        "created_at": row.created_at.isoformat(),
    }


async def _load(db: AsyncSession, workspace_name: str) -> list[dict[str, Any]]:
    rows = (
        await db.scalars(
            select(models.DreamDigest)
            .where(models.DreamDigest.workspace_name == workspace_name)
            .order_by(models.DreamDigest.period.asc(), models.DreamDigest.id.asc())
        )
    ).all()
    return [_as_record(row) for row in rows]


async def _trim(db: AsyncSession, workspace_name: str, kind: str, keep: int) -> None:
    ids = list(
        await db.scalars(
            select(models.DreamDigest.id)
            .where(models.DreamDigest.workspace_name == workspace_name)
            .where(models.DreamDigest.kind == kind)
            .order_by(models.DreamDigest.period.desc(), models.DreamDigest.id.desc())
        )
    )
    extra = ids[keep:]
    if not extra:
        return
    from sqlalchemy import delete

    await db.execute(
        delete(models.DreamDigest).where(
            models.DreamDigest.workspace_name == workspace_name,
            models.DreamDigest.id.in_(extra),
        )
    )


async def _save(
    workspace_name: str,
    *,
    kind: str,
    period: str,
    content: str,
    source_ids: list[str],
    tokens: int,
    now: datetime,
    external: bool,
    skipped: bool,
) -> dict[str, Any]:
    record_id = generate_nanoid()
    async with tracked_db("dreams.save") as db:
        existing = await db.scalar(
            select(models.DreamDigest).where(
                models.DreamDigest.workspace_name == workspace_name,
                models.DreamDigest.kind == kind,
                models.DreamDigest.period == period,
            )
        )
        if existing is None:
            row = models.DreamDigest(
                id=record_id,
                workspace_name=workspace_name,
                kind=kind,
                period=period,
                content=content,
                source_ids=source_ids,
                tokens=tokens,
                external=external,
                skipped=skipped,
                created_at=now,
            )
            db.add(row)
        else:
            existing.content = content
            existing.source_ids = source_ids
            existing.tokens = tokens
            existing.external = external
            existing.skipped = skipped
            row = existing
        keep = _DAILY_HISTORY if kind == "daily" else _WEEKLY_HISTORY
        await db.flush()
        await _trim(db, workspace_name, kind, keep)
        await db.commit()
        await db.refresh(row)
        return _as_record(row)


def _source_conclusion_query(workspace_name: str) -> Any:
    """Conclusions one dream run may read. Scoped to the caller's workspace."""
    return (
        select(
            models.Document.id,
            models.Document.content,
            models.Document.internal_metadata,
        )
        .where(models.Document.workspace_name == workspace_name)
        .where(models.Document.deleted_at.is_(None))
        .where(models.Document.superseded_by.is_(None))
        .order_by(models.Document.created_at.desc())
        .limit(40)
    )


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
        if workspace is None or not inference_enabled(workspace.configuration):
            return []
        existing = await _load(db, workspace_name)
        rows = (await db.execute(_source_conclusion_query(workspace_name))).all()
    conclusions: list[tuple[str, str, bool]] = []
    for row in rows:
        raw_meta: Any = row.internal_metadata
        meta = cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
        trust = meta.get("source_trust")
        tainted = trust is None or trust == "external"
        conclusions.append((str(row.id), str(row.content), tainted is True))
    created: list[dict[str, Any]] = []
    digests = [row for row in existing if row["kind"] == "daily"]
    inputs_external = any(tainted for _doc_id, _text, tainted in conclusions)

    if conclusions and not any(row["period"] == daily_period for row in digests):
        lines = "\n".join(
            f"- {doc_id}: {text}" for doc_id, text, _tainted in conclusions
        )
        prompt = (
            "Write a daily digest for this workspace only.\n"
            + "Do not infer health, religion, politics, sex, immigration, "
            + "crime, or pay.\n"
            + lines
        )
        text, spent = await _complete(workspace_name, "dream.daily", prompt)
        record = await _accept(
            workspace_name,
            kind="daily",
            period=daily_period,
            text=text,
            spent=spent,
            source_ids=[doc_id for doc_id, _text, _tainted in conclusions],
            external=inputs_external,
            now=clock,
        )
        created.append(record)

    async with tracked_db("dreams.read.weekly", read_only=True) as db:
        stored = await _load(db, workspace_name)
    daily_ready = [
        row
        for row in stored
        if row["kind"] == "daily" and row["content"] and not row["skipped"]
    ]
    window = daily_ready[-_WEEKLY_WINDOW:]
    weekly_done = any(
        row["kind"] == "weekly" and row["period"] == weekly_period for row in stored
    )
    if window and not weekly_done:
        lines = "\n".join("- " + row["id"] + ": " + row["content"] for row in window)
        prompt = (
            "Write a weekly insight from these daily digests only.\n"
            + "Do not use raw messages.\n"
            + lines
        )
        text, spent = await _complete(workspace_name, "dream.weekly", prompt)
        record = await _accept(
            workspace_name,
            kind="weekly",
            period=weekly_period,
            text=text,
            spent=spent,
            source_ids=[row["id"] for row in window],
            external=any(bool(row["external"]) for row in window),
            now=clock,
        )
        created.append(record)
    await _rebuild_cards(workspace_name)
    return created


async def _accept(
    workspace_name: str,
    *,
    kind: str,
    period: str,
    text: str,
    spent: int,
    source_ids: list[str],
    external: bool,
    now: datetime,
) -> dict[str, Any]:
    """Store the digest, or an empty attempt when the output cannot be kept."""
    from src.sentica.safety import keep_conclusion

    category = banned_category(text)
    if category is not None:
        logger.info("Dropped a conclusion category=%s", category)
        note_drop(category)
    kept = bool(text.strip()) and category is None
    if kept and not await keep_conclusion(text, workspace_name=workspace_name):
        kept = False
    return await _save(
        workspace_name,
        kind=kind,
        period=period,
        content=text if kept else "",
        source_ids=source_ids,
        tokens=spent,
        now=now,
        external=external,
        skipped=not kept,
    )


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


async def _rebuild_cards(workspace_name: str) -> None:
    """Rebuild every card in the workspace from untainted conclusions."""
    from src.crud.peer_card import replace_peer_card_from_conclusions

    async with tracked_db("dreams.cards") as db:
        pairs = (
            await db.execute(
                select(models.Document.observer, models.Document.observed)
                .where(models.Document.workspace_name == workspace_name)
                .distinct()
                .order_by(models.Document.observer, models.Document.observed)
            )
        ).all()
        for observer, observed in pairs:
            await replace_peer_card_from_conclusions(
                db,
                workspace_name,
                observer=str(observer),
                observed=str(observed),
            )
