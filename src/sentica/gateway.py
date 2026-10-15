"""One OpenAI-compatible gateway for every model call.

Gateway mode is off unless ``SENTICA_GATEWAY_MODE`` is true. Each function
has its own base URL and model. A call never switches to another model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from nanoid import generate as generate_nanoid
from pydantic import BaseModel

from src.exceptions import (
    GatewayBudgetExceeded,
    GatewayUnavailable,
    ValidationException,
)
from src.llm.types import (
    HonchoLLMCallResponse,
    HonchoLLMCallStreamChunk,
    LLMTelemetryContext,
)

logger = logging.getLogger(__name__)

_PURPOSES = ("deriver", "summary", "dialectic", "dream", "embed")
_LOCK = threading.Lock()
_SPENT: dict[tuple[str, str, str], int] = {}


def gateway_enabled() -> bool:
    """Return whether model calls must go through the gateway."""
    raw = os.environ.get("SENTICA_GATEWAY_MODE", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def reset_token_counters() -> None:
    """Drop in-process counters. Tests use this between cases."""
    with _LOCK:
        _SPENT.clear()


def tokens_spent(tenant: str, purpose: str) -> int:
    """Tokens recorded today for one tenant and purpose."""
    key = (tenant, purpose, datetime.now(UTC).date().isoformat())
    with _LOCK:
        return _SPENT.get(key, 0)


def _enabled_flag(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValidationException("Refusing to call: " + name + " is not set")
    return value


def _budget() -> int | None:
    raw = os.environ.get("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationException(
            "Refusing to call: SENTICA_GATEWAY_DAILY_TOKEN_BUDGET is not an integer"
        ) from exc
    if value < 0:
        raise ValidationException(
            "Refusing to call: SENTICA_GATEWAY_DAILY_TOKEN_BUDGET is negative"
        )
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


def _reserve(tenant: str, purpose: str) -> None:
    budget = _budget()
    if budget is None:
        return
    if tokens_spent(tenant, purpose) >= budget:
        raise GatewayBudgetExceeded("token budget exhausted")


def _charge(tenant: str, purpose: str, tokens: int) -> None:
    if tokens < 0:
        return
    key = (tenant, purpose, datetime.now(UTC).date().isoformat())
    with _LOCK:
        _SPENT[key] = _SPENT.get(key, 0) + tokens


def _headers(
    *, tenant: str, workspace: str, purpose: str, request_id: str
) -> dict[str, str]:
    return {
        "X-Sentica-Tenant": tenant,
        "X-Sentica-Workspace": workspace,
        "X-Sentica-Purpose": purpose,
        "X-Sentica-Request-Id": request_id,
    }


def _mapping(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, Any], value)


def _request_id(telemetry: LLMTelemetryContext | None) -> str:
    if telemetry is not None and telemetry.span_id:
        return telemetry.span_id
    return generate_nanoid()


async def _post(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    attempts = int(os.environ.get("SENTICA_GATEWAY_RETRY_ATTEMPTS", "3"))
    wait = float(os.environ.get("SENTICA_GATEWAY_RETRY_WAIT_SECONDS", "0.05"))
    last = GatewayUnavailable("model gateway unavailable")
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "Gateway transport failure on attempt %s for %s",
                attempt,
                headers["X-Sentica-Purpose"],
            )
            last = GatewayUnavailable("model gateway unavailable")
            last.__cause__ = exc
        else:
            if response.status_code < 500 and response.status_code != 429:
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
                return payload
            logger.warning(
                "Gateway status %s on attempt %s for %s",
                response.status_code,
                attempt,
                headers["X-Sentica-Purpose"],
            )
            last = GatewayUnavailable("model gateway unavailable")
        if attempt < attempts:
            await asyncio.sleep(wait)
    raise last


def _usage(payload: dict[str, Any]) -> int:
    usage = _mapping(payload.get("usage"))
    if usage is None:
        return 0
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return 0
    return prompt + completion


async def _chat(
    *,
    purpose: str,
    workspace: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    telemetry: LLMTelemetryContext | None,
) -> tuple[dict[str, Any], str]:
    tenant = _tenant()
    _reserve(tenant, purpose)
    base, model = _route(purpose)
    request_id = _request_id(telemetry)
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    payload = await _post(
        base + "/chat/completions",
        body,
        _headers(
            tenant=tenant,
            workspace=workspace,
            purpose=purpose,
            request_id=request_id,
        ),
    )
    _charge(tenant, purpose, _usage(payload))
    return payload, model


def _message(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValidationException(
            "Refusing to call: the gateway returned no completion"
        )
    first = _mapping(cast(object, choices[0]))
    message = _mapping(None if first is None else first.get("message"))
    if message is None:
        raise ValidationException(
            "Refusing to call: the gateway returned no completion"
        )
    return message


def _embedding_index(row: dict[str, Any]) -> int:
    index = row.get("index", 0)
    if isinstance(index, int):
        return index
    return 0


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
) -> HonchoLLMCallResponse[Any] | AsyncIterator[HonchoLLMCallStreamChunk]:
    """Send one function's chat call to its configured gateway model."""
    purpose = _purpose(telemetry)
    workspace = _workspace(telemetry)
    history: list[dict[str, Any]] = (
        list(messages) if messages else [{"role": "user", "content": prompt}]
    )
    tool_calls_made: list[dict[str, Any]] = []
    content = ""
    output_tokens = 0
    iterations = 0
    for _ in range(max(1, max_tool_iterations)):
        iterations += 1
        payload, _model = await _chat(
            purpose=purpose,
            workspace=workspace,
            messages=history,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            telemetry=telemetry,
        )
        output_tokens += _usage(payload)
        message = _message(payload)
        text = message.get("content")
        content = text if isinstance(text, str) else ""
        calls_value = message.get("tool_calls")
        if not isinstance(calls_value, list) or tool_executor is None:
            break
        history.append(message)
        for item in cast(list[object], calls_value):
            call = _mapping(item)
            if call is None:
                continue
            function = _mapping(call.get("function"))
            if function is None:
                continue
            name = function.get("name")
            if not isinstance(name, str):
                continue
            arguments = function.get("arguments", "{}")
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
            result = tool_executor(name, parsed)
            if isinstance(result, Awaitable):
                result = await result
            tool_calls_made.append({"name": name, "arguments": parsed})
            call_id = call.get("id")
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id if isinstance(call_id, str) else "",
                    "content": str(result),
                }
            )
    parsed_content: Any = content
    if response_model is not None:
        parsed_content = response_model.model_validate_json(content)
    response = HonchoLLMCallResponse(
        content=parsed_content,
        output_tokens=output_tokens,
        input_tokens=0,
        finish_reasons=["stop"],
        tool_calls_made=tool_calls_made,
        iterations=iterations,
    )
    if not stream:
        return response

    async def _chunks() -> AsyncIterator[HonchoLLMCallStreamChunk]:
        yield HonchoLLMCallStreamChunk(content=content, is_done=False)
        yield HonchoLLMCallStreamChunk(
            content="",
            is_done=True,
            finish_reasons=["stop"],
            output_tokens=output_tokens,
        )

    return _chunks()


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts with the embed route. No provider SDK."""
    if not texts:
        return []
    purpose = "embed"
    tenant = _tenant()
    workspace = _workspace(None)
    _reserve(tenant, purpose)
    base, model = _route(purpose)
    payload = await _post(
        base + "/embeddings",
        {"model": model, "input": texts},
        _headers(
            tenant=tenant,
            workspace=workspace,
            purpose=purpose,
            request_id=generate_nanoid(),
        ),
    )
    _charge(tenant, purpose, _usage(payload))
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
