"""OpenAI-compatible gateway stand-in.

The server records every chat and embedding request. When ``strict`` is set,
a tool list must already be in OpenAI shape and the Sentica headers must be
present. ``serve`` blocks for the runtime-role job.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

Decide = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]

_HEADER_NAMES = (
    "X-Sentica-Tenant",
    "X-Sentica-Workspace",
    "X-Sentica-Purpose",
    "X-Sentica-Request-Id",
)


def tool_shape_error(body: dict[str, Any]) -> str | None:
    """Return why a tool list is not OpenAI function shape."""
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    for tool in cast(list[object], tools):
        if not isinstance(tool, dict):
            return "tool"
        raw = cast(dict[str, Any], tool)
        if raw.get("type") != "function":
            return "type"
        function = raw.get("function")
        if not isinstance(function, dict):
            return "function"
        if not isinstance(cast(dict[str, Any], function).get("parameters"), dict):
            return "parameters"
    return None


def header_error(headers: dict[str, str]) -> str | None:
    for name in _HEADER_NAMES:
        if not headers.get(name):
            return name
    return None


class MockGateway(ThreadingHTTPServer):
    """In-process gateway. ``base_url`` includes ``/v1``."""

    records: list[dict[str, Any]]
    decide: Decide
    dimensions: int
    strict: bool

    def __init__(
        self,
        decide: Decide | None = None,
        *,
        dimensions: int = 1536,
        strict: bool = True,
        port: int = 0,
    ) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.records = []
        self.dimensions = dimensions
        self.strict = strict
        self.decide = decide or self._default_decide
        self._thread: threading.Thread | None = None

    def _default_decide(
        self, path: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        del headers
        if path.rstrip("/").endswith("/embeddings"):
            incoming = body.get("input", [])
            texts = [incoming] if isinstance(incoming, str) else list(incoming)
            return {
                "data": [
                    {
                        "index": index,
                        "embedding": [0.25] * self.dimensions,
                    }
                    for index in range(len(texts))
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 0},
            }
        response_format = body.get("response_format")
        content = "ok"
        if (
            isinstance(response_format, dict)
            and cast(dict[str, Any], response_format).get("type") == "json_schema"
        ):
            content = '{"explicit":[]}'
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }

    @property
    def base_url(self) -> str:
        host, port = cast(tuple[str, int], self.server_address)
        return "http://" + host + ":" + str(port) + "/v1"

    def start(self) -> str:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        gateway = cast(MockGateway, self.server)
        if self.path.rstrip("/") == "/health":
            self._send(200, {"ok": True})
            return
        if self.path.rstrip("/") == "/__capture":
            self._send(200, {"requests": gateway.records})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        gateway = cast(MockGateway, self.server)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        parsed: object = json.loads(raw.decode() or "{}")
        body = cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}
        headers = {name: self.headers.get(name) or "" for name in _HEADER_NAMES}
        headers["X-Sentica-Stream"] = self.headers.get("X-Sentica-Stream") or ""
        headers["X-Sentica-Embed-Op"] = self.headers.get("X-Sentica-Embed-Op") or ""
        record = {"path": self.path, "body": body, "headers": dict(headers)}
        gateway.records.append(record)
        if gateway.strict:
            reason = header_error(headers) or tool_shape_error(body)
            if reason is not None:
                self._send(400, {"error": reason})
                return
        payload = gateway.decide(self.path, body, headers)
        self._send(200, payload)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def serve(port: int) -> None:
    """Block. The runtime-role job points gateway mode at this process."""
    dimensions = int(os.environ.get("SENTICA_MOCK_EMBED_DIMENSIONS", "1536"))
    server = MockGateway(dimensions=dimensions, strict=True, port=port)
    server.serve_forever()


_MODELS = {
    "SENTICA_GATEWAY_DERIVER_MODEL": "sentica-deriver",
    "SENTICA_GATEWAY_SUMMARY_MODEL": "sentica-summary",
    "SENTICA_GATEWAY_DIALECTIC_MODEL": "sentica-dialectic",
    "SENTICA_GATEWAY_DREAM_MODEL": "sentica-dream",
    "SENTICA_GATEWAY_EMBED_MODEL": "sentica-embed",
}


def chat_message(
    content: str | None,
    tool_calls: list[tuple[str, str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """One OpenAI chat completion body."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish = "stop"
    if tool_calls:
        finish = "tool_calls"
        message["tool_calls"] = [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
            for call_id, name, arguments in tool_calls
        ]
    return {
        "choices": [{"finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }


def message_has_tool(body: dict[str, Any]) -> bool:
    """Whether the history already includes a tool result."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    for message in cast(list[object], messages):
        if isinstance(message, dict) and cast(dict[str, Any], message).get("role") == "tool":
            return True
    return False


def function_names(body: dict[str, Any]) -> list[str]:
    """OpenAI function names on a chat request."""
    tools = body.get("tools")
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for tool in cast(list[object], tools):
        if not isinstance(tool, dict):
            continue
        raw = cast(dict[str, Any], tool)
        function = raw.get("function")
        if isinstance(function, dict):
            name = cast(dict[str, Any], function).get("name")
        else:
            name = raw.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def forbid_provider_sdks(monkeypatch: Any) -> None:
    """Make every provider SDK constructor raise."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider SDK was constructed")

    import anthropic
    import openai
    from google import genai

    from src.llm import registry

    monkeypatch.setattr(openai, "AsyncOpenAI", _boom)
    monkeypatch.setattr(openai, "OpenAI", _boom)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _boom)
    monkeypatch.setattr(anthropic, "Anthropic", _boom)
    monkeypatch.setattr(genai, "Client", _boom)
    registry.get_openai_client.cache_clear()
    registry.get_anthropic_client.cache_clear()
    registry.get_gemini_client.cache_clear()
    registry.get_openai_override_client.cache_clear()
    registry.get_anthropic_override_client.cache_clear()
    registry.get_gemini_override_client.cache_clear()


def use_real_embeddings(monkeypatch: Any) -> None:
    """Drop the autouse embedding mock so gateway mode handles the calls."""
    from src.embedding_client import EmbeddingClient, embedding_client

    for name in (
        "embed",
        "simple_batch_embed",
        "batch_embed",
        "prepare_chunks",
        "truncate_to_token_limit",
    ):
        monkeypatch.setattr(
            embedding_client,
            name,
            getattr(EmbeddingClient, name).__get__(embedding_client),
        )


def apply_gateway_env(
    monkeypatch: Any, base_url: str, *, tenant: str = "tenant-a"
) -> None:
    """Point gateway mode at one server and name every purpose model."""
    monkeypatch.setenv("SENTICA_GATEWAY_MODE", "on")
    monkeypatch.setenv("SENTICA_GATEWAY_BASE_URL", base_url)
    monkeypatch.setenv("SENTICA_TENANT_ID", tenant)
    monkeypatch.setenv("SENTICA_GATEWAY_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("SENTICA_GATEWAY_RETRY_WAIT_SECONDS", "0")
    monkeypatch.delenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", raising=False)
    for name, model in _MODELS.items():
        monkeypatch.setenv(name, model)
