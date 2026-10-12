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
_installed = False


class ProductionLogFilter(logging.Filter):
    """Drop content that must not reach a log line."""

    def filter(self, record: logging.LogRecord) -> bool:
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
        if (
            "[parameters:" in rendered
            or "Payload:" in rendered
            or _WORK_UNIT.search(rendered)
        ):
            record.msg = "log redacted"
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
        redactor.filter(record)
        return original(self, record)

    logging.Handler.handle = _handle
    _installed = True
