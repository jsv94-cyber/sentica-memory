"""Drop sensitive inferences before they are stored.

Rules run first. An optional model check runs only when
``SENTICA_SAFETY_MODEL_CHECK`` is on, and a failed check drops the text.
"""

from __future__ import annotations

import logging
import os
import re
import threading

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
_UNPARSEABLE: dict[str, int] = {}


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


def record_drop(category: str) -> None:
    """Count one drop and log its reason code. The text is not logged."""
    from src.sentica.ops_metrics import FILTER_CATEGORIES

    allowed = frozenset(FILTER_CATEGORIES) | {"unparseable"}
    code = category if category in allowed else "unspecified"
    logger.info("Dropped a conclusion category=%s", code)
    if code == "unparseable":
        note_unparseable()
        return
    note_drop(code)


def drops(tenant: str | None = None) -> int:
    """How many conclusions this process has dropped for the tenant."""
    key = tenant if tenant is not None else _tenant()
    with _LOCK:
        return _DROPS.get(key, 0)


def note_unparseable() -> None:
    """Count a drop caused by an unparseable safety reply, not by a ban."""
    tenant = _tenant()
    with _LOCK:
        _UNPARSEABLE[tenant] = _UNPARSEABLE.get(tenant, 0) + 1


def unparseable_drops(tenant: str | None = None) -> int:
    """How many conclusions were dropped because the reply was not yes or no."""
    key = tenant if tenant is not None else _tenant()
    with _LOCK:
        return _UNPARSEABLE.get(key, 0)


def reset_drops() -> None:
    """Clear counters. Tests call this between cases."""
    with _LOCK:
        _DROPS.clear()
        _UNPARSEABLE.clear()


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


def is_safety_outage(exc: BaseException) -> bool:
    """True when the queue item must be retried instead of dropping the text."""
    return _propagates(exc)


def _propagates(exc: BaseException) -> bool:
    """Outages retry the queue item. They do not drop the conclusion."""
    from src.exceptions import GatewayBudgetExceeded, GatewayUnavailable

    if isinstance(exc, (GatewayUnavailable, GatewayBudgetExceeded)):
        return True
    try:
        import redis.exceptions as redis_errors
    except ImportError:
        return False
    return isinstance(exc, redis_errors.RedisError)


def _unparseable_failure(exc: BaseException) -> bool:
    """A gateway validation error, a malformed 200, or a 4xx is not a ban."""
    from src.exceptions import ValidationException

    if isinstance(exc, ValidationException):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 400 <= status < 500:
        return True
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500


def _classify(answer: str) -> str:
    folded = answer.strip().casefold()
    if folded.startswith("yes"):
        return "yes"
    if folded.startswith("no"):
        return "no"
    return "unparseable"


async def _ask(prompt: str, *, workspace_name: str) -> str:
    from src.config import settings
    from src.llm.api import honcho_llm_call
    from src.llm.types import LLMTelemetryContext

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
    return response.content


async def model_check_bans(text: str, *, workspace_name: str | None) -> str:
    """Ask the configured model when the optional check is on.

    Returns ``yes``, ``no``, or ``unparseable``. Only ``yes`` is a ban.
    An unparseable reply, a gateway validation error, a malformed 200, or a
    4xx is asked once more and then dropped with the unparseable counter.
    Gateway, budget, and Redis errors propagate. Any other exception is
    raised and is not counted as a ban. The prompt is this line only.
    An open database session is a programming error.
    """
    if not model_check_enabled():
        return "no"
    if not workspace_name:
        logger.warning("Safety model check refused a conclusion with no workspace")
        return "yes"
    from src.dependencies import open_db_sessions

    if open_db_sessions() > 0:
        raise RuntimeError("Refusing the safety check: a database session is open")
    prompt = (
        "Reply yes if the conclusion states or implies a health condition, "
        + "sexual orientation or activity, religion, political view, "
        + "immigration status, criminal history, personal finances, or a "
        + "medical reason for leave. Reply no otherwise.\n\n"
        + text
    )
    verdict = "unparseable"
    for _attempt in range(2):
        try:
            verdict = _classify(await _ask(prompt, workspace_name=workspace_name))
        except Exception as exc:
            if _propagates(exc):
                raise
            if _unparseable_failure(exc):
                verdict = "unparseable"
            else:
                raise
        if verdict != "unparseable":
            return verdict
    return verdict


async def keep_conclusion(text: str, *, workspace_name: str | None) -> bool:
    """Rules first, then the optional model check. A drop is counted."""
    category = banned_category(text)
    if category is not None:
        record_drop(category)
        return False
    verdict = await model_check_bans(text, workspace_name=workspace_name)
    if verdict == "yes":
        record_drop("model")
        return False
    if verdict == "unparseable":
        record_drop("unparseable")
        return False
    return True
