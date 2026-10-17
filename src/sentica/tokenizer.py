"""Local tokenizer for gateway model names that tiktoken does not know."""

from __future__ import annotations

import os

import tiktoken

_DEFAULT_ENCODING = "cl100k_base"


def tokenizer_aliases() -> dict[str, str]:
    """Parse ``SENTICA_GATEWAY_TOKENIZER_ALIASES``.

    The value is comma-separated ``alias=encoding`` pairs. A blank value is
    an empty map. A missing equals sign or an unknown encoding is refused by
    :func:`validate_tokenizer_env`.
    """
    raw = os.environ.get("SENTICA_GATEWAY_TOKENIZER_ALIASES", "")
    found: dict[str, str] = {}
    if not raw.strip():
        return found
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("SENTICA_GATEWAY_TOKENIZER_ALIASES is missing '='")
        alias, encoding = item.split("=", 1)
        alias = alias.strip()
        encoding = encoding.strip()
        if not alias or not encoding:
            raise ValueError("SENTICA_GATEWAY_TOKENIZER_ALIASES has an empty pair")
        found[alias] = encoding
    return found


def fallback_encoding_name() -> str:
    """Encoding used when the model is not a known tiktoken model."""
    raw = os.environ.get("SENTICA_GATEWAY_TOKENIZER", "").strip()
    return raw or _DEFAULT_ENCODING


def encoding_for_gateway_model(model: str) -> tiktoken.Encoding:
    """Resolve a gateway model alias, then a known model, then the fallback."""
    aliases = tokenizer_aliases()
    if model in aliases:
        return tiktoken.get_encoding(aliases[model])
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding(fallback_encoding_name())


def count_gateway_tokens(texts: list[str], model: str) -> int:
    """Count tokens with the encoding configured for ``model``."""
    encoding = encoding_for_gateway_model(model)
    total = 0
    for text in texts:
        if text:
            total += len(encoding.encode(text))
    return total


def validate_tokenizer_env() -> None:
    """Refuse a tokenizer name or alias encoding tiktoken cannot load."""
    configured = os.environ.get("SENTICA_GATEWAY_TOKENIZER")
    if configured is not None and configured.strip():
        try:
            tiktoken.get_encoding(configured.strip())
        except (KeyError, ValueError) as exc:
            raise ValueError(
                "SENTICA_GATEWAY_TOKENIZER is not a tiktoken encoding"
            ) from exc
    try:
        aliases = tokenizer_aliases()
    except ValueError:
        raise
    for encoding in aliases.values():
        try:
            tiktoken.get_encoding(encoding)
        except (KeyError, ValueError) as exc:
            raise ValueError(
                "SENTICA_GATEWAY_TOKENIZER_ALIASES names an unknown encoding"
            ) from exc
