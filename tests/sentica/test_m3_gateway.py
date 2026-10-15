"""M3: every model function uses one gateway, with headers, budget, and pause."""

from __future__ import annotations

import json
import sys
import threading
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
    GatewayUnavailable,
    ValidationException,
)
from src.llm.api import honcho_llm_call
from src.llm.types import LLMTelemetryContext
from src.models import Message
from src.sentica.gateway import reset_token_counters
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
    requests: list[dict[str, str | None]]
    status: int
    tool_round: bool

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests = []
        self.status = 200
        self.tool_round = False


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        gateway = cast(_Gateway, self.server)
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
        elif gateway.tool_round and len(gateway.requests) == 1:
            payload = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
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
    monkeypatch.setenv("SENTICA_GATEWAY_RETRY_WAIT_SECONDS", "0")
    monkeypatch.delenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", raising=False)
    for name, model in _ROUTES.items():
        monkeypatch.setenv(name, model)
    yield server
    server.shutdown()
    reset_token_counters()


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
    monkeypatch.setenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", "5")
    await _call("deriver.representation", "representation")
    with pytest.raises(GatewayBudgetExceeded, match="token budget exhausted") as caught:
        await _call("deriver.representation", "representation")
    assert caught.value.status_code == 429
    assert caught.value.detail == "token budget exhausted"
    await _call("summary.short", "summary")
    monkeypatch.setenv("SENTICA_TENANT_ID", "tenant-b")
    await _call("deriver.representation", "representation")
    assert [row["model"] for row in gateway.requests] == [
        "sentica-deriver",
        "sentica-summary",
        "sentica-deriver",
    ]
    assert [row["tenant"] for row in gateway.requests] == [
        "tenant-a",
        "tenant-a",
        "tenant-b",
    ]
    paused = await _pause(GatewayBudgetExceeded("token budget exhausted"))
    assert paused is True


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
    assert len({row["request_id"] for row in gateway.requests}) == 1
    paused = await _pause(GatewayUnavailable("model gateway unavailable"))
    assert paused is True


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
