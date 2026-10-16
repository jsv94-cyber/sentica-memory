"""Drop sensitive inferences before they are stored.

Rules run first. An optional model check runs only when
``SENTICA_SAFETY_MODEL_CHECK`` is on, and a failed check drops the text.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

PROMPT_RULES = """Do not infer or store any of the following, even when the messages only imply them: health conditions or medical reasons, sexual orientation or sexual activity, religion, political views, immigration status, criminal history, or an individual's personal finances or pay. A pattern such as low-sugar meals plus insulin pumps is a medical inference and must not be stored. Leave or absence is stored as dates only, with no reason."""

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "health",
        re.compile(
            r"\b(diabet\w*|insulin|cancer|hiv|aids|pregnan\w*|diagnos\w*|medication|prescription|illness|disease|symptom|chemo|tumor|disability|depressed|depression|bipolar|adhd|autis\w*)\b"
        ),
    ),
    (
        "sexual",
        re.compile(
            r"\b(sexual orientation|sexual activity|gay|lesbian|bisexual|heterosexual|homosexual|transgender)\b"
        ),
    ),
    (
        "religion",
        re.compile(
            r"\b(christian|muslim|jewish|hindu|buddhist|atheist|catholic|protestant|mosque|synagogue|religious belief)\b"
        ),
    ),
    (
        "politics",
        re.compile(
            r"\b(democrat|republican|libertarian|socialist|voted for|political party|political view)\b"
        ),
    ),
    (
        "immigration",
        re.compile(
            r"\b(undocumented|visa status|immigration status|asylum|green card|citizenship status|deported)\b"
        ),
    ),
    (
        "criminal",
        re.compile(
            r"\b(felony|misdemeanor|arrested|convicted|criminal record|parole|prison)\b"
        ),
    ),
    (
        "finances",
        re.compile(
            r"\b(salary|paycheck|paycheque|wage|net worth|bank account|annual income|hourly pay)\b"
        ),
    ),
    (
        "leave",
        re.compile(
            r"\b(out sick|sick leave|medical leave|called in sick|absent because)\b|(\b(sick|ill|flu|covid|hospital)\b.{0,40}\b(leave|absent|absence)\b)|(\b(leave|absent|absence)\b.{0,40}\b(sick|ill|flu|covid|hospital)\b)"
        ),
    ),
)

_LOCK = threading.Lock()
_DROPS: dict[str, int] = {}


def banned_category(text: str) -> str | None:
    """Return the banned category, or None when the text may be stored."""
    folded = text.casefold()
    if ("low-sugar" in folded or "low sugar" in folded) and "insulin" in folded:
        return "health"
    for name, pattern in _RULES:
        if pattern.search(folded):
            return name
    return None


def _tenant() -> str:
    raw = os.environ.get("SENTICA_TENANT_ID", "").strip()
    return raw or "unset"


def note_drop(category: str = "unspecified") -> None:
    """Count one dropped conclusion for the current tenant."""
    from src.sentica.ops_metrics import note_filtered

    tenant = _tenant()
    with _LOCK:
        _DROPS[tenant] = _DROPS.get(tenant, 0) + 1
    note_filtered(category)


def drops(tenant: str | None = None) -> int:
    """How many conclusions this process has dropped for the tenant."""
    key = tenant if tenant is not None else _tenant()
    with _LOCK:
        return _DROPS.get(key, 0)


def reset_drops() -> None:
    """Clear counters. Tests call this between cases."""
    with _LOCK:
        _DROPS.clear()


def model_check_enabled() -> bool:
    """On in production unless explicitly turned off. Malformed values refuse."""
    from src.exceptions import ValidationException
    from src.sentica.runtime import production_enabled

    raw = os.environ.get("SENTICA_SAFETY_MODEL_CHECK")
    if raw is None or raw.strip() == "":
        return production_enabled()
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValidationException(
        "Refusing to start: SENTICA_SAFETY_MODEL_CHECK is not a boolean"
    )


def _conclusion_text_query(workspace_name: str) -> Any:
    """Recent conclusion text for one workspace. The model check uses this."""
    from sqlalchemy import select

    from src import models

    return (
        select(models.Document.content)
        .where(models.Document.workspace_name == workspace_name)
        .where(models.Document.deleted_at.is_(None))
        .where(models.Document.superseded_by.is_(None))
        .order_by(models.Document.created_at.desc())
        .limit(40)
    )


async def _recent_conclusion_texts(workspace_name: str) -> list[str]:
    """Load conclusion text, then return. The caller must not hold this session."""
    from src.dependencies import tracked_db

    async with tracked_db("safety.conclusions", read_only=True) as db:
        rows = (await db.scalars(_conclusion_text_query(workspace_name))).all()
    return [str(row) for row in rows]


async def model_check_bans(text: str, *, workspace_name: str | None) -> bool:
    """Ask the configured model when the optional check is on.

    A missing workspace, a transport failure, or a yes answer drops the text.
    The text itself is not logged. Recent conclusions from the same workspace
    are part of the prompt, loaded before the model call.
    """
    if not model_check_enabled():
        return False
    if not workspace_name:
        logger.warning("Safety model check refused a conclusion with no workspace")
        return True
    from src.config import settings
    from src.llm.api import honcho_llm_call
    from src.llm.types import LLMTelemetryContext

    try:
        context = await _recent_conclusion_texts(workspace_name)
    except Exception:
        logger.warning("Safety model check failed closed")
        return True
    prompt = (
        "Reply yes if the conclusion states or implies a health condition, "
        + "sexual orientation or activity, religion, political view, "
        + "immigration status, criminal history, personal finances, or a "
        + "medical reason for leave. Reply no otherwise.\n\n"
        + text
    )
    if context:
        prompt = prompt + "\n" + "\n".join(context)
    from src.dependencies import open_db_sessions

    if open_db_sessions() > 0:
        logger.warning("Safety model check refused while a database session is open")
        return True
    try:
        response = await honcho_llm_call(
            model_config=settings.SUMMARY.MODEL_CONFIG,
            prompt=prompt,
            max_tokens=8,
            telemetry=LLMTelemetryContext(
                workspace_name=workspace_name,
                call_purpose="summary.safety",
                parent_category="summary",
            ),
        )
    except Exception:
        logger.warning("Safety model check failed closed")
        return True
    return response.content.strip().casefold().startswith("yes")


async def keep_conclusion(text: str, *, workspace_name: str | None) -> bool:
    """Rules first, then the optional model check. A drop is counted."""
    category = banned_category(text)
    if category is not None:
        logger.info("Dropped a conclusion category=%s", category)
        note_drop(category)
        return False
    if await model_check_bans(text, workspace_name=workspace_name):
        logger.info("Dropped a conclusion category=%s", "model")
        note_drop("model")
        return False
    return True
