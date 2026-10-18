"""R3 gateway startup, budget, and tokenizer checks."""

from __future__ import annotations

import pytest

from src.sentica.runtime import SenticaStartupError, load_instance
from src.sentica.tokenizer import encoding_for_gateway_model


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SENTICA_GATEWAY_RETRY_ATTEMPTS", "0"),
        ("SENTICA_GATEWAY_RETRY_ATTEMPTS", "nope"),
        ("SENTICA_GATEWAY_RETRY_WAIT_SECONDS", "-1"),
        ("SENTICA_GATEWAY_MODE", "maybe"),
        ("SENTICA_GATEWAY_TIMEOUT_SECONDS", "0"),
        ("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", "-5"),
        ("SENTICA_GATEWAY_BASE_URL", "ftp://example.com"),
        ("SENTICA_GATEWAY_REDIS_URL", "http://localhost"),
        ("SENTICA_GATEWAY_DERIVER_MODEL", " "),
        ("SENTICA_GATEWAY_TOKENIZER", "not-a-tokenizer"),
        ("SENTICA_GATEWAY_TOKENIZER_ALIASES", "sentica-embed"),
        ("SENTICA_GATEWAY_EMBED_TIMEOUT_SECONDS", "nan"),
    ],
)
def test_invalid_gateway_env_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("SENTICA_MODE", "test")
    assert load_instance() is None
    monkeypatch.setenv(name, value)
    with pytest.raises(SenticaStartupError, match=name):
        load_instance()


def test_unknown_tokenizer_names_the_rejected_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTICA_MODE", "test")
    assert load_instance() is None
    monkeypatch.setenv("SENTICA_GATEWAY_TOKENIZER", "not-a-tokenizer")
    with pytest.raises(SenticaStartupError, match="not-a-tokenizer"):
        load_instance()


def test_gateway_alias_uses_the_configured_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTICA_GATEWAY_TOKENIZER", "o200k_base")
    monkeypatch.setenv("SENTICA_GATEWAY_TOKENIZER_ALIASES", "sentica-embed=cl100k_base")
    alias = encoding_for_gateway_model("sentica-embed")
    fallback = encoding_for_gateway_model("not-a-real-model")
    assert alias.name == "cl100k_base"
    assert fallback.name == "o200k_base"


async def test_budget_reserve_and_charge_are_one_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.sentica.gateway import (
        _async_client,  # pyright: ignore[reportPrivateUsage]
        _charge,  # pyright: ignore[reportPrivateUsage]
        _reserve,  # pyright: ignore[reportPrivateUsage]
        _total_key,  # pyright: ignore[reportPrivateUsage]
        reset_token_counters,
    )
    from tests.sentica.resp_redis import RespRedis  # noqa: I001

    redis = RespRedis()
    try:
        monkeypatch.setenv("SENTICA_GATEWAY_REDIS_URL", redis.url)
        monkeypatch.setenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", "10")
        reset_token_counters()
        await _reserve("tenant-a", "deriver", 6)
        assert redis.store[_total_key("tenant-a")] == "6"
        await _charge("tenant-a", "deriver", 4)
        assert redis.store[_total_key("tenant-a")] == "10"
        assert redis.commands.count("EVAL") >= 2
        assert "INCRBY" not in redis.commands
        assert await _async_client() is await _async_client()
    finally:
        redis.close()


async def test_a_new_redis_url_closes_the_old_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client for the previous URL cannot open another connection."""
    from redis.exceptions import MaxConnectionsError

    from src.sentica.gateway import (
        _async_client,  # pyright: ignore[reportPrivateUsage]
        _drop_async_pool,  # pyright: ignore[reportPrivateUsage]
        reset_token_counters,
    )
    from tests.sentica.resp_redis import RespRedis  # noqa: I001

    first = RespRedis()
    second = RespRedis()
    try:
        monkeypatch.setenv("SENTICA_GATEWAY_REDIS_URL", first.url)
        reset_token_counters()
        old = await _async_client()
        assert old is not None
        assert await old.ping() is True
        monkeypatch.setenv("SENTICA_GATEWAY_REDIS_URL", second.url)
        new = await _async_client()
        assert new is not None
        assert new is not old
        with pytest.raises(MaxConnectionsError):
            await old.ping()
        assert await new.ping() is True
    finally:
        try:
            current = await _async_client()
            if current is not None:
                await current.aclose(close_connection_pool=True)
        finally:
            _drop_async_pool()
            first.close()
            second.close()


async def test_concurrent_holds_stay_inside_a_100_token_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten in-flight holds of 40 cannot spend a 100-token budget past 80."""
    import asyncio

    from src.exceptions import GatewayBudgetExceeded
    from src.sentica.gateway import (
        _reserve,  # pyright: ignore[reportPrivateUsage]
        _settle,  # pyright: ignore[reportPrivateUsage]
        _total_key,  # pyright: ignore[reportPrivateUsage]
        reset_token_counters,
    )
    from tests.sentica.resp_redis import RespRedis  # noqa: I001

    redis = RespRedis()
    tenant = "budget-hold"
    try:
        monkeypatch.setenv("SENTICA_GATEWAY_REDIS_URL", redis.url)
        monkeypatch.setenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", "100")
        reset_token_counters()

        async def _one() -> int:
            try:
                applied = await _reserve(tenant, "deriver", 40)
            except GatewayBudgetExceeded:
                return 0
            await _settle(tenant, "deriver", 40, applied)
            return applied

        admitted = await asyncio.gather(*(_one() for _ in range(10)))
        spent = sum(admitted)
        with redis.lock:
            stored = int(redis.store.get(_total_key(tenant), "0"))
        assert spent <= 100
        assert stored <= 100
        assert spent == 80
        assert stored == 80
    finally:
        reset_token_counters()
        redis.close()


async def test_concurrent_redis_holds_stay_inside_a_100_token_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten concurrent holds of 40 never pass a 100-token budget on Redis.

    ``SENTICA_BUDGET_TEST_REDIS_URL`` is the Redis the test-python job
    provides. A missing URL fails the test. The in-process stand-in above
    reimplements the reserve check, so this test is what fails when the
    script compares ``total > total_limit`` and skips adding the hold.
    """
    import asyncio
    import os
    import uuid

    from src.exceptions import GatewayBudgetExceeded
    from src.sentica.gateway import (
        _async_client,  # pyright: ignore[reportPrivateUsage]
        _drop_async_pool,  # pyright: ignore[reportPrivateUsage]
        _purpose_key,  # pyright: ignore[reportPrivateUsage]
        _reserve,  # pyright: ignore[reportPrivateUsage]
        _settle,  # pyright: ignore[reportPrivateUsage]
        _total_key,  # pyright: ignore[reportPrivateUsage]
    )

    url = os.environ.get("SENTICA_BUDGET_TEST_REDIS_URL", "").strip()
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true"
    assert url, "SENTICA_BUDGET_TEST_REDIS_URL is not set" + (" in CI" if in_ci else "")
    tenant = "budget-redis-" + uuid.uuid4().hex
    monkeypatch.setenv("SENTICA_GATEWAY_REDIS_URL", url)
    monkeypatch.setenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", "100")
    monkeypatch.delenv("SENTICA_GATEWAY_DERIVER_DAILY_TOKEN_BUDGET", raising=False)
    _drop_async_pool()

    async def _one() -> int:
        try:
            applied = await _reserve(tenant, "deriver", 40)
        except GatewayBudgetExceeded:
            return 0
        await _settle(tenant, "deriver", 40, applied)
        return applied

    try:
        admitted = await asyncio.gather(*(_one() for _ in range(10)))
        spent = sum(admitted)
        client = await _async_client()
        assert client is not None
        stored = int(await client.get(_total_key(tenant)) or "0")
        purpose = int(await client.get(_purpose_key(tenant, "deriver")) or "0")
        assert spent <= 100
        assert stored <= 100
        assert purpose <= 100
        assert spent == 80
        assert stored == 80
        assert purpose == 80
    finally:
        client = await _async_client()
        if client is not None:
            match = "sentica:budget:" + tenant + ":*"
            keys = [key async for key in client.scan_iter(match=match, count=50)]
            if keys:
                await client.delete(*keys)
            await client.aclose(close_connection_pool=True)
        _drop_async_pool()


def test_chat_hold_counts_tool_schemas_and_list_content() -> None:
    """A hold covers multipart text and the tool schemas sent with the call."""
    from typing import Any

    from src.sentica.gateway import _chat_hold  # pyright: ignore[reportPrivateUsage]
    from src.sentica.tokenizer import count_gateway_tokens

    model = "gpt-4o-mini"
    prose = "the customer prefers the window seat on every flight"
    description = "Look up a stored conclusion about the caller"
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": prose}]}
    ]
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": description,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    hold = _chat_hold(messages, 8, model, tools)
    pieces = count_gateway_tokens([prose, description, "search_memory"], model)
    assert hold >= pieces + 8
    plain = _chat_hold([{"role": "user", "content": prose}], 8, model, None)
    assert plain >= count_gateway_tokens([prose], model) + 8


async def test_a_failed_chat_releases_its_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure returns the reserved tokens to the budget."""
    from src.exceptions import GatewayUnavailable
    from src.sentica.gateway import (
        _chat,  # pyright: ignore[reportPrivateUsage]
        _total_key,  # pyright: ignore[reportPrivateUsage]
        reset_token_counters,
    )
    from tests.sentica.resp_redis import RespRedis  # noqa: I001

    redis = RespRedis()
    try:
        monkeypatch.setenv("SENTICA_GATEWAY_REDIS_URL", redis.url)
        monkeypatch.setenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", "1000")
        monkeypatch.setenv("SENTICA_GATEWAY_BASE_URL", "http://127.0.0.1:9/v1")
        monkeypatch.setenv("SENTICA_GATEWAY_DERIVER_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("SENTICA_TENANT_ID", "tenant-a")
        monkeypatch.setenv("SENTICA_GATEWAY_RETRY_ATTEMPTS", "1")
        monkeypatch.setenv("SENTICA_GATEWAY_TIMEOUT_SECONDS", "0.2")
        reset_token_counters()
        with pytest.raises(GatewayUnavailable):
            await _chat(
                purpose="deriver",
                workspace="left",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=16,
                tools=None,
                tool_choice=None,
                response_model=None,
                json_mode=False,
                telemetry=None,
                mark_stream=False,
            )
        assert redis.store.get(_total_key("tenant-a"), "0") == "0"
    finally:
        reset_token_counters()
        redis.close()
