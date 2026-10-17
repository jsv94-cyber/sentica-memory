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
    from collections.abc import Callable
    from typing import Any, cast

    import redis.asyncio as aioredis

    from src.exceptions import GatewayBudgetExceeded
    from src.sentica.gateway import (
        _reserve,  # pyright: ignore[reportPrivateUsage]
        _settle,  # pyright: ignore[reportPrivateUsage]
        _total_key,  # pyright: ignore[reportPrivateUsage]
        reset_token_counters,
    )

    url = "redis://127.0.0.1:6379/15"
    connect = cast(Callable[..., Any], aioredis.Redis.from_url)
    client = connect(url, decode_responses=True, protocol=2)
    tenant = "budget-hold"
    try:
        await client.ping()
        await client.flushdb()
        monkeypatch.setenv("SENTICA_GATEWAY_REDIS_URL", url)
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
        stored = int(await client.get(_total_key(tenant)) or "0")
        assert spent <= 100
        assert stored <= 100
        assert spent == 80
        assert stored == 80
    finally:
        await client.aclose()
        reset_token_counters()
