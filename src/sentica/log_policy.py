"""Production logs keep a location, or an allow-listed count.

Every record is written as the logger name, function, line, and level.
The formatted message is omitted. An allow list of exact logger and
format-string pairs from src.sentica may keep a line that carries ids
and counts only. Access logs are dropped. Exception records keep the
exception class and nothing else.
"""

from __future__ import annotations

import logging
import os

# Exact (logger name, format string) pairs. The format string is the
# record's msg before interpolation, so an f-string cannot match.
ALLOW_LIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("src.sentica.db_connect", "proxy tenant=%s connections=%s"),
        ("src.sentica.safety", "Dropped a conclusion category=%s"),
    }
)
_LOCATION = "%s %s %s %s"
_installed = False


class ProductionLogFilter(logging.Filter):
    """Drop message text unless the format string is allow-listed."""

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
        template = record.msg if isinstance(record.msg, str) else ""
        if (record.name, template) in ALLOW_LIST:
            return True
        record.args = (
            record.name,
            record.funcName,
            record.lineno,
            record.levelname,
        )
        record.msg = _LOCATION
        record.exc_text = None
        record.stack_info = None
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
