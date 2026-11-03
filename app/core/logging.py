"""Structured logging: JSON or text, request-id correlation, secret redaction.

Outputs (Phase 13): console (stdout), a rotated file (by size or daily,
optionally gzip-compressed), native journald and the best-effort remote
syslog handler (configured from the database, see
``app.services.syslog.log_handler``).
"""

from __future__ import annotations

import contextvars
import gzip
import json
import logging
import os
import re
import shutil
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from typing import TYPE_CHECKING, Any

from app.core.config import Settings
from app.core.journald import JournaldHandler, journal_stream_connected

if TYPE_CHECKING:
    from app.services.syslog.log_handler import RemoteLogHandler

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

LOG_FILE_NAME = "nas-manager.log"
_QUIET_LOGGERS = ("paramiko", "botocore", "boto3", "s3transfer", "urllib3", "httpcore2", "httpx2")
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

_state: dict[str, Any] = {"journald": False, "remote": None}


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


def _gzip_namer(name: str) -> str:
    return f"{name}.gz"


def _gzip_rotator(source: str, dest: str) -> None:
    with open(source, "rb") as src, gzip.open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(source)


def _file_handler(settings: Settings) -> logging.Handler:
    log_dir = settings.resolved_log_dir
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = log_dir / settings.log_file_name
    handler: RotatingFileHandler | TimedRotatingFileHandler
    if settings.log_rotation == "daily":
        handler = TimedRotatingFileHandler(
            path,
            when="midnight",
            utc=True,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
    else:
        handler = RotatingFileHandler(
            path,
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
    if settings.log_compress:
        handler.namer = _gzip_namer
        handler.rotator = _gzip_rotator
    return handler


def journald_enabled(settings: Settings) -> bool:
    if settings.log_journald == "off":
        return False
    if settings.log_journald == "on":
        return True
    return journal_stream_connected(settings.log_journald_socket)


def remote_log_handler() -> RemoteLogHandler | None:
    """This process's remote syslog handler (None before configure_logging)."""
    handler: RemoteLogHandler | None = _state["remote"]
    return handler


def logging_status(settings: Settings) -> dict[str, Any]:
    remote = remote_log_handler()
    return {
        "level": settings.log_level,
        "format": settings.log_format,
        "console": settings.log_to_console and not _state["journald"],
        "file": str(settings.resolved_log_dir / settings.log_file_name)
        if settings.log_to_file
        else None,
        "rotation": settings.log_rotation,
        "max_bytes": settings.log_max_bytes,
        "backup_count": settings.log_backup_count,
        "compress": settings.log_compress,
        "journald": bool(_state["journald"]),
        "syslog_identifier": settings.syslog_identifier,
        "remote_targets": len(remote.targets) if remote is not None else 0,
        "remote_dropped": remote.dropped if remote is not None else 0,
        "remote_send_errors": remote.send_errors if remote is not None else 0,
    }


def configure_logging(settings: Settings) -> None:
    """Install application handlers on the root logger (idempotent)."""
    from app.services.syslog.log_handler import make_handler

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()

    formatter: logging.Formatter = (
        JsonFormatter() if settings.log_format == "json" else logging.Formatter(TEXT_FORMAT)
    )
    use_journald = journald_enabled(settings)
    handlers: list[logging.Handler] = []
    # Under systemd the journal already captures stdout; with the native
    # handler active the console copy would only duplicate every line.
    if settings.log_to_console and not use_journald:
        handlers.append(logging.StreamHandler(sys.stdout))
    if settings.log_to_file:
        handlers.append(_file_handler(settings))
    for handler in handlers:
        handler.setFormatter(formatter)
    if use_journald:
        handlers.append(
            JournaldHandler(
                settings.log_journald_socket, settings.syslog_identifier, reserved=_RESERVED
            )
        )
    remote = make_handler(settings)
    remote.setFormatter(formatter)
    handlers.append(remote)
    for handler in handlers:
        handler.addFilter(ContextFilter())
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    _state["journald"] = use_journald
    _state["remote"] = remote
    root.setLevel(settings.log_level)
    # Remote-sync client libraries (Phase 7) log every SSH handshake at INFO
    # and request headers at DEBUG; keep them at WARNING whatever the app level.
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
