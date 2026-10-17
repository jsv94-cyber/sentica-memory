"""One OpenAI-compatible gateway for every model call.

Gateway mode is off unless ``SENTICA_GATEWAY_MODE`` is true. Each function
has its own base URL and model. A call never switches to another model.

The daily token counter is a backstop in the instance Redis. The authoritative
budget is enforced at the Sentica gateway. A response that omits usage is
charged an estimate, never zero.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from datetime import UTC, datetime
from typing import Any, NoReturn, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from nanoid import generate as generate_nanoid
from pydantic import BaseModel, ValidationError

from src.exceptions import (
    GatewayBudgetExceeded,
    GatewayStructuredOutputError,
    GatewayUnavailable,
    ValidationException,
)
from src.llm.types import (
    HonchoLLMCallResponse,
    HonchoLLMCallStreamChunk,
    LLMTelemetryContext,
    StreamingResponseWithMetadata,
)

logger = logging.getLogger(__name__)

_PURPOSES = ("deriver", "summary", "dialectic", "dream", "embed")
_FILTERED_FINISH = frozenset(
    {
        "content_filter",
        "refusal",
        "safety",
        "recitation",
        "prohibited_content",
        "blocklist",
        "spii",
        "image_safety",
    }
)
_CAP_FINISH = frozenset({"length", "max_tokens", "model_context_window_exceeded"})
_SYNTHESIS = (
    "You have reached the maximum number of tool calls. "
    "Based on all the information you have gathered, provide your final response now. "
    "Do not attempt to call any more tools."
)
_BUDGET_TTL_SECONDS = 172800
_async_pool: tuple[str, Any] | None = None
# One script holds the estimate on both counters. -1 means that limit is unset.
# A hold that would pass the limit is refused, so in-flight calls cannot
# each observe the same spent total.
_RESERVE_SCRIPT = """
-- sentica-budget-reserve
local total = tonumber(redis.call('GET', KEYS[1]) or '0')
local spent = tonumber(redis.call('GET', KEYS[2]) or '0')
local total_limit = tonumber(ARGV[1])
local purpose_limit = tonumber(ARGV[2])
local hold = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
if total_limit >= 0 and total + hold > total_limit then
  return 0
end
if purpose_limit >= 0 and spent + hold > purpose_limit then
  return 0
end
if hold > 0 then
  redis.call('INCRBY', KEYS[1], hold)
  redis.call('EXPIRE', KEYS[1], ttl)
  redis.call('INCRBY', KEYS[2], hold)
  redis.call('EXPIRE', KEYS[2], ttl)
end
return 1
"""
# One script replaces a hold with the actual token count. A negative delta
# releases the unused part of the hold. Counters are clamped at zero so a
# release cannot make the next check fail open.
_SETTLE_SCRIPT = """
-- sentica-budget-settle
local actual = tonumber(ARGV[1])
local hold = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local delta = actual - hold
if delta ~= 0 then
  redis.call('INCRBY', KEYS[1], delta)
  redis.call('INCRBY', KEYS[2], delta)
end
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
local total = tonumber(redis.call('GET', KEYS[1]) or '0')
local spent = tonumber(redis.call('GET', KEYS[2]) or '0')
if total < 0 then
  redis.call('SET', KEYS[1], '0')
  total = 0
end
if spent < 0 then
  redis.call('SET', KEYS[2], '0')
end
return total
"""
_ON_VALUES = frozenset({"1", "true", "yes", "on"})
_OFF_VALUES = frozenset({"0", "false", "no", "off"})
_GATEWAY_ENV = (
    "SENTICA_GATEWAY_MODE",
    "SENTICA_GATEWAY_BASE_URL",
    "SENTICA_GATEWAY_DERIVER_BASE_URL",
    "SENTICA_GATEWAY_SUMMARY_BASE_URL",
    "SENTICA_GATEWAY_DIALECTIC_BASE_URL",
    "SENTICA_GATEWAY_DREAM_BASE_URL",
    "SENTICA_GATEWAY_EMBED_BASE_URL",
    "SENTICA_GATEWAY_DERIVER_MODEL",
    "SENTICA_GATEWAY_SUMMARY_MODEL",
    "SENTICA_GATEWAY_DIALECTIC_MODEL",
    "SENTICA_GATEWAY_DREAM_MODEL",
    "SENTICA_GATEWAY_EMBED_MODEL",
    "SENTICA_GATEWAY_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_DERIVER_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_SUMMARY_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_DIALECTIC_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_DREAM_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_EMBED_DAILY_TOKEN_BUDGET",
    "SENTICA_GATEWAY_REDIS_URL",
    "SENTICA_GATEWAY_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_DERIVER_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_SUMMARY_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_DIALECTIC_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_DREAM_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_EMBED_TIMEOUT_SECONDS",
    "SENTICA_GATEWAY_RETRY_ATTEMPTS",
    "SENTICA_GATEWAY_RETRY_WAIT_SECONDS",
    "SENTICA_GATEWAY_TOKENIZER",
    "SENTICA_GATEWAY_TOKENIZER_ALIASES",
)


def gateway_env_names() -> tuple[str, ...]:
    """Every gateway variable checked at startup."""
    return _GATEWAY_ENV


def gateway_enabled() -> bool:
    """Return whether model calls must go through the gateway."""
    raw = os.environ.get("SENTICA_GATEWAY_MODE", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _day() -> str:
    return datetime.now(UTC).date().isoformat()


def _total_key(tenant: str) -> str:
    return "sentica:budget:" + tenant + ":" + _day()


def _purpose_key(tenant: str, purpose: str) -> str:
    return "sentica:budget:" + tenant + ":" + purpose + ":" + _day()


def _redis_url() -> str | None:
    """Instance Redis for the budget backstop.

    ``SENTICA_GATEWAY_REDIS_URL`` wins. Otherwise a ``redis://`` or
    ``rediss://`` ``CACHE_URL`` is used. ``mem://`` and other schemes are not.
    The ``suppress`` query used by the cache client is removed.
    """
    raw = os.environ.get("SENTICA_GATEWAY_REDIS_URL", "").strip()
    if not raw:
        raw = os.environ.get("CACHE_URL", "").strip()
    if not raw:
        from src.config import settings

        raw = settings.CACHE.URL
    if not raw.startswith(("redis://", "rediss://")):
        return None
    parts = urlsplit(raw)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "suppress"
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _sync_client() -> Any | None:
    url = _redis_url()
    if url is None:
        return None
    import redis

    connect = cast(Callable[..., Any], redis.Redis.from_url)
    return connect(url, decode_responses=True, protocol=2, socket_timeout=2.0)


def _async_client() -> Any | None:
    """Return the pooled async client for the current Redis URL.

    A new URL replaces the pool. The client is not closed here: closing it
    closes the pool.
    """
    global _async_pool
    url = _redis_url()
    if url is None:
        return None
    if _async_pool is not None and _async_pool[0] == url:
        return _async_pool[1]
    import redis.asyncio as aioredis

    connect = cast(Callable[..., Any], aioredis.Redis.from_url)
    client = connect(url, decode_responses=True, protocol=2, socket_timeout=2.0)
    _async_pool = (url, client)
    return client


def _drop_async_pool() -> None:
    global _async_pool
    _async_pool = None


def reset_token_counters() -> None:
    """Delete this process's budget keys. Tests use this between cases."""
    _drop_async_pool()
    client = _sync_client()
    if client is None:
        return
    try:
        keys = list(client.scan_iter(match="sentica:budget:*", count=200))
        if keys:
            client.delete(*keys)
    finally:
        client.close()


def tokens_spent(tenant: str, purpose: str | None = None) -> int:
    """Tokens recorded today for one tenant, or for one purpose."""
    client = _sync_client()
    if client is None:
        return 0
    key = _total_key(tenant) if purpose is None else _purpose_key(tenant, purpose)
    try:
        raw = client.get(key)
    finally:
        client.close()
    if raw is None:
        return 0
    return int(raw)


def _optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationException(
            "Refusing to call: " + name + " is not an integer"
        ) from exc
    if value < 0:
        raise ValidationException("Refusing to call: " + name + " is negative")
    return value


def _budgets(purpose: str) -> tuple[int | None, int | None]:
    return (
        _optional_int("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET"),
        _optional_int("SENTICA_GATEWAY_" + purpose.upper() + "_DAILY_TOKEN_BUDGET"),
    )


async def charge_tokens(tenant: str, purpose: str, tokens: int) -> int:
    """Add tokens to the shared budget and return the tenant total.

    API and deriver processes call the same Redis key. A process-local
    counter would hide the other process's write.
    """
    await _charge(tenant, purpose, max(tokens, 1))
    return tokens_spent(tenant)


def _limit_arg(limit: int | None) -> str:
    if limit is None:
        return "-1"
    return str(limit)


async def _reserve(tenant: str, purpose: str, hold: int) -> int:
    """Hold ``hold`` tokens. Returns the hold applied, or 0 when no budget is set."""
    total_limit, purpose_limit = _budgets(purpose)
    if total_limit is None and purpose_limit is None:
        return 0
    if hold < 1:
        hold = 1
    client = _async_client()
    if client is None:
        raise GatewayUnavailable(
            "Refusing to call: a gateway budget is set and Redis is not configured"
        )
    try:
        allowed = await client.eval(
            _RESERVE_SCRIPT,
            2,
            _total_key(tenant),
            _purpose_key(tenant, purpose),
            _limit_arg(total_limit),
            _limit_arg(purpose_limit),
            str(hold),
            str(_BUDGET_TTL_SECONDS),
        )
    except GatewayBudgetExceeded:
        raise
    except Exception as exc:
        raise GatewayUnavailable(
            "Refusing to call: the gateway budget store is unavailable"
        ) from exc
    if int(allowed) == 0:
        raise GatewayBudgetExceeded("token budget exhausted")
    return hold


async def _settle(tenant: str, purpose: str, actual: int, hold: int) -> None:
    """Replace ``hold`` with ``actual`` on both counters."""
    if actual < 0:
        actual = 0
    total_limit, purpose_limit = _budgets(purpose)
    client = _async_client()
    if client is None:
        if total_limit is not None or purpose_limit is not None:
            raise GatewayUnavailable(
                "Refusing to call: a gateway budget is set and Redis is not configured"
            )
        return
    try:
        await client.eval(
            _SETTLE_SCRIPT,
            2,
            _total_key(tenant),
            _purpose_key(tenant, purpose),
            str(actual),
            str(hold),
            str(_BUDGET_TTL_SECONDS),
        )
    except Exception as exc:
        if total_limit is not None or purpose_limit is not None:
            raise GatewayUnavailable(
                "Refusing to call: the gateway budget store is unavailable"
            ) from exc
        logger.warning("gateway budget store did not record tokens")


async def _charge(tenant: str, purpose: str, tokens: int) -> None:
    if tokens < 1:
        tokens = 1
    await _settle(tenant, purpose, tokens, 0)


async def _release_hold(tenant: str, purpose: str, hold: int) -> None:
    """Give back a hold. A store error leaves the hold in place."""
    if hold < 1:
        return
    try:
        await _settle(tenant, purpose, 0, hold)
    except GatewayUnavailable:
        logger.warning("gateway hold was not released")


def _enabled_flag(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValidationException("Refusing to call: " + name + " is not set")
    return value


def _route(purpose: str) -> tuple[str, str]:
    if purpose not in _PURPOSES:
        raise ValidationException("Refusing to call: the gateway purpose is missing")
    specific = "SENTICA_GATEWAY_" + purpose.upper() + "_BASE_URL"
    base = os.environ.get(specific, "").strip()
    if not base:
        base = os.environ.get("SENTICA_GATEWAY_BASE_URL", "").strip()
    if not base:
        raise ValidationException("Refusing to call: " + specific + " is not set")
    model = _enabled_flag("SENTICA_GATEWAY_" + purpose.upper() + "_MODEL")
    return base.rstrip("/"), model


def _tenant() -> str:
    return _enabled_flag("SENTICA_TENANT_ID")


def _workspace(telemetry: LLMTelemetryContext | None) -> str:
    from src.sentica.workspace_scope import current_workspace

    name = telemetry.workspace_name if telemetry is not None else None
    if not name:
        name = current_workspace()
    if not name:
        raise ValidationException("Refusing to call: workspace is missing")
    return name


def _purpose(telemetry: LLMTelemetryContext | None) -> str:
    call = telemetry.call_purpose if telemetry is not None else None
    parent = telemetry.parent_category if telemetry is not None else None
    raw = (call or "").lower()
    parent_name = (parent or "").lower()
    if raw.startswith("deriver") or parent_name == "representation":
        return "deriver"
    if raw.startswith("summary") or parent_name == "summary":
        return "summary"
    if raw.startswith("dialectic") or parent_name == "dialectic":
        return "dialectic"
    if raw.startswith("dream") or parent_name == "dream":
        return "dream"
    raise ValidationException("Refusing to call: the gateway purpose is missing")


_PURPOSE_TIMEOUTS = {
    "deriver": 120.0,
    "dream": 180.0,
    "dialectic": 60.0,
    "summary": 60.0,
    "embed": 30.0,
}
_UNAVAILABLE_STATUS = {401, 403, 404, 408, 429}


def _positive_number(raw: str, message: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationException(message) from exc
    if value <= 0:
        raise ValidationException(message)
    return value


def _timeout_seconds(purpose: str) -> float:
    specific = "SENTICA_GATEWAY_" + purpose.upper() + "_TIMEOUT_SECONDS"
    raw = os.environ.get(specific)
    if raw is not None and raw.strip():
        return _positive_number(
            raw.strip(), "Refusing to call: gateway timeout is not a number"
        )
    fallback = os.environ.get("SENTICA_GATEWAY_TIMEOUT_SECONDS")
    if fallback is not None and fallback.strip():
        return _positive_number(
            fallback.strip(), "Refusing to call: gateway timeout is not a number"
        )
    return _PURPOSE_TIMEOUTS.get(purpose, 60.0)


def _attempts() -> int:
    raw = os.environ.get("SENTICA_GATEWAY_RETRY_ATTEMPTS")
    if raw is None or raw.strip() == "":
        return 3
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationException(
            "Refusing to call: SENTICA_GATEWAY_RETRY_ATTEMPTS is not an integer"
        ) from exc
    if value < 1:
        raise ValidationException(
            "Refusing to call: SENTICA_GATEWAY_RETRY_ATTEMPTS must be positive"
        )
    return value


def _wait() -> float:
    raw = os.environ.get("SENTICA_GATEWAY_RETRY_WAIT_SECONDS")
    if raw is None or raw.strip() == "":
        return 0.05
    return _positive_number(
        raw.strip(),
        "Refusing to call: SENTICA_GATEWAY_RETRY_WAIT_SECONDS is not a number",
    )


def _refuse(message: str) -> NoReturn:
    from src.sentica.runtime import SenticaStartupError

    raise SenticaStartupError("Refusing to start: " + message)


def _optional_positive(name: str) -> None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return
    try:
        number = float(raw.strip())
    except ValueError:
        _refuse(name + " is not a number")
    if not math.isfinite(number) or number <= 0:
        _refuse(name + " must be a positive number")


def _optional_budget(name: str) -> None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return
    try:
        value = int(raw.strip())
    except ValueError:
        _refuse(name + " is not an integer")
    if value < 0:
        _refuse(name + " is negative")


def refuse_invalid_gateway_env() -> None:
    """Refuse startup when any gateway environment value is invalid.

    Blank optional values stay at their defaults. This runs in test mode and
    in production. Call-time checks still refuse a bad value on a later call.
    """
    raw_mode = os.environ.get("SENTICA_GATEWAY_MODE")
    if (
        raw_mode is not None
        and raw_mode.strip()
        and raw_mode.strip().lower() not in _ON_VALUES | _OFF_VALUES
    ):
        _refuse("SENTICA_GATEWAY_MODE is invalid")

    attempts = os.environ.get("SENTICA_GATEWAY_RETRY_ATTEMPTS")
    if attempts is not None and attempts.strip():
        try:
            parsed_attempts = int(attempts.strip())
        except ValueError:
            _refuse("SENTICA_GATEWAY_RETRY_ATTEMPTS is not an integer")
        if parsed_attempts < 1:
            _refuse("SENTICA_GATEWAY_RETRY_ATTEMPTS must be positive")

    _optional_positive("SENTICA_GATEWAY_RETRY_WAIT_SECONDS")
    _optional_positive("SENTICA_GATEWAY_TIMEOUT_SECONDS")
    for purpose in _PURPOSES:
        _optional_positive("SENTICA_GATEWAY_" + purpose.upper() + "_TIMEOUT_SECONDS")
        _optional_budget("SENTICA_GATEWAY_" + purpose.upper() + "_DAILY_TOKEN_BUDGET")
    _optional_budget("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET")

    for name in (
        "SENTICA_GATEWAY_BASE_URL",
        *("SENTICA_GATEWAY_" + purpose.upper() + "_BASE_URL" for purpose in _PURPOSES),
    ):
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            continue
        if not raw.strip().startswith(("http://", "https://")):
            _refuse(name + " must be an http or https URL")

    redis_url = os.environ.get("SENTICA_GATEWAY_REDIS_URL")
    if (
        redis_url is not None
        and redis_url.strip()
        and not redis_url.strip().startswith(("redis://", "rediss://"))
    ):
        _refuse("SENTICA_GATEWAY_REDIS_URL must be a redis or rediss URL")

    for purpose in _PURPOSES:
        name = "SENTICA_GATEWAY_" + purpose.upper() + "_MODEL"
        raw = os.environ.get(name)
        if raw is None:
            continue
        if not raw.strip():
            _refuse(name + " is empty")

    from src.sentica.tokenizer import validate_tokenizer_env

    try:
        validate_tokenizer_env()
    except ValueError as exc:
        _refuse(str(exc))


def _headers(
    *,
    tenant: str,
    workspace: str,
    purpose: str,
    request_id: str,
    stream: bool = False,
    embed_operation: str | None = None,
) -> dict[str, str]:
    headers = {
        "X-Sentica-Tenant": tenant,
        "X-Sentica-Workspace": workspace,
        "X-Sentica-Purpose": purpose,
        "X-Sentica-Request-Id": request_id,
        "X-Sentica-Stream": "true" if stream else "false",
    }
    if embed_operation is not None:
        headers["X-Sentica-Embed-Op"] = embed_operation
    return headers


def _mapping(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, Any], value)


def _request_id(telemetry: LLMTelemetryContext | None) -> str:
    if telemetry is not None and telemetry.span_id:
        return telemetry.span_id
    return generate_nanoid()


def _emit_call(
    *,
    telemetry: LLMTelemetryContext | None,
    model: str,
    max_tokens: int,
    prompt_tokens: int,
    completion_tokens: int,
    finish_reason: str | None,
    outcome: str,
    error: BaseException | None,
    attempt: int,
    has_tools: bool,
    tool_call_count: int,
    was_stream: bool,
    duration_ms: float,
) -> None:
    """Fire one LLMCallCompletedEvent. A telemetry failure is ignored."""
    try:
        from src.telemetry.events import CallPurpose, LLMCallCompletedEvent, emit

        call_purpose = None
        if telemetry is not None and telemetry.call_purpose:
            try:
                call_purpose = CallPurpose(telemetry.call_purpose)
            except ValueError:
                call_purpose = None
        retry_attempts = _attempts()
        emit(
            LLMCallCompletedEvent(
                workspace_name=(
                    telemetry.workspace_name if telemetry is not None else None
                ),
                call_purpose=call_purpose,
                parent_category=(
                    telemetry.parent_category if telemetry is not None else None
                ),
                transport="openai",
                provider_label=None,
                model=model,
                effective_max_output_tokens=max_tokens,
                provider_input_tokens=prompt_tokens,
                provider_output_tokens=completion_tokens,
                finish_reason=finish_reason,
                outcome=cast(Any, outcome),
                is_final_attempt=attempt >= retry_attempts,
                error_class=type(error).__name__ if error is not None else None,
                attempt=attempt,
                retry_attempts=retry_attempts,
                was_fallback=False,
                duration_ms=duration_ms,
                has_tools=has_tools,
                tool_call_count=tool_call_count,
                was_stream=was_stream,
                run_id=telemetry.run_id if telemetry is not None else None,
                iteration=telemetry.iteration if telemetry is not None else None,
            )
        )
    except Exception:
        logger.debug("Failed to emit LLMCallCompletedEvent", exc_info=True)


def _usage_pair(
    payload: dict[str, Any],
    messages: list[dict[str, Any]],
    content: str,
    model: str,
) -> tuple[int, int, bool]:
    """Return prompt tokens, completion tokens, and whether usage was present."""
    usage = _mapping(payload.get("usage"))
    if usage is not None:
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if (
            isinstance(prompt, int)
            and isinstance(completion, int)
            and prompt >= 0
            and completion >= 0
        ):
            return prompt, completion, True
    from src.sentica.tokenizer import count_gateway_tokens

    texts: list[str] = []
    for message in messages:
        value = message.get("content")
        if isinstance(value, str) and value:
            texts.append(value)
    if content:
        texts.append(content)
    estimate = count_gateway_tokens(texts, model)
    if estimate < 1:
        estimate = 1
    return estimate, 0, False


def _chat_hold(messages: list[dict[str, Any]], max_tokens: int, model: str) -> int:
    """Upper bound for one chat call: prompt tokens plus the completion cap."""
    from src.sentica.tokenizer import count_gateway_tokens

    texts: list[str] = []
    for message in messages:
        value = message.get("content")
        if isinstance(value, str) and value:
            texts.append(value)
    prompt = count_gateway_tokens(texts, model)
    hold = prompt + max(max_tokens, 0)
    if hold < 1:
        return 1
    return hold


def _request_body(
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    response_model: type[BaseModel] | None,
    json_mode: bool,
) -> dict[str, Any]:
    """Build an OpenAI chat body with the existing adapter. No SDK client."""
    from src.llm.backends.openai import OpenAIBackend

    backend = OpenAIBackend(None)
    params = backend._build_params(  # pyright: ignore[reportPrivateUsage]
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=None,
        stop=None,
        tools=tools,
        tool_choice=tool_choice,
        thinking_effort=None,
        thinking_budget_tokens=None,
        extra_params=None,
    )
    if response_model is not None:
        params["response_format"] = OpenAIBackend._json_schema_response_format(  # pyright: ignore[reportPrivateUsage]
            response_model
        )
    elif json_mode:
        params["response_format"] = {"type": "json_object"}
    params.pop("timeout", None)
    params.pop("extra_body", None)
    return params


async def _post(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: float,
    telemetry: LLMTelemetryContext | None,
    model: str,
    max_tokens: int,
    has_tools: bool,
    round_index: int = 1,
) -> tuple[dict[str, Any], int]:
    attempts = _attempts()
    wait = _wait()
    last = GatewayUnavailable("model gateway unavailable")
    stem = headers["X-Sentica-Request-Id"]
    for attempt in range(1, attempts + 1):
        stamped = dict(headers)
        stamped["X-Sentica-Request-Id"] = (
            stem + ":r" + str(round_index) + ":a" + str(attempt)
        )
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=body, headers=stamped)
        except httpx.HTTPError as exc:
            logger.warning(
                "Gateway transport failure on attempt %s for %s",
                attempt,
                headers["X-Sentica-Purpose"],
            )
            last = GatewayUnavailable("model gateway unavailable")
            last.__cause__ = exc
            _emit_call(
                telemetry=telemetry,
                model=model,
                max_tokens=max_tokens,
                prompt_tokens=0,
                completion_tokens=0,
                finish_reason=None,
                outcome="error",
                error=last,
                attempt=attempt,
                has_tools=has_tools,
                tool_call_count=0,
                was_stream=False,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        else:
            status = response.status_code
            unavailable = status in _UNAVAILABLE_STATUS or status >= 500
            if not unavailable:
                try:
                    response.raise_for_status()
                    payload = _mapping(cast(object, response.json()))
                except (httpx.HTTPError, ValueError) as exc:
                    raise ValidationException(
                        "Refusing to call: the gateway returned an unreadable body"
                    ) from exc
                if payload is None:
                    raise ValidationException(
                        "Refusing to call: the gateway returned an unreadable body"
                    )
                return payload, attempt
            logger.warning(
                "Gateway status %s on attempt %s for %s",
                response.status_code,
                attempt,
                headers["X-Sentica-Purpose"],
            )
            last = GatewayUnavailable("model gateway unavailable")
            _emit_call(
                telemetry=telemetry,
                model=model,
                max_tokens=max_tokens,
                prompt_tokens=0,
                completion_tokens=0,
                finish_reason=None,
                outcome="error",
                error=last,
                attempt=attempt,
                has_tools=has_tools,
                tool_call_count=0,
                was_stream=False,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        if attempt < attempts:
            await asyncio.sleep(wait)
    raise last


def _choice(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValidationException(
            "Refusing to call: the gateway returned no completion"
        )
    first = _mapping(cast(object, choices[0]))
    message = _mapping(None if first is None else first.get("message"))
    if first is None or message is None:
        raise ValidationException(
            "Refusing to call: the gateway returned no completion"
        )
    finish = first.get("finish_reason")
    return first, message, finish if isinstance(finish, str) else "stop"


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls_value = message.get("tool_calls")
    if not isinstance(calls_value, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in cast(list[object], calls_value):
        call = _mapping(item)
        if call is None:
            raise ValidationException("Refusing to call: a tool call was malformed")
        function = _mapping(call.get("function"))
        if function is None:
            raise ValidationException("Refusing to call: a tool call was malformed")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationException("Refusing to call: a tool call was malformed")
        arguments = function.get("arguments", "{}")
        if arguments == "":
            arguments = "{}"
        try:
            loaded: object = (
                json.loads(arguments) if isinstance(arguments, str) else arguments
            )
        except json.JSONDecodeError as exc:
            raise ValidationException(
                "Refusing to call: a tool argument was not an object"
            ) from exc
        parsed = _mapping(loaded)
        if parsed is None:
            raise ValidationException(
                "Refusing to call: a tool argument was not an object"
            )
        call_id = call.get("id")
        calls.append(
            {
                "id": call_id if isinstance(call_id, str) else "",
                "name": name,
                "input": parsed,
            }
        )
    return calls


def _text(message: dict[str, Any]) -> str:
    text = message.get("content")
    return text if isinstance(text, str) else ""


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _structured_failure(kind: str, raw: str) -> GatewayStructuredOutputError:
    encoded = raw.encode("utf-8", errors="replace")
    digest = hashlib.sha256(encoded).hexdigest()
    return GatewayStructuredOutputError(
        kind + " sha256=" + digest + " length=" + str(len(encoded))
    )


def _interpret(
    *,
    message: dict[str, Any],
    content: str,
    finish: str,
    response_model: type[BaseModel] | None,
    json_mode: bool,
    purpose: str,
) -> Any:
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        raise _structured_failure("refusal", refusal) from None
    if finish in _FILTERED_FINISH:
        raise _structured_failure("filtered", finish) from None
    if purpose == "summary" and not content.strip() and finish in _CAP_FINISH:
        raise _structured_failure("empty_summary", content) from None
    if response_model is None:
        if json_mode:
            try:
                json.loads(_strip_fence(content))
            except json.JSONDecodeError:
                raise _structured_failure("malformed", content) from None
            return _strip_fence(content)
        return content
    try:
        parsed = response_model.model_validate_json(_strip_fence(content))
    except ValidationError:
        raise _structured_failure("malformed", content) from None
    if response_model.__name__ == "PromptRepresentation" and finish in _CAP_FINISH:
        explicit = getattr(parsed, "explicit", None)
        if explicit is None or explicit == []:
            raise _structured_failure("empty_representation", content) from None
    return parsed


async def _settle_paid(tenant: str, purpose: str, actual: int, hold: int) -> None:
    """Record a completed call. A store failure does not discard the completion."""
    try:
        await _settle(tenant, purpose, actual, hold)
    except GatewayUnavailable:
        logger.warning("gateway charge failed after a paid call")


async def _chat(
    *,
    purpose: str,
    workspace: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    response_model: type[BaseModel] | None,
    json_mode: bool,
    telemetry: LLMTelemetryContext | None,
    mark_stream: bool,
    round_index: int = 1,
) -> tuple[
    dict[str, Any],
    str,
    list[dict[str, Any]],
    str,
    int,
    int,
    str,
    int,
    float,
]:
    tenant = _tenant()
    base, model = _route(purpose)
    applied = await _reserve(tenant, purpose, _chat_hold(messages, max_tokens, model))
    request_id = _request_id(telemetry)
    body = _request_body(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        tools=tools,
        tool_choice=tool_choice,
        response_model=response_model,
        json_mode=json_mode,
    )
    started = time.perf_counter()
    try:
        payload, attempt = await _post(
            base + "/chat/completions",
            body,
            _headers(
                tenant=tenant,
                workspace=workspace,
                purpose=purpose,
                request_id=request_id,
                stream=mark_stream,
            ),
            timeout=_timeout_seconds(purpose),
            telemetry=telemetry,
            model=model,
            max_tokens=max_tokens,
            has_tools=bool(tools),
            round_index=round_index,
        )
        _choice_row, message, finish = _choice(payload)
        del _choice_row
        content = _text(message)
        prompt_tokens, completion_tokens, present = _usage_pair(
            payload, messages, content, model
        )
        billed = (
            prompt_tokens + completion_tokens
            if present
            else max(prompt_tokens + completion_tokens, 1)
        )
        await _settle_paid(tenant, purpose, billed, applied)
        applied = 0
        calls = _tool_calls(message)
        duration_ms = (time.perf_counter() - started) * 1000
        return (
            message,
            finish,
            calls,
            content,
            prompt_tokens,
            completion_tokens,
            model,
            attempt,
            duration_ms,
        )
    finally:
        if applied:
            await _release_hold(tenant, purpose, applied)


def _append_tools(
    history: list[dict[str, Any]],
    message_calls: list[dict[str, Any]],
    results: list[tuple[str, str]],
) -> None:
    from src.llm.backend import CompletionResult, ToolCallResult
    from src.llm.history_adapters import OpenAIHistoryAdapter

    adapter = OpenAIHistoryAdapter()
    history.append(
        adapter.format_assistant_tool_message(
            CompletionResult(
                content=None,
                tool_calls=[
                    ToolCallResult(
                        id=str(call["id"]),
                        name=str(call["name"]),
                        input=cast(dict[str, Any], call["input"]),
                    )
                    for call in message_calls
                ],
            )
        )
    )
    history.extend(
        adapter.format_tool_results(
            [{"tool_id": tool_id, "result": result} for tool_id, result in results]
        )
    )


def _offered_names(tools: list[dict[str, Any]] | None) -> set[str]:
    """Tool names from either a flat schema or an OpenAI function schema."""
    names: set[str] = set()
    for tool in tools or []:
        direct = tool.get("name")
        if isinstance(direct, str):
            names.add(direct)
        function = tool.get("function")
        if isinstance(function, dict):
            nested = cast(dict[str, Any], function).get("name")
            if isinstance(nested, str):
                names.add(nested)
    return names


async def _execute_tools(
    calls: list[dict[str, Any]],
    tool_executor: Callable[[str, dict[str, Any]], Any],
    offered: set[str],
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], set[str]]:
    from src.utils.agent_tools import unoffered_tool_refusal

    made: list[dict[str, Any]] = []
    results: list[tuple[str, str]] = []
    succeeded: set[str] = set()
    for call in calls:
        name = str(call["name"])
        parsed = cast(dict[str, Any], call["input"])
        if name not in offered:
            result: Any = unoffered_tool_refusal(name)
        else:
            result = tool_executor(name, parsed)
            if isinstance(result, Awaitable):
                result = await result
            if not str(result).startswith("ERROR:"):
                succeeded.add(name)
        made.append({"tool_name": name, "tool_input": parsed, "name": name})
        results.append((str(call["id"]), str(result)))
    return made, results, succeeded


async def complete_llm(
    *,
    prompt: str,
    max_tokens: int,
    response_model: type[BaseModel] | None,
    stream: bool,
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    tool_executor: Callable[[str, dict[str, Any]], Any] | None,
    max_tool_iterations: int,
    messages: list[dict[str, Any]] | None,
    telemetry: LLMTelemetryContext | None,
    json_mode: bool = False,
    max_input_tokens: int | None = None,
    force_tools_until: Collection[str] | None = None,
    max_forced_iterations: int = 3,
) -> HonchoLLMCallResponse[Any] | StreamingResponseWithMetadata:
    """Send one function's chat call to its configured gateway model."""
    purpose = _purpose(telemetry)
    workspace = _workspace(telemetry)
    history: list[dict[str, Any]] = (
        list(messages) if messages else [{"role": "user", "content": prompt}]
    )

    def _fit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if max_input_tokens is None:
            return rows
        from src.llm.conversation import truncate_messages_to_fit

        return truncate_messages_to_fit(rows, max_input_tokens)

    history = _fit(history)
    tool_calls_made: list[dict[str, Any]] = []
    total_input = 0
    tool_output = 0
    iterations = 0
    next_round = 0
    limit = max(1, max_tool_iterations)
    parsed: Any = ""
    finish = "stop"
    final_output = 0
    model = ""

    async def _once(
        active_tools: list[dict[str, Any]] | None,
        active_choice: str | dict[str, Any] | None,
        round_index: int,
    ) -> tuple[
        dict[str, Any], str, list[dict[str, Any]], str, int, int, str, int, float
    ]:
        del round_index
        nonlocal next_round
        next_round += 1
        return await _chat(
            purpose=purpose,
            workspace=workspace,
            messages=history,
            max_tokens=max_tokens,
            tools=active_tools,
            tool_choice=active_choice,
            response_model=response_model,
            json_mode=json_mode,
            telemetry=telemetry,
            mark_stream=stream,
            round_index=next_round,
        )

    def _note(
        prompt_tokens: int,
        completion_tokens: int,
        round_model: str,
        attempt: int,
        duration_ms: float,
        round_finish: str,
        call_count: int,
        active_tools: list[dict[str, Any]] | None,
        *,
        outcome: str,
        error: BaseException | None,
    ) -> None:
        _emit_call(
            telemetry=telemetry,
            model=round_model,
            max_tokens=max_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=round_finish,
            outcome=outcome,
            error=error,
            attempt=attempt,
            has_tools=bool(active_tools),
            tool_call_count=call_count,
            was_stream=stream,
            duration_ms=duration_ms,
        )

    async def _accept(
        active_tools: list[dict[str, Any]] | None,
        active_choice: str | dict[str, Any] | None,
        *,
        fetched: bool,
    ) -> Any:
        nonlocal message, finish, content, total_input, final_output, model
        nonlocal prompt_tokens, completion_tokens, duration_ms
        tries = 0
        if not fetched:
            (
                message,
                finish,
                _ignored,
                content,
                prompt_tokens,
                completion_tokens,
                model,
                _attempt,
                duration_ms,
            ) = await _once(active_tools, active_choice, iterations or 1)
            total_input += prompt_tokens
            final_output = completion_tokens
        while True:
            tries += 1
            try:
                value = _interpret(
                    message=message,
                    content=content,
                    finish=finish,
                    response_model=response_model,
                    json_mode=json_mode,
                    purpose=purpose,
                )
            except GatewayStructuredOutputError as exc:
                _note(
                    prompt_tokens,
                    completion_tokens,
                    model,
                    tries,
                    duration_ms,
                    finish,
                    0,
                    active_tools,
                    outcome="error",
                    error=exc,
                )
                if tries >= _attempts():
                    raise
                await asyncio.sleep(_wait())
                (
                    message,
                    finish,
                    _ignored,
                    content,
                    prompt_tokens,
                    completion_tokens,
                    model,
                    _attempt,
                    duration_ms,
                ) = await _once(active_tools, active_choice, iterations or 1)
                total_input += prompt_tokens
                final_output = completion_tokens
                continue
            _note(
                prompt_tokens,
                completion_tokens,
                model,
                tries,
                duration_ms,
                finish,
                0,
                active_tools,
                outcome="success",
                error=None,
            )
            return value

    hit_limit = True
    message: dict[str, Any] = {}
    content = ""
    calls: list[dict[str, Any]] = []
    # Same rule as src/llm/tool_loop.py: a required/any choice relaxes to auto
    # once a gated tool has run, or after max_forced_iterations. An empty gate
    # means there is no gate, so the first successful round is enough.
    effective_tool_choice = tool_choice
    gate = frozenset(force_tools_until or ()) or None
    forced_rounds = 0
    for index in range(limit):
        iterations = index + 1
        history = _fit(history)
        (
            message,
            finish,
            calls,
            content,
            prompt_tokens,
            completion_tokens,
            model,
            attempt,
            duration_ms,
        ) = await _once(tools, effective_tool_choice, iterations)
        total_input += prompt_tokens
        if not calls or tool_executor is None:
            if (
                not calls
                and tool_executor is not None
                and gate is not None
                and effective_tool_choice in ("required", "any")
                and forced_rounds + 1 < max_forced_iterations
                and index < limit - 1
            ):
                forced_rounds += 1
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "Your last response called no tool. Call one of "
                            + ", ".join(sorted(gate))
                            + " to read the relevant memory before answering."
                        ),
                    }
                )
                continue
            hit_limit = False
            final_output = completion_tokens
            parsed = await _accept(tools, effective_tool_choice, fetched=True)
            break
        offered = _offered_names(tools)
        made, results, succeeded = await _execute_tools(calls, tool_executor, offered)
        tool_calls_made.extend(made)
        _append_tools(history, calls, results)
        tool_output += completion_tokens
        _note(
            prompt_tokens,
            completion_tokens,
            model,
            attempt,
            duration_ms,
            finish,
            len(calls),
            tools,
            outcome="success",
            error=None,
        )
        if effective_tool_choice in ("required", "any"):
            forced_rounds += 1
            gate_met = gate is None or bool(succeeded & gate)
            if gate_met or forced_rounds >= max_forced_iterations:
                effective_tool_choice = "auto"
    if hit_limit:
        history.append({"role": "user", "content": _SYNTHESIS})
        iterations += 1
        parsed = await _accept(None, None, fetched=False)
    response = HonchoLLMCallResponse(
        content=parsed,
        output_tokens=tool_output + final_output,
        input_tokens=total_input,
        finish_reasons=[finish],
        tool_calls_made=tool_calls_made,
        iterations=iterations,
    )
    if not stream:
        return response

    # v1 streaming is simulated: the completion is finished, then chunked.
    async def _chunks() -> AsyncIterator[HonchoLLMCallStreamChunk]:
        text = content if isinstance(parsed, str) else ""
        if not isinstance(parsed, str) and response_model is not None:
            text = content
        yield HonchoLLMCallStreamChunk(content=text, is_done=False)
        yield HonchoLLMCallStreamChunk(
            content="",
            is_done=True,
            finish_reasons=[finish],
            output_tokens=final_output,
        )

    return StreamingResponseWithMetadata(
        stream=_chunks(),
        tool_calls_made=tool_calls_made,
        input_tokens=total_input,
        output_tokens=tool_output,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        iterations=iterations,
    )


def _embedding_index(row: dict[str, Any]) -> int:
    index = row.get("index", 0)
    if isinstance(index, int):
        return index
    return 0


async def embed_texts(
    texts: list[str], *, operation: str = "embed"
) -> list[list[float]]:
    """Embed texts with the embed route. No provider SDK and no provider key."""
    if not texts:
        return []
    purpose = "embed"
    tenant = _tenant()
    workspace = _workspace(None)
    base, model = _route(purpose)
    from src.sentica.tokenizer import count_gateway_tokens

    prompt_hold = count_gateway_tokens(texts, model)
    applied = await _reserve(tenant, purpose, max(prompt_hold, 1))
    messages = [{"role": "user", "content": "\n".join(texts)}]
    from src.config import settings

    embed_body: dict[str, Any] = {"model": model, "input": texts}
    if settings.EMBEDDING.resolve_send_dimensions():
        embed_body["dimensions"] = settings.EMBEDDING.VECTOR_DIMENSIONS
    try:
        payload, _attempt = await _post(
            base + "/embeddings",
            embed_body,
            _headers(
                tenant=tenant,
                workspace=workspace,
                purpose=purpose,
                request_id=generate_nanoid(),
                embed_operation=operation,
            ),
            timeout=_timeout_seconds(purpose),
            telemetry=None,
            model=model,
            max_tokens=0,
            has_tools=False,
        )
        prompt_tokens, completion_tokens, present = _usage_pair(
            payload, messages, "", model
        )
        billed = (
            prompt_tokens + completion_tokens
            if present
            else max(prompt_tokens + completion_tokens, 1)
        )
        await _settle_paid(tenant, purpose, billed, applied)
        applied = 0
    finally:
        if applied:
            await _release_hold(tenant, purpose, applied)
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValidationException("Refusing to call: the gateway returned no vectors")
    decoded: list[dict[str, Any]] = []
    for item in cast(list[object], rows):
        row = _mapping(item)
        if row is not None:
            decoded.append(row)
    decoded.sort(key=_embedding_index)
    vectors: list[list[float]] = []
    for row in decoded:
        embedding = row.get("embedding")
        if not isinstance(embedding, list):
            raise ValidationException(
                "Refusing to call: the gateway returned no vectors"
            )
        vector: list[float] = []
        for value in cast(list[object], embedding):
            if not isinstance(value, int | float):
                raise ValidationException(
                    "Refusing to call: the gateway returned no vectors"
                )
            vector.append(float(value))
        vectors.append(vector)
    if len(vectors) != len(texts):
        raise ValidationException("Refusing to call: the gateway returned no vectors")
    return vectors
