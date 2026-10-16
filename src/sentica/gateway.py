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
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
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
    url = _redis_url()
    if url is None:
        return None
    import redis.asyncio as aioredis

    connect = cast(Callable[..., Any], aioredis.Redis.from_url)
    return connect(url, decode_responses=True, protocol=2, socket_timeout=2.0)


def reset_token_counters() -> None:
    """Delete this process's budget keys. Tests use this between cases."""
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


async def _read_spent(client: Any, tenant: str, purpose: str) -> tuple[int, int]:
    total = await client.get(_total_key(tenant))
    spent = await client.get(_purpose_key(tenant, purpose))
    return int(total or 0), int(spent or 0)


async def _reserve(tenant: str, purpose: str) -> None:
    total_limit, purpose_limit = _budgets(purpose)
    if total_limit is None and purpose_limit is None:
        return
    client = _async_client()
    if client is None:
        raise GatewayUnavailable(
            "Refusing to call: a gateway budget is set and Redis is not configured"
        )
    try:
        try:
            total, spent = await _read_spent(client, tenant, purpose)
        except GatewayBudgetExceeded:
            raise
        except Exception as exc:
            raise GatewayUnavailable(
                "Refusing to call: the gateway budget store is unavailable"
            ) from exc
    finally:
        await client.aclose()
    if total_limit is not None and total >= total_limit:
        raise GatewayBudgetExceeded("token budget exhausted")
    if purpose_limit is not None and spent >= purpose_limit:
        raise GatewayBudgetExceeded("token budget exhausted")


async def _charge(tenant: str, purpose: str, tokens: int) -> None:
    if tokens < 1:
        tokens = 1
    total_limit, purpose_limit = _budgets(purpose)
    client = _async_client()
    if client is None:
        if total_limit is not None or purpose_limit is not None:
            raise GatewayUnavailable(
                "Refusing to call: a gateway budget is set and Redis is not configured"
            )
        return
    try:
        try:
            await client.incrby(_total_key(tenant), tokens)
            await client.expire(_total_key(tenant), _BUDGET_TTL_SECONDS)
            await client.incrby(_purpose_key(tenant, purpose), tokens)
            await client.expire(_purpose_key(tenant, purpose), _BUDGET_TTL_SECONDS)
        except Exception as exc:
            if total_limit is not None or purpose_limit is not None:
                raise GatewayUnavailable(
                    "Refusing to call: the gateway budget store is unavailable"
                ) from exc
            logger.warning("gateway budget store did not record tokens")
    finally:
        await client.aclose()


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
    payload: dict[str, Any], messages: list[dict[str, Any]], content: str
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
    from src.utils.tokens import estimate_tokens

    texts: list[str] = []
    for message in messages:
        value = message.get("content")
        if isinstance(value, str) and value:
            texts.append(value)
    if content:
        texts.append(content)
    estimate = estimate_tokens(texts)
    if estimate < 1:
        estimate = 1
    return estimate, 0, False


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


async def _charge_paid(tenant: str, purpose: str, tokens: int) -> None:
    """Record a completed call. A store failure does not discard the completion."""
    try:
        await _charge(tenant, purpose, tokens)
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
    await _reserve(tenant, purpose)
    base, model = _route(purpose)
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
    prompt_tokens, completion_tokens, present = _usage_pair(payload, messages, content)
    billed = (
        prompt_tokens + completion_tokens
        if present
        else max(prompt_tokens + completion_tokens, 1)
    )
    await _charge_paid(tenant, purpose, billed)
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


async def _execute_tools(
    calls: list[dict[str, Any]],
    tool_executor: Callable[[str, dict[str, Any]], Any],
    offered: set[str],
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    from src.utils.agent_tools import unoffered_tool_refusal

    made: list[dict[str, Any]] = []
    results: list[tuple[str, str]] = []
    for call in calls:
        name = str(call["name"])
        parsed = cast(dict[str, Any], call["input"])
        if name not in offered:
            result: Any = unoffered_tool_refusal(name)
        else:
            result = tool_executor(name, parsed)
            if isinstance(result, Awaitable):
                result = await result
        made.append({"tool_name": name, "tool_input": parsed, "name": name})
        results.append((str(call["id"]), str(result)))
    return made, results


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
        ) = await _once(tools, tool_choice, iterations)
        total_input += prompt_tokens
        if not calls or tool_executor is None:
            hit_limit = False
            final_output = completion_tokens
            parsed = await _accept(tools, tool_choice, fetched=True)
            break
        offered = {
            name
            for tool in (tools or [])
            if isinstance((name := tool.get("name")), str)
        }
        made, results = await _execute_tools(calls, tool_executor, offered)
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
    await _reserve(tenant, purpose)
    base, model = _route(purpose)
    messages = [{"role": "user", "content": "\n".join(texts)}]
    from src.config import settings

    embed_body: dict[str, Any] = {"model": model, "input": texts}
    if settings.EMBEDDING.resolve_send_dimensions():
        embed_body["dimensions"] = settings.EMBEDDING.VECTOR_DIMENSIONS
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
    prompt_tokens, completion_tokens, present = _usage_pair(payload, messages, "")
    billed = (
        prompt_tokens + completion_tokens
        if present
        else max(prompt_tokens + completion_tokens, 1)
    )
    await _charge_paid(tenant, purpose, billed)
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
