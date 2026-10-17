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
        reset_token_counters,
    )
    from tests.sentica.resp_redis import RespRedis  # noqa: I001

    redis = RespRedis()
    try:
        monkeypatch.setenv("SENTICA_GATEWAY_REDIS_URL", redis.url)
        monkeypatch.setenv("SENTICA_GATEWAY_DAILY_TOKEN_BUDGET", "10")
        reset_token_counters()
        await _reserve("tenant-a", "deriver")
        await _charge("tenant-a", "deriver", 4)
        assert redis.commands.count("EVAL") >= 2
        assert "INCRBY" not in redis.commands
        assert _async_client() is _async_client()
    finally:
        redis.close()
