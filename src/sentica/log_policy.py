"""Production logs keep class names, counts, and timings.

Exception text, SQL parameters, queue payloads, and work-unit keys can
carry message text or peer and workspace names. In production those
records are replaced before they are formatted.
"""

from __future__ import annotations

import logging
import os
import re

_WORK_UNIT = re.compile(
    r"\b(?:representation|summary|dream|webhook|deletion|reconciler|"
    + r"scope_backfill|scope_removal):[^\s]+"
)
_DETAIL = re.compile(r"(?is)\bDETAIL\b\s*:.*")
_RESOURCE = re.compile(
    r"(?i)(?:/v3/\S+"
    + r"|\b(?:workspace|peer|session)(?:[_\s-]*(?:id|name))?\b\s*[:=]?\s*\S+)"
)
_installed = False


def _redact_text(rendered: str) -> str:
    """Remove names, paths, and server DETAIL text from one log line."""
    cleaned = _DETAIL.sub("DETAIL redacted", rendered)
    cleaned = _RESOURCE.sub("redacted", cleaned)
    if "[parameters:" in cleaned or "Payload:" in cleaned or _WORK_UNIT.search(cleaned):
        return "log redacted"
    return cleaned


class ProductionLogFilter(logging.Filter):
    """Drop content that must not reach a log line."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" or record.name.startswith("uvicorn.access"):
            return False
        if record.exc_info or record.exc_text:
            exc = record.exc_info[1] if record.exc_info else None
            record.msg = exc.__class__.__name__ if exc is not None else "Exception"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return True
        try:
            rendered = record.getMessage()
        except Exception:
            record.msg = "log redacted"
            record.args = ()
            return True
        cleaned = _redact_text(rendered)
        if cleaned != rendered:
            record.msg = cleaned
            record.args = ()
        return True


def install_production_log_filter() -> None:
    """Attach the filter once when this process is production.

    Logger filters do not see records that propagate from a child. Handler
    filters do, including handlers created after this process starts.
    """
    global _installed
    if _installed:
        return
    if os.environ.get("SENTICA_MODE", "").strip() != "production":
        return
    redactor = ProductionLogFilter()
    original = logging.Handler.handle

    def _handle(self: logging.Handler, record: logging.LogRecord) -> bool:
        if not redactor.filter(record):
            return True
        return original(self, record)

    logging.Handler.handle = _handle
    _installed = True
