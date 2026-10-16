"""Real agents talk only to an OpenAI-shaped gateway."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from src.utils.summarizer import create_short_summary as real_short_summary
from tests.sentica.mock_gateway import (
    MockGateway,
    apply_gateway_env,
    chat_message,
    forbid_provider_sdks,
    function_names,
    message_has_tool,
    use_real_embeddings,
)


def _arguments(name: str) -> dict[str, Any]:
    if name in {"search_memory", "search_messages"}:
        return {"query": "local hello"}
    if name == "grep_messages":
        return {"text": "local hello"}
    if name.startswith("create_observations"):
        return {"observations": [{"content": "local hello"}]}
    return {}


def _decide(path: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    if path.rstrip("/").endswith("/embeddings"):
        incoming = body.get("input", [])
        texts = [incoming] if isinstance(incoming, str) else list(incoming)
        return {
            "data": [
                {"index": index, "embedding": [0.25] * 1536}
                for index in range(len(texts))
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 0},
        }
    response_format = body.get("response_format")
    formatted = (
        cast(dict[str, Any], response_format)
        if isinstance(response_format, dict)
        else {}
    )
    if formatted.get("type") == "json_schema":
        schema = formatted.get("json_schema")
        named = cast(dict[str, Any], schema) if isinstance(schema, dict) else {}
        if named.get("name") == "PromptRepresentation":
            return chat_message('{"explicit":[{"content":"local hello"}]}')
        return chat_message("{}")
    names = function_names(body)
    # Dialectic keeps calling tools until the iteration limit drops them.
    # The configured limit is 2 in CI and 1 by default, so stopping after
    # one result would skip the final call without tools.
    if names and headers.get("X-Sentica-Purpose") == "dialectic":
        return chat_message(None, [("c1", names[0], _arguments(names[0]))])
    if names and not message_has_tool(body):
        return chat_message(None, [("c1", names[0], _arguments(names[0]))])
    if names and message_has_tool(body):
        return chat_message("ok")
    return chat_message("ok")


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Any:
    server = MockGateway(_decide, dimensions=1536, strict=True)
    apply_gateway_env(monkeypatch, server.start())
    forbid_provider_sdks(monkeypatch)
    yield server
    server.close()


def _assert_headers(server: MockGateway) -> None:
    assert server.records
    for record in server.records:
        headers = cast(dict[str, str], record["headers"])
        assert headers["X-Sentica-Tenant"]
        assert headers["X-Sentica-Workspace"]
        assert headers["X-Sentica-Purpose"]
        assert headers["X-Sentica-Request-Id"]
        body = cast(dict[str, Any], record["body"])
        for name in function_names(body):
            assert name


async def _workspace(client: Any) -> str:
    from starlette.testclient import TestClient

    from tests.sentica.rls_job import authorize

    assert isinstance(client, TestClient)
    name = "gw" + __import__("uuid").uuid4().hex[:8]
    authorize(client, [name], primary=name)
    assert client.post("/v3/workspaces", json={"name": name}).status_code in (
        200,
        201,
    )
    assert client.post(
        f"/v3/workspaces/{name}/sessions", json={"id": "s1"}
    ).status_code in (200, 201)
    assert client.post(
        f"/v3/workspaces/{name}/peers", json={"name": "p1"}
    ).status_code in (200, 201)
    posted = client.post(
        f"/v3/workspaces/{name}/sessions/s1/messages",
        json={"messages": [{"content": "local hello", "peer_id": "p1"}]},
    )
    assert posted.status_code in (200, 201), posted.text
    return name


async def test_dialectic_agent_uses_openai_tools_and_a_final_call(
    client: Any, gateway: MockGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.dialectic.core import DialecticAgent
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    logged: list[dict[str, Any]] = []

    def _capture(_self: object, **kwargs: Any) -> None:
        logged.append(kwargs)

    monkeypatch.setattr(DialecticAgent, "_log_response_metrics", _capture)
    workspace = await _workspace(client)
    bound = set_workspace(workspace)
    try:
        agent = DialecticAgent(workspace, "s1", "p1", "p1", reasoning_level="minimal")
        answer = await agent.answer("what is known")
        stream_agent = DialecticAgent(
            workspace, "s1", "p1", "p1", reasoning_level="minimal"
        )
        chunks = [chunk async for chunk in stream_agent.answer_stream("what is known")]
    finally:
        reset_workspace(bound)
    assert answer
    assert "".join(chunks)
    _assert_headers(gateway)
    bodies = [
        cast(dict[str, Any], record["body"])
        for record in gateway.records
        if not str(record["path"]).endswith("/embeddings")
    ]
    assert any(function_names(body) for body in bodies)
    assert any("tools" not in body for body in bodies)
    assert any(
        "maximum number of tool calls" in json.dumps(body.get("messages"))
        for body in bodies
    )
    assert logged
    assert all(item["input_tokens"] > 0 for item in logged)
    assert all(item["iterations"] >= 2 for item in logged)
    assert all(item["tool_calls_count"] >= 1 for item in logged)


async def test_deriver_summary_dreams_and_specialists_stay_on_the_gateway(
    client: Any, db_session: Any, gateway: MockGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    import src.deriver.deriver as deriver_module
    import src.dreamer.specialists as specialists_module
    import src.utils.summarizer as summarizer_module
    from src import crud, models
    from src.dependencies import tracked_db
    from src.deriver.deriver import process_representation_tasks_batch
    from src.dreamer.specialists import DeductionSpecialist, InductionSpecialist
    from src.llm.api import honcho_llm_call as real_llm_call
    from src.llm.types import LLMTelemetryContext
    from src.sentica.dreams import run_dreams
    from src.sentica.workspace_scope import reset_workspace, set_workspace
    from src.utils.config_helpers import get_configuration

    assert isinstance(db_session, AsyncSession)
    workspace = await _workspace(client)
    monkeypatch.setattr(deriver_module, "honcho_llm_call", real_llm_call)
    monkeypatch.setattr(specialists_module, "honcho_llm_call", real_llm_call)
    monkeypatch.setattr(summarizer_module, "honcho_llm_call", real_llm_call)
    monkeypatch.setattr("src.utils.summarizer.create_short_summary", real_short_summary)
    bound = set_workspace(workspace)
    try:
        message = await db_session.scalar(
            select(models.Message).where(
                models.Message.workspace_name == workspace,
                models.Message.content == "local hello",
            )
        )
        assert message is not None
        db_session.add(
            models.Collection(observer="p1", observed="p1", workspace_name=workspace)
        )
        await db_session.commit()
        db_session.add(
            models.Document(
                content="local hello",
                observer="p1",
                observed="p1",
                workspace_name=workspace,
                session_name="s1",
            )
        )
        await db_session.commit()
        async with tracked_db("r1.summary.config", read_only=True) as db:
            stored_workspace = await crud.get_workspace(db, workspace_name=workspace)
            session = await crud.get_session(
                db, session_name="s1", workspace_name=workspace
            )
            configuration = get_configuration(None, session, stored_workspace)
        await process_representation_tasks_batch(
            [message],
            configuration,
            observers=["p1"],
            observed="p1",
            queue_item_message_ids=[message.id],
        )
        await DeductionSpecialist().run(
            workspace_name=workspace,
            observer="p1",
            observed="p1",
            session_name="s1",
        )
        await InductionSpecialist().run(
            workspace_name=workspace,
            observer="p1",
            observed="p1",
            session_name="s1",
        )
        summary = await real_short_summary(
            "local hello",
            10,
            None,
            telemetry=LLMTelemetryContext(
                workspace_name=workspace,
                call_purpose="summary.short",
                parent_category="summary",
            ),
        )
        from datetime import UTC, datetime

        dreams = await run_dreams(workspace, now=datetime(2026, 10, 15, tzinfo=UTC))
    finally:
        reset_workspace(bound)
    assert summary.content
    assert dreams is not None
    _assert_headers(gateway)
    purposes = {
        cast(dict[str, str], record["headers"])["X-Sentica-Purpose"]
        for record in gateway.records
    }
    assert {"deriver", "dream", "summary"} <= purposes
    deriver_bodies = [
        cast(dict[str, Any], record["body"])
        for record in gateway.records
        if cast(dict[str, str], record["headers"])["X-Sentica-Purpose"] == "deriver"
    ]
    response_format = cast(dict[str, Any], deriver_bodies[0]["response_format"])
    assert response_format["type"] == "json_schema"
    schema = cast(dict[str, Any], response_format["json_schema"])
    assert schema["name"] == "PromptRepresentation"
    assert isinstance(schema["schema"], dict)
    dream_blob = json.dumps(
        [
            record["body"]
            for record in gateway.records
            if cast(dict[str, str], record["headers"])["X-Sentica-Purpose"] == "dream"
        ]
    )
    assert "local hello" in dream_blob
    specialist_tools = [
        function_names(cast(dict[str, Any], record["body"]))
        for record in gateway.records
        if cast(dict[str, str], record["headers"])["X-Sentica-Purpose"] == "dream"
        and function_names(cast(dict[str, Any], record["body"]))
    ]
    assert any("create_observations_deductive" in names for names in specialist_tools)
    assert any("create_observations_inductive" in names for names in specialist_tools)


async def test_every_embedding_path_uses_the_gateway(
    gateway: MockGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.config import settings
    from src.embedding_client import EmbeddingClient, EmbeddingTokenLimitError
    from src.sentica.workspace_scope import reset_workspace, set_workspace

    use_real_embeddings(monkeypatch)

    def _cap(_self: object) -> int:
        return 4

    monkeypatch.setattr(EmbeddingClient, "_local_token_cap", _cap)
    text = "alpha beta gamma delta epsilon zeta eta theta"
    bound = set_workspace("left")
    try:
        client = EmbeddingClient()
        assert client.provider == settings.EMBEDDING.MODEL_CONFIG.transport
        assert client.model == "sentica-embed"
        assert client.vector_dimensions == settings.EMBEDDING.VECTOR_DIMENSIONS
        assert client.encoding.encode("ab")
        truncated = client.truncate_to_token_limit(text)
        assert len(client.encoding.encode(truncated)) <= 4
        chunks = client.prepare_chunks({"doc": text})
        assert len(chunks["doc"]) > 1
        with pytest.raises(EmbeddingTokenLimitError):
            await client.simple_batch_embed([text])
        vector = await client.embed("local hello")
        embedded = await client.batch_embed({"doc": text})
    finally:
        reset_workspace(bound)
    assert len(vector) == 1536
    assert len(embedded["doc"]) > 1
    embed_rows = [
        record
        for record in gateway.records
        if str(record["path"]).endswith("/embeddings")
    ]
    operations = {
        cast(dict[str, str], record["headers"])["X-Sentica-Embed-Op"]
        for record in embed_rows
    }
    assert {"embed", "batch_embed"} <= operations
    flat: list[object] = []
    for record in embed_rows:
        incoming = cast(dict[str, Any], record["body"]).get("input")
        if isinstance(incoming, list):
            flat.extend(cast(list[object], incoming))
    assert text not in flat
    _assert_headers(gateway)
