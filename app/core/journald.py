"""Native systemd-journald output (stdlib only, no python-systemd).

Records are sent as datagrams in the journal's native protocol to
``/run/systemd/journal/socket``: ``FIELD=value`` lines, or the binary-safe
``FIELD\\n<64-bit little-endian length><value>\\n`` form for values that
contain a newline. Besides MESSAGE and PRIORITY every record carries
SYSLOG_IDENTIFIER, LOGGER, REQUEST_ID and CODE_* fields, and each extra
field becomes an upper-case ``NAS_*`` field, so audit lines can be queried
with ``journalctl NAS_AUDIT_EVENT=auth.login.failure``.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import socket
import struct
from pathlib import Path
from typing import Any

_FIELD_BAD = re.compile(r"[^A-Z0-9_]")
_MAX_MESSAGE_BYTES = 32 * 1024
_SKIP = {"audit_details"}


def journal_stream_connected(socket_path: Path) -> bool:
    """True when systemd connected stdout/stderr to the journal (it sets
    JOURNAL_STREAM for services) and the native socket is present."""
    return bool(os.environ.get("JOURNAL_STREAM")) and socket_path.exists()


def priority(levelno: int) -> int:
    if levelno >= logging.CRITICAL:
        return 2
    if levelno >= logging.ERROR:
        return 3
    if levelno >= logging.WARNING:
        return 4
    if levelno >= logging.INFO:
        return 6
    return 7


def field_name(key: str) -> str | None:
    name = _FIELD_BAD.sub("_", key.upper()).lstrip("_")
    if not name:
        return None
    if not name.startswith("NAS_"):
        name = f"NAS_{name}"
    return name[:64]


def encode(fields: dict[str, Any]) -> bytes:
    parts: list[bytes] = []
    for key, value in fields.items():
        if value is None:
            continue
        text = (
            value
            if isinstance(value, str)
            else (json.dumps(value, default=str) if isinstance(value, dict | list) else str(value))
        )
        data = text.encode("utf-8", "replace")
        name = key.encode("ascii")
        if b"\n" in data:
            parts.append(name + b"\n" + struct.pack("<Q", len(data)) + data + b"\n")
        else:
            parts.append(name + b"=" + data + b"\n")
    return b"".join(parts)


class JournaldHandler(logging.Handler):
    def __init__(self, socket_path: Path, identifier: str, reserved: set[str]) -> None:
        super().__init__()
        self.socket_path = str(socket_path)
        self.identifier = identifier
        self.reserved = reserved
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    def fields(self, record: logging.LogRecord) -> dict[str, Any]:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
        fields: dict[str, Any] = {
            "MESSAGE": message,
            "PRIORITY": priority(record.levelno),
            "SYSLOG_IDENTIFIER": self.identifier,
            "LOGGER": record.name,
            "CODE_FILE": record.pathname,
            "CODE_LINE": record.lineno,
            "CODE_FUNC": record.funcName,
            "THREAD_NAME": record.threadName,
        }
        request_id = getattr(record, "request_id", None)
        if request_id and request_id != "-":
            fields["REQUEST_ID"] = request_id
        for key, value in record.__dict__.items():
            if key in self.reserved or key.startswith("_") or key in _SKIP:
                continue
            name = field_name(key)
            if name is not None and name not in fields:
                fields[name] = value
        return fields

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = self.fields(record)
            data = encode(fields)
            try:
                self._sock.sendto(data, self.socket_path)
            except OSError as exc:
                if exc.errno not in (errno.EMSGSIZE, errno.ENOBUFS):
                    raise
                fields["MESSAGE"] = (
                    str(fields["MESSAGE"]).encode()[:_MAX_MESSAGE_BYTES].decode("utf-8", "ignore")
                    + " [truncated]"
                )
                self._sock.sendto(encode(fields), self.socket_path)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            self._sock.close()
        finally:
            super().close()
