"""Server-sent event framing for streamed dialectic answers.

Shared by the peer and workspace chat routes so the two stay in step; the
frames they emit are part of the public API and the SDKs parse them.
"""

import json
from collections.abc import AsyncIterator

from src.schemas.api import Evidence
from src.utils.evidence import EvidenceAccumulator


async def format_dialectic_sse_stream(
    chunks: AsyncIterator[str],
    evidence: EvidenceAccumulator | None = None,
    workspace_id: str | None = None,
) -> AsyncIterator[str]:
    """Frame answer chunks as SSE events, then a terminal event.

    Evidence can only be known once the answer has finished streaming, so it
    rides on the terminal event rather than a frame of its own. `evidence` is
    the accumulator the agent filled in while answering; None means the caller
    did not ask for evidence and the terminal event carries only `done`.
    """
    async for chunk in chunks:
        frame: dict[str, object] = {"delta": {"content": chunk}, "done": False}
        if workspace_id is not None:
            frame["workspace_id"] = workspace_id
        yield f"data: {json.dumps(frame)}\n\n"

    final: dict[str, object] = {"done": True}
    if workspace_id is not None:
        final["workspace_id"] = workspace_id
    if evidence is not None:
        final["evidence"] = _serializable(evidence.build())
    yield f"data: {json.dumps(final)}\n\n"


def _serializable(evidence: Evidence) -> object:
    """Round-trip through Pydantic's JSON encoder for the datetime fields."""
    return json.loads(evidence.model_dump_json())
