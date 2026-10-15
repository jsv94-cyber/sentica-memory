"""OpenAI-compatible embeddings for the local stack.

The vectors are deterministic and not all zeros. The process listens on
port 8080 and ignores the API key.
"""

from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def embedding_for(text: str, dimensions: int = 1536) -> list[float]:
    """A stable non-zero vector derived from the text."""
    if dimensions < 1:
        raise ValueError("dimensions must be positive")
    seed = hashlib.sha256(text.encode()).digest()
    values: list[float] = []
    while len(values) < dimensions:
        seed = hashlib.sha256(seed).digest()
        for index in range(0, len(seed), 4):
            number = int.from_bytes(seed[index : index + 4], "big")
            values.append((number / 2**32) * 2 - 1)
    vector = values[:dimensions]
    if vector[0] == 0.0:
        vector[0] = 0.5
    return vector


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in {"/v1/embeddings", "/embeddings"}:
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw.decode() or "{}")
        incoming = body.get("input", [])
        if isinstance(incoming, str):
            texts = [incoming]
        else:
            texts = [str(item) for item in incoming]
        dimensions = int(body.get("dimensions") or 1536)
        data = [
            {
                "object": "embedding",
                "index": index,
                "embedding": embedding_for(text, dimensions),
            }
            for index, text in enumerate(texts)
        ]
        self._send(
            200,
            {
                "object": "list",
                "data": data,
                "model": body.get("model", "fake"),
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _send(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8080), _Handler).serve_forever()


if __name__ == "__main__":
    main()
