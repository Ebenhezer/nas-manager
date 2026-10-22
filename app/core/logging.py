"""Structured logging: JSON or text, request-id correlation, secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from app.core.config import Settings

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

LOG_FILE_NAME = "nas-manager.log"
TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "request_id",
    "color_message",  # uvicorn adds an ANSI-coloured duplicate of the message
}
_SENSITIVE = re.compile(r"pass(word)?|secret|token|authorization|api[_-]?key|credential", re.I)
_HANDLER_MARK = "_nas_manager_handler"
REDACTED = "***"


class ContextFilter(logging.Filter):
    """Attach the request id and redact sensitive extra fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        # An explicit request_id (e.g. from the access log) wins over the ambient context.
        record.request_id = record.__dict__.get("request_id") or request_id_var.get() or "-"
        for key in list(record.__dict__):
            if key not in _RESERVED and _SENSITIVE.search(key):
                setattr(record, key, REDACTED)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id and request_id != "-":
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    """Install application handlers on the root logger (idempotent)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()

    formatter: logging.Formatter = (
        JsonFormatter() if settings.log_format == "json" else logging.Formatter(TEXT_FORMAT)
    )
    handlers: list[logging.Handler] = []
    if settings.log_to_console:
        handlers.append(logging.StreamHandler(sys.stdout))
    if settings.log_to_file:
        log_dir = settings.resolved_log_dir
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        handlers.append(
            RotatingFileHandler(
                log_dir / LOG_FILE_NAME,
                maxBytes=settings.log_max_bytes,
                backupCount=settings.log_backup_count,
                encoding="utf-8",
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(ContextFilter())
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(settings.log_level)
