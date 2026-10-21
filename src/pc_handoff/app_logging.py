"""Structured application logging: JSON lines in ``<dataDir>/logs/app.log`` with a traceId.

Each line is one JSON object with ``timestamp`` (ISO 8601 with offset), ``level``,
``traceId`` (null outside a trace scope) and ``message``, plus ``detail`` when the caller
passes ``extra={"detail": ...}`` or logs an exception. This log is separate from the
``activity_log.json`` audit file (requirement section 8, ADR-002).
"""

import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

from pc_handoff.clock import toIso

LOGGER_NAME = "pc_handoff"
LOGS_DIR_NAME = "logs"
APP_LOG_FILE_NAME = "app.log"

traceIdVar: ContextVar[str | None] = ContextVar("traceId", default=None)


def newTraceId() -> str:
    """Return a fresh traceId (32 lowercase hex characters)."""
    return uuid.uuid4().hex


@contextmanager
def traceScope(traceId: str | None) -> Iterator[str | None]:
    """Set ``traceId`` for every log line emitted in this context, then restore the previous one."""
    token = traceIdVar.set(traceId)
    try:
        yield traceId
    finally:
        traceIdVar.reset(token)


class JsonLineFormatter(logging.Formatter):
    """Format each record as exactly one JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTimestamp(record),
            "level": record.levelname,
            "traceId": getattr(record, "traceId", None) or traceIdVar.get(),
            "message": self.safeMessage(record),
        }
        detail = self.buildDetail(record)
        if detail is not None:
            payload["detail"] = detail
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            # e.g. a circular structure or non-string dict keys in ``detail``.
            payload["detail"] = repr(detail)
            return json.dumps(payload, ensure_ascii=False, default=repr)

    def formatTimestamp(self, record: logging.LogRecord) -> str:
        return toIso(datetime.fromtimestamp(record.created).astimezone())

    def safeMessage(self, record: logging.LogRecord) -> str:
        try:
            return record.getMessage()
        except Exception:  # a bad format string must never break logging
            return repr(record.msg)

    def buildDetail(self, record: logging.LogRecord) -> Any:
        detail = getattr(record, "detail", None)
        if record.exc_info:
            excText = self.formatException(record.exc_info)
            detail = excText if detail is None else f"{detail}\n{excText}"
        return detail


class AppLogFileHandler(logging.FileHandler):
    """UTF-8 append-mode file handler used for ``app.log`` (marker type for idempotency)."""


def configureAppLogging(dataDir: str | Path) -> logging.Logger:
    """Attach the JSON-lines ``app.log`` handler for ``dataDir`` to the ``pc_handoff`` logger.

    Creates ``<dataDir>/logs/``. Idempotent: calling it again for the same data directory adds
    no handler; calling it for a different directory replaces the previous ``app.log`` handler.
    """
    logsDir = Path(dataDir) / LOGS_DIR_NAME
    logsDir.mkdir(parents=True, exist_ok=True)
    logPath = (logsDir / APP_LOG_FILE_NAME).resolve()

    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if not isinstance(handler, AppLogFileHandler):
            continue
        if Path(handler.baseFilename) == logPath:
            return logger
        logger.removeHandler(handler)
        handler.close()

    handler = AppLogFileHandler(logPath, mode="a", encoding="utf-8")
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
    return logger
