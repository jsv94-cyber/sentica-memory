"""M3: every model function uses one gateway, with headers, budget, and pause."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest
from sqlalchemy import func, select

from src.config import settings
from src.deriver.queue_manager import QueueManager
from src.embedding_client import EmbeddingClient
from src.exceptions import (
    GatewayBudgetExceeded,
    GatewayStructuredOutputError,
    GatewayUnavailable,
    ValidationException,
)
from src.llm.api import honcho_llm_call
from src.llm.types import LLMTelemetryContext
from src.models import Message
from src.sentica.gateway import charge_tokens, reset_token_counters, tokens_spent
from src.sentica.workspace_scope import reset_workspace, set_workspace
from src.utils.summarizer import create_long_summary as real_long_summary
from src.utils.summarizer import create_short_summary as real_short_summary

_ROUTES = {
    "SENTICA_GATEWAY_DERIVER_MODEL": "sentica-deriver",
    "SENTICA_GATEWAY_SUMMARY_MODEL": "sentica-summary",
    "SENTICA_GATEWAY_DIALECTIC_MODEL": "sentica-dialectic",
    "SENTICA_GATEWAY_DREAM_MODEL": "sentica-dream",
    "SENTICA_GATEWAY_EMBED_MODEL": "sentica-embed",
}


class _Gateway(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    status: int
    tool_round: bool
    always_tools: bool
    omit_usage: bool
    delay: float
    scripted: dict[str, str] | None
    replies: list[str] | None

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests = []
        self.status = 200
        self.tool_round = False
        self.always_tools = False
        self.omit_usage = False
        self.delay = 0.0
        self.scripted = None
        self.replies = None


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        gateway = cast(_Gateway, self.server)
        if gateway.delay:
            time.sleep(gateway.delay)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        parsed: object = json.loads(raw.decode())
        body = cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}
        model = body.get("model")
        record = {
            "path": self.path,
            "tenant": self.headers.get("X-Sentica-Tenant"),
            "workspace": self.headers.get("X-Sentica-Workspace"),
            "purpose": self.headers.get("X-Sentica-Purpose"),
            "request_id": self.headers.get("X-Sentica-Request-Id"),
            "model": model if isinstance(model, str) else None,
            "body": body,
            "embed_op": self.headers.get("X-Sentica-Embed-Op"),
        }
        gateway.requests.append(record)
        if gateway.status != 200:
            self.send_response(gateway.status)
            self.end_headers()
            return
        if self.path.endswith("/embeddings"):
            inputs = body.get("input")
            counted = cast(list[object], inputs) if isinstance(inputs, list) else []
            count = len(counted)
            payload = {
                "data": [
                    {"index": index, "embedding": [0.25, 0.5]} for index in range(count)
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 0},
            }
        elif gateway.replies:
            index = len(gateway.requests) - 1
            content = gateway.replies[min(index, len(gateway.replies) - 1)]
            payload = {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        elif gateway.scripted is not None:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": gateway.scripted.get("content", "ok"),
            }
            refusal = gateway.scripted.get("refusal")
            if refusal:
                message["refusal"] = refusal
            payload = {
                "choices": [
                    {
                        "finish_reason": gateway.scripted.get("finish", "stop"),
                        "message": message,
                    }
                ]
            }
        elif (gateway.always_tools or gateway.tool_round) and body.get("tools"):
            if gateway.tool_round and len(gateway.requests) != 1:
                payload = {
                    "choices": [
                        {"message": {"role": "assistant", "content": "gateway ok"}}
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                }
            else:
                payload = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_memory",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
        else:
            payload = {
                "choices": [
                    {"message": {"role": "assistant", "content": "gateway ok"}}
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        if gateway.omit_usage:
            payload.pop("usage", None)
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Gateway]:
    from tests.sentica.resp_redis import RespRedis

    redis = RespRedis()
    monkeypatch.setenv("SENTICA_GATEWAY_REDIS_URL", redis.url)
    reset_token_counters()
    server = _Gateway()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = cast(tuple[str, int], server.server_address)
    host, port = address
    base = f"http://{host}:{port}/v1"
    monkeypatch.setenv("SENTICA_GATEWAY_MODE", "true")
    monkeypatch.setenv("SENTICA_GATEWAY_BASE_URL", base)
    monkeypatch.setenv("SENTICA_TENANT_ID", "tenant-a")
    monkeypatch.setenv("SENTICA_GATEWAY_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("SENTICA_GATEWAY_RETRY_WAIT_SECONDS", "0.001")
    monkeypatch.delenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", raising=False)
    for name, model in _ROUTES.items():
        monkeypatch.setenv(name, model)
    yield server
    reset_token_counters()
    server.shutdown()
    redis.close()


def _forbid_sdks(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider SDK was constructed")

    for module_name, attribute in (
        ("openai", "AsyncOpenAI"),
        ("google.genai", "Client"),
    ):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, attribute):
            monkeypatch.setattr(module, attribute, _boom)


async def _pause(error: Exception) -> bool:
    marked: list[str] = []

    async def _mark(*_args: object, **_kwargs: object) -> None:
        marked.append("errored")

    manager = QueueManager.__new__(QueueManager)
    manager.mark_queue_item_as_errored = _mark  # type: ignore[method-assign]
    paused = await QueueManager._handle_processing_error(  # pyright: ignore[reportPrivateUsage]
        manager,
        error,
        [],
        "wu",
        "summary",
    )
    assert marked == []
    return paused


async def _call(purpose: str, parent: str) -> None:
    await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt="hello",
        max_tokens=16,
        telemetry=LLMTelemetryContext(
            workspace_name="left",
            call_purpose=purpose,
            parent_category=parent,
        ),
    )


async def test_each_function_calls_the_gateway_with_headers(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_sdks(monkeypatch)
    await _call("deriver.representation", "representation")
    await real_short_summary(
        "local hello",
        20,
        telemetry=LLMTelemetryContext(workspace_name="left"),
    )
    await real_long_summary(
        "local hello",
        telemetry=LLMTelemetryContext(workspace_name="left"),
    )
    await _call("dialectic.answer", "dialectic")
    await _call("dream.deduction", "dream")
    token = set_workspace("left")
    try:
        vector = await EmbeddingClient.embed(EmbeddingClient(), "local hello")
    finally:
        reset_workspace(token)
    assert vector == [0.25, 0.5]
    purposes = [row["purpose"] for row in gateway.requests]
    assert purposes == [
        "deriver",
        "summary",
        "summary",
        "dialectic",
        "dream",
        "embed",
    ]
    models = {row["purpose"]: row["model"] for row in gateway.requests}
    assert models["deriver"] == "sentica-deriver"
    assert models["summary"] == "sentica-summary"
    assert models["dialectic"] == "sentica-dialectic"
    assert models["dream"] == "sentica-dream"
    assert models["embed"] == "sentica-embed"
    request_ids = [row["request_id"] for row in gateway.requests]
    assert all(request_ids)
    assert len(set(request_ids)) == len(request_ids)
    for row in gateway.requests:
        assert row["tenant"] == "tenant-a"
        assert row["workspace"] == "left"


async def test_dialectic_tool_call_stays_on_the_same_model(gateway: _Gateway) -> None:
    gateway.tool_round = True

    async def _search(_name: str, _arguments: dict[str, Any]) -> str:
        return "found"

    await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt="find it",
        max_tokens=16,
        tools=[{"type": "function", "function": {"name": "search_memory"}}],
        tool_executor=_search,
        telemetry=LLMTelemetryContext(
            workspace_name="left",
            call_purpose="dialectic.answer",
            parent_category="dialectic",
        ),
    )
    assert [row["model"] for row in gateway.requests] == [
        "sentica-dialectic",
        "sentica-dialectic",
    ]


async def test_budget_stop_does_not_switch_model(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One tenant total covers every purpose. The model does not change."""
    monkeypatch.setenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", "5")
    await _call("deriver.representation", "representation")
    with pytest.raises(GatewayBudgetExceeded, match="token budget exhausted") as caught:
        await _call("summary.short", "summary")
    assert caught.value.status_code == 429
    assert caught.value.detail == "token budget exhausted"
    monkeypatch.setenv("SENTICA_TENANT_ID", "tenant-b")
    await _call("deriver.representation", "representation")
    assert [row["model"] for row in gateway.requests] == [
        "sentica-deriver",
        "sentica-deriver",
    ]
    assert [row["tenant"] for row in gateway.requests] == [
        "tenant-a",
        "tenant-b",
    ]
    paused = await _pause(GatewayBudgetExceeded("token budget exhausted"))
    assert paused is True
    budget_pause = QueueManager()
    await QueueManager._handle_processing_error(  # pyright: ignore[reportPrivateUsage]
        budget_pause,
        GatewayBudgetExceeded("token budget exhausted"),
        [],
        "wu",
        "summary",
    )
    assert budget_pause._gateway_outage_pending is True  # pyright: ignore[reportPrivateUsage]


async def test_gateway_outage_pauses_without_deleting_messages(
    gateway: _Gateway, db_session: Any
) -> None:
    before = await db_session.scalar(select(func.count()).select_from(Message))
    gateway.status = 500
    with pytest.raises(GatewayUnavailable, match="model gateway unavailable"):
        await _call("summary.short", "summary")
    after = await db_session.scalar(select(func.count()).select_from(Message))
    assert before == after
    assert len(gateway.requests) == 3
    assert {row["model"] for row in gateway.requests} == {"sentica-summary"}
    request_ids = [str(row["request_id"]) for row in gateway.requests]
    stems = {item.rsplit(":r", 1)[0] for item in request_ids}
    assert len(stems) == 1
    assert [item.rsplit(":a", 1)[-1] for item in request_ids] == ["1", "2", "3"]
    paused = await _pause(GatewayUnavailable("model gateway unavailable"))
    assert paused is True


def _round_indexes(request_ids: list[str]) -> list[str]:
    return [item.rsplit(":a", 1)[0].rsplit(":r", 1)[-1] for item in request_ids]


async def test_each_round_gets_its_own_request_id(gateway: _Gateway) -> None:
    """Structured-output retry and the final no-tools call use a new round."""
    from pydantic import BaseModel

    class _Line(BaseModel):
        content: str

    gateway.replies = ["not-json", '{"content":"kept"}']
    parsed = await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt="hello",
        max_tokens=16,
        response_model=_Line,
        telemetry=LLMTelemetryContext(
            workspace_name="left",
            call_purpose="summary.short",
            parent_category="summary",
        ),
    )
    assert isinstance(parsed.content, _Line)
    assert parsed.content.content == "kept"
    structured_ids = [str(row["request_id"]) for row in gateway.requests]
    assert _round_indexes(structured_ids) == ["1", "2"]
    assert [item.rsplit(":a", 1)[-1] for item in structured_ids] == ["1", "1"]
    assert len(set(structured_ids)) == len(structured_ids)

    gateway.replies = None
    gateway.requests.clear()
    gateway.always_tools = True

    async def _search(_name: str, _arguments: dict[str, Any]) -> str:
        return "found"

    await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt="find it",
        max_tokens=16,
        tools=[{"type": "function", "function": {"name": "search_memory"}}],
        tool_executor=_search,
        max_tool_iterations=1,
        telemetry=LLMTelemetryContext(
            workspace_name="left",
            call_purpose="dialectic.answer",
            parent_category="dialectic",
        ),
    )
    loop_ids = [str(row["request_id"]) for row in gateway.requests]
    assert _round_indexes(loop_ids) == ["1", "2"]
    assert [item.rsplit(":a", 1)[-1] for item in loop_ids] == ["1", "1"]
    assert len(set(loop_ids)) == len(loop_ids)


async def test_missing_tenant_workspace_or_model_does_not_call(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTICA_TENANT_ID")
    with pytest.raises(ValidationException, match="SENTICA_TENANT_ID"):
        await _call("deriver.representation", "representation")
    monkeypatch.setenv("SENTICA_TENANT_ID", "tenant-a")
    with pytest.raises(ValidationException, match="workspace is missing"):
        await honcho_llm_call(
            model_config=settings.SUMMARY.MODEL_CONFIG,
            prompt="hello",
            max_tokens=16,
            telemetry=LLMTelemetryContext(
                call_purpose="dream.deduction",
                parent_category="dream",
            ),
        )
    monkeypatch.delenv("SENTICA_GATEWAY_EMBED_MODEL")
    token = set_workspace("left")
    try:
        with pytest.raises(ValidationException, match="SENTICA_GATEWAY_EMBED_MODEL"):
            await EmbeddingClient.embed(EmbeddingClient(), "local hello")
    finally:
        reset_workspace(token)
    assert gateway.requests == []


async def test_purpose_limit_does_not_block_another_purpose(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTICA_GATEWAY_DERIVER_DAILY_TOKEN_BUDGET", "5")
    await _call("deriver.representation", "representation")
    with pytest.raises(GatewayBudgetExceeded):
        await _call("deriver.representation", "representation")
    await _call("summary.short", "summary")
    assert [row["purpose"] for row in gateway.requests] == ["deriver", "summary"]


async def test_missing_usage_is_charged_an_estimate(gateway: _Gateway) -> None:
    gateway.omit_usage = True
    await _call("deriver.representation", "representation")
    assert tokens_spent("tenant-a") >= 1
    assert tokens_spent("tenant-a", "deriver") >= 1


async def test_budget_is_shared_across_processes(gateway: _Gateway) -> None:
    del gateway
    await charge_tokens("tenant-share", "deriver", 4)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio\nfrom src.sentica.gateway import charge_tokens\nasyncio.run(charge_tokens('tenant-share', 'summary', 3))\n",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0, completed.stderr
    assert tokens_spent("tenant-share") == 7


async def test_tokens_and_completed_event_use_prompt_and_completion(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.telemetry.events.llm import LLMCallCompletedEvent

    seen: list[object] = []

    def _capture(event: object) -> None:
        seen.append(event)

    monkeypatch.setattr("src.telemetry.events.emit", _capture)
    response = await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt="hello",
        max_tokens=16,
        telemetry=LLMTelemetryContext(
            workspace_name="left",
            call_purpose="deriver.representation",
            parent_category="representation",
        ),
    )
    assert response.input_tokens == 4
    assert response.output_tokens == 2
    events = [event for event in seen if isinstance(event, LLMCallCompletedEvent)]
    assert len(events) == 1
    assert events[0].provider_input_tokens == 4
    assert events[0].provider_output_tokens == 2
    assert events[0].transport == "openai"
    assert events[0].outcome == "success"
    assert gateway.requests


async def test_honcho_tools_leave_as_openai_functions(gateway: _Gateway) -> None:
    async def _search(_name: str, _arguments: dict[str, Any]) -> str:
        return "found"

    await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt="find it",
        max_tokens=16,
        tools=[
            {
                "name": "grep_messages",
                "description": "find messages",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            }
        ],
        tool_executor=_search,
        telemetry=LLMTelemetryContext(
            workspace_name="left",
            call_purpose="dialectic.answer",
            parent_category="dialectic",
        ),
    )
    body = cast(dict[str, Any], gateway.requests[0]["body"])
    tool = cast(dict[str, Any], cast(list[object], body["tools"])[0])
    function = cast(dict[str, Any], tool["function"])
    assert tool["type"] == "function"
    assert function["name"] == "grep_messages"
    assert isinstance(function["parameters"], dict)


async def test_tool_limit_makes_a_final_call_without_tools(gateway: _Gateway) -> None:
    gateway.always_tools = True

    async def _search(_name: str, _arguments: dict[str, Any]) -> str:
        return "found"

    await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt="find it",
        max_tokens=16,
        tools=[
            {
                "name": "search_memory",
                "description": "search",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        tool_executor=_search,
        max_tool_iterations=1,
        telemetry=LLMTelemetryContext(
            workspace_name="left",
            call_purpose="dialectic.answer",
            parent_category="dialectic",
        ),
    )
    bodies = [cast(dict[str, Any], row["body"]) for row in gateway.requests]
    assert "tools" in bodies[0]
    assert "tools" not in bodies[-1]
    encoded = json.dumps(bodies[-1]["messages"])
    assert "maximum number of tool calls" in encoded


async def test_fenced_json_schema_parses(gateway: _Gateway) -> None:
    from src.utils.representation import PromptRepresentation

    gateway.scripted = {
        "content": '```json\n{"explicit":[]}\n```',
        "finish": "stop",
    }
    response = await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt="remember",
        max_tokens=16,
        response_model=PromptRepresentation,
        json_mode=True,
        telemetry=LLMTelemetryContext(
            workspace_name="left",
            call_purpose="deriver.representation",
            parent_category="representation",
        ),
    )
    assert isinstance(response.content, PromptRepresentation)
    assert response.content.explicit == []
    body = cast(dict[str, Any], gateway.requests[0]["body"])
    response_format = cast(dict[str, Any], body["response_format"])
    assert response_format["type"] == "json_schema"
    schema = cast(dict[str, Any], response_format["json_schema"])
    assert schema["name"] == "PromptRepresentation"


async def test_malformed_or_refused_output_fails_without_sentry_or_the_text(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sentry_sdk

    from src.utils.representation import PromptRepresentation

    captured: list[object] = []

    def _capture(*args: object, **kwargs: object) -> None:
        del kwargs
        captured.append(args)

    monkeypatch.setattr(sentry_sdk, "capture_exception", _capture)
    monkeypatch.setenv("SENTICA_GATEWAY_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("SENTICA_GATEWAY_RETRY_WAIT_SECONDS", "0.001")
    gateway.scripted = {"content": "not-json SECRET-CANARY", "finish": "stop"}
    with pytest.raises(GatewayStructuredOutputError) as caught:
        await honcho_llm_call(
            model_config=settings.SUMMARY.MODEL_CONFIG,
            prompt="remember",
            max_tokens=16,
            response_model=PromptRepresentation,
            json_mode=True,
            telemetry=LLMTelemetryContext(
                workspace_name="left",
                call_purpose="deriver.representation",
                parent_category="representation",
            ),
        )
    assert "SECRET-CANARY" not in str(caught.value)
    assert caught.value.sentry_policy is None
    assert captured == []
    assert len(gateway.requests) == 2
    gateway.requests.clear()
    gateway.scripted = {"content": "", "finish": "stop", "refusal": "SECRET-REFUSAL"}
    with pytest.raises(GatewayStructuredOutputError) as refused:
        await honcho_llm_call(
            model_config=settings.SUMMARY.MODEL_CONFIG,
            prompt="remember",
            max_tokens=16,
            response_model=PromptRepresentation,
            telemetry=LLMTelemetryContext(
                workspace_name="left",
                call_purpose="deriver.representation",
                parent_category="representation",
            ),
        )
    assert "SECRET-REFUSAL" not in str(refused.value)
    assert captured == []


async def test_empty_representation_on_a_clean_stop_is_kept(gateway: _Gateway) -> None:
    from src.utils.representation import PromptRepresentation

    gateway.scripted = {"content": '{"explicit":[]}', "finish": "stop"}
    response = await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt="remember",
        max_tokens=16,
        response_model=PromptRepresentation,
        telemetry=LLMTelemetryContext(
            workspace_name="left",
            call_purpose="deriver.representation",
            parent_category="representation",
        ),
    )
    assert isinstance(response.content, PromptRepresentation)
    gateway.requests.clear()
    gateway.scripted = {"content": '{"explicit":[]}', "finish": "length"}
    with pytest.raises(GatewayStructuredOutputError):
        await honcho_llm_call(
            model_config=settings.SUMMARY.MODEL_CONFIG,
            prompt="remember",
            max_tokens=16,
            response_model=PromptRepresentation,
            telemetry=LLMTelemetryContext(
                workspace_name="left",
                call_purpose="deriver.representation",
                parent_category="representation",
            ),
        )


async def test_empty_capped_summary_is_not_replaced(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.utils.summarizer import SummaryType
    from src.utils.summarizer import (
        _create_summary as create_summary,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setattr("src.utils.summarizer.create_short_summary", real_short_summary)
    gateway.scripted = {"content": "", "finish": "length"}
    with pytest.raises(GatewayStructuredOutputError):
        await create_summary(
            "local hello",
            None,
            SummaryType.SHORT,
            10,
            "m1",
            1,
            1,
            "local hello",
            1,
            telemetry=LLMTelemetryContext(
                workspace_name="left",
                call_purpose="summary.short",
                parent_category="summary",
            ),
        )


async def test_purpose_timeout_stops_the_call(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.delay = 0.4
    monkeypatch.setenv("SENTICA_GATEWAY_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("SENTICA_GATEWAY_DIALECTIC_TIMEOUT_SECONDS", "0.05")
    with pytest.raises(GatewayUnavailable):
        await _call("dialectic.answer", "dialectic")


async def test_gateway_embeddings_do_not_construct_a_provider_client(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.embedding_client import EmbeddingTokenLimitError

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider SDK was constructed")

    import openai
    from google import genai

    monkeypatch.setattr(openai, "AsyncOpenAI", _boom)
    monkeypatch.setattr(openai, "OpenAI", _boom)
    monkeypatch.setattr(genai, "Client", _boom)

    def _cap(_self: object) -> int:
        return 4

    monkeypatch.setattr(EmbeddingClient, "_local_token_cap", _cap)
    for name in (
        "embed",
        "simple_batch_embed",
        "prepare_chunks",
        "batch_embed",
        "truncate_to_token_limit",
    ):
        monkeypatch.setattr(
            EmbeddingClient(),
            name,
            getattr(EmbeddingClient, name).__get__(EmbeddingClient()),
        )
    text = "alpha beta gamma delta epsilon zeta eta theta"
    token = set_workspace("left")
    try:
        client = EmbeddingClient()
        assert client.provider
        assert client.model == "sentica-embed"
        assert client.vector_dimensions == settings.EMBEDDING.VECTOR_DIMENSIONS
        assert client.encoding.encode("ab")
        truncated = client.truncate_to_token_limit(text)
        assert len(client.encoding.encode(truncated)) <= 4
        chunks = client.prepare_chunks({"doc": text})
        assert len(chunks["doc"]) > 1
        with pytest.raises(EmbeddingTokenLimitError):
            await client.simple_batch_embed([text])
        embedded = await client.batch_embed({"doc": text})
    finally:
        reset_workspace(token)
    assert len(embedded["doc"]) > 1
    bodies = [
        cast(dict[str, Any], row["body"])
        for row in gateway.requests
        if str(row["path"]).endswith("/embeddings")
    ]
    inputs: list[object] = []
    for body in bodies:
        incoming = body["input"]
        if isinstance(incoming, list):
            inputs.extend(cast(list[object], incoming))
    assert text not in inputs
    assert len(inputs) == len(embedded["doc"])
    assert any(row["embed_op"] == "batch_embed" for row in gateway.requests)


async def test_gateway_outage_backs_off_before_the_next_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.deriver import queue_manager as qm_mod

    monkeypatch.setattr(settings.DERIVER, "POLLING_JITTER_RATIO", 0.0)
    monkeypatch.setattr(settings.DERIVER, "POLLING_BACKOFF_ENABLED", True)
    monkeypatch.setattr(settings.DERIVER, "POLLING_SLEEP_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(settings.DERIVER, "POLLING_BACKOFF_MULTIPLIER", 2.0)
    monkeypatch.setattr(settings.DERIVER, "POLLING_SLEEP_MAX_INTERVAL_SECONDS", 30.0)
    monkeypatch.setattr(settings.DERIVER, "SCHEDULER", "api")
    manager = qm_mod.QueueManager()
    flagged = await qm_mod.QueueManager._handle_processing_error(  # pyright: ignore[reportPrivateUsage]
        manager,
        GatewayUnavailable("model gateway unavailable"),
        [],
        "wu",
        "summary",
    )
    assert flagged is True
    assert manager._gateway_outage_pending is True  # pyright: ignore[reportPrivateUsage]
    events: list[tuple[str, float]] = []

    async def fake_claim() -> dict[str, str]:
        events.append(("claim", manager._current_poll_interval))  # pyright: ignore[reportPrivateUsage]
        manager.shutdown_event.set()
        return {"representation:ws:s:p": "aqs"}

    async def fake_process(*_args: object) -> None:
        return None

    async def fake_sleep(seconds: float) -> None:
        events.append(("sleep", seconds))

    monkeypatch.setattr(manager, "get_and_claim_work_units", fake_claim)
    monkeypatch.setattr(manager, "process_work_unit", fake_process)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await manager.polling_loop()
    assert events[0] == ("sleep", 1.0)
    assert events[1][0] == "claim"
    assert manager._current_poll_interval == 2.0  # pyright: ignore[reportPrivateUsage]


async def test_budget_exhaustion_backs_off_before_the_next_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.deriver import queue_manager as qm_mod

    monkeypatch.setattr(settings.DERIVER, "POLLING_JITTER_RATIO", 0.0)
    monkeypatch.setattr(settings.DERIVER, "POLLING_BACKOFF_ENABLED", True)
    monkeypatch.setattr(settings.DERIVER, "POLLING_SLEEP_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(settings.DERIVER, "POLLING_BACKOFF_MULTIPLIER", 2.0)
    monkeypatch.setattr(settings.DERIVER, "POLLING_SLEEP_MAX_INTERVAL_SECONDS", 30.0)
    monkeypatch.setattr(settings.DERIVER, "SCHEDULER", "api")
    manager = qm_mod.QueueManager()
    flagged = await qm_mod.QueueManager._handle_processing_error(  # pyright: ignore[reportPrivateUsage]
        manager,
        GatewayBudgetExceeded("token budget exhausted"),
        [],
        "wu",
        "summary",
    )
    assert flagged is True
    assert manager._gateway_outage_pending is True  # pyright: ignore[reportPrivateUsage]
    events: list[tuple[str, float]] = []

    async def fake_claim() -> dict[str, str]:
        events.append(("claim", manager._current_poll_interval))  # pyright: ignore[reportPrivateUsage]
        manager.shutdown_event.set()
        return {"representation:ws:s:p": "aqs"}

    async def fake_sleep(seconds: float) -> None:
        events.append(("sleep", seconds))

    monkeypatch.setattr(manager, "get_and_claim_work_units", fake_claim)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await manager.polling_loop()
    assert events[0] == ("sleep", 1.0)
    assert events[1][0] == "claim"


async def test_unavailable_statuses_and_a_charge_failure_keep_the_completion(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.exceptions import GatewayUnavailable
    from src.sentica.gateway import (
        _tool_calls,  # pyright: ignore[reportPrivateUsage]
    )

    for status in (401, 403, 404, 408):
        gateway.status = status
        gateway.requests.clear()
        with pytest.raises(GatewayUnavailable):
            await _call("summary.short", "summary")
    gateway.status = 200
    monkeypatch.setenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", "10")
    monkeypatch.setattr("src.sentica.gateway._async_client", lambda: None)
    with pytest.raises(GatewayUnavailable):
        await _call("summary.short", "summary")
    monkeypatch.delenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET")

    async def _charge_down(*_args: object, **_kwargs: object) -> None:
        raise GatewayUnavailable("budget store down")

    monkeypatch.setattr("src.sentica.gateway._charge", _charge_down)
    response = await honcho_llm_call(
        model_config=settings.SUMMARY.MODEL_CONFIG,
        prompt="hello",
        max_tokens=16,
        telemetry=LLMTelemetryContext(
            workspace_name="left",
            call_purpose="summary.short",
            parent_category="summary",
        ),
    )
    assert response.content == "gateway ok"
    parsed = _tool_calls(
        {"tool_calls": [{"id": "1", "function": {"name": "search", "arguments": ""}}]}
    )
    assert parsed[0]["input"] == {}
    with pytest.raises(ValidationException, match="malformed"):
        _tool_calls({"tool_calls": [{"id": "1"}]})


async def test_gateway_forwards_limits_dimensions_and_truncation(
    gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.llm.conversation import truncate_messages_to_fit
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    seen: dict[str, int] = {}
    real = truncate_messages_to_fit

    def _wrapped(
        messages: list[dict[str, Any]],
        max_tokens: int,
        preserve_system: bool = True,
    ) -> list[dict[str, Any]]:
        seen["max"] = max_tokens
        return real(messages, max_tokens, preserve_system)

    monkeypatch.setattr("src.llm.conversation.truncate_messages_to_fit", _wrapped)

    def _send_dimensions(self: object) -> bool:
        del self
        return True

    monkeypatch.setattr(
        type(settings.EMBEDDING),
        "resolve_send_dimensions",
        _send_dimensions,
    )
    token = set_workspace("left")
    try:
        await honcho_llm_call(
            model_config=settings.SUMMARY.MODEL_CONFIG,
            prompt="hello",
            max_tokens=16,
            max_input_tokens=40,
            telemetry=LLMTelemetryContext(
                workspace_name="left",
                call_purpose="summary.short",
                parent_category="summary",
            ),
        )
        from src.sentica.gateway import embed_texts

        await embed_texts(["tea"])
    finally:
        reset_workspace(token)
    assert seen["max"] == 40
    bodies = [cast(dict[str, Any], row["body"]) for row in gateway.requests]
    assert any(
        body.get("dimensions") == settings.EMBEDDING.VECTOR_DIMENSIONS
        for body in bodies
    )
