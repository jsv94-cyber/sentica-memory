"""Tool errors handed back to the model stay inside the output budget."""

from __future__ import annotations

from src.config import settings
from src.utils.agent_tools import create_tool_executor


async def test_tool_error_text_is_capped_before_it_reaches_the_model() -> None:
    """A huge exception, such as a SQL statement, is not returned in full."""
    secret = "STATEMENT" + ("x" * (settings.LLM.MAX_TOOL_OUTPUT_CHARS + 5000))

    async def _boom(_ctx: object, _tool_input: dict[str, object]) -> str:
        raise RuntimeError(secret)

    executor = await create_tool_executor(
        "workspace",
        "observer",
        "observed",
        handler_resolver=lambda name: _boom if name == "search_memory" else None,
    )
    result = await executor("search_memory", {})
    assert isinstance(result, str)
    assert "truncated" in result
    assert secret not in result
    assert len(result) < settings.LLM.MAX_TOOL_OUTPUT_CHARS + 200
