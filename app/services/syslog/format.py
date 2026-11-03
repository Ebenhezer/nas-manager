"""Syslog message rendering: RFC 5424 (default) and RFC 3164 (legacy).

Pure functions, no I/O. Header fields are restricted to printable ASCII as
the RFCs require; structured-data values are escaped (``"``, ``\\``, ``]``).
Audit events carry their fields as RFC 5424 structured data under the SD-ID
``nas@32473`` (32473 is the private enterprise number IANA reserves for
documentation, RFC 5612) and the full event as JSON in the message body.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.models import AuditEvent
from app.models.syslog_destination import FACILITIES, FORMAT_RFC3164

SD_ID = "nas@32473"

SEVERITY_CRITICAL = 2
SEVERITY_ERROR = 3
SEVERITY_WARNING = 4
SEVERITY_NOTICE = 5
SEVERITY_INFO = 6
SEVERITY_DEBUG = 7

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_NOT_PRINTABLE = re.compile(r"[^\x21-\x7e]")
_SD_NAME_BAD = re.compile(r'[^\x21-\x7e]|[= \]"]')


@dataclass(frozen=True)
class SyslogMessage:
    severity: int
    timestamp: datetime
    msgid: str
    text: str
    structured: dict[str, Any] = field(default_factory=dict)


def level_severity(levelno: int) -> int:
    if levelno >= logging.CRITICAL:
        return SEVERITY_CRITICAL
    if levelno >= logging.ERROR:
        return SEVERITY_ERROR
    if levelno >= logging.WARNING:
        return SEVERITY_WARNING
    if levelno >= logging.INFO:
        return SEVERITY_INFO
    return SEVERITY_DEBUG


def pri(facility: str, severity: int) -> int:
    return FACILITIES[facility] * 8 + severity


def header_field(value: str | None, max_length: int) -> str:
    """A printable-ASCII header field, ``-`` (NILVALUE) when empty."""
    if not value:
        return "-"
    cleaned = _NOT_PRINTABLE.sub("_", value)[:max_length]
    return cleaned or "-"


def _sd_value(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def structured_data(params: dict[str, Any]) -> str:
    parts = [
        f'{_SD_NAME_BAD.sub("_", name)[:32]}="{_sd_value(value)}"'
        for name, value in params.items()
        if value is not None and value != ""
    ]
    if not parts:
        return "-"
    return f"[{SD_ID} {' '.join(parts)}]"


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def render_rfc5424(
    message: SyslogMessage, *, facility: str, hostname: str, app_name: str, procid: str
) -> bytes:
    timestamp = _utc(message.timestamp).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    header = " ".join(
        (
            f"<{pri(facility, message.severity)}>1",
            timestamp,
            header_field(hostname, 255),
            header_field(app_name, 48),
            header_field(procid, 128),
            header_field(message.msgid, 32),
            structured_data(message.structured),
        )
    )
    return f"{header} {message.text}".encode()


def render_rfc3164(
    message: SyslogMessage, *, facility: str, hostname: str, app_name: str, procid: str
) -> bytes:
    stamp = _utc(message.timestamp)
    timestamp = f"{_MONTHS[stamp.month - 1]} {stamp.day:>2} {stamp:%H:%M:%S}"
    tag = header_field(app_name, 32)
    text = " ".join(message.text.splitlines())
    return (
        f"<{pri(facility, message.severity)}>{timestamp} {header_field(hostname, 255)} "
        f"{tag}[{header_field(procid, 16)}]: {text}"
    ).encode()


def render(
    message: SyslogMessage,
    *,
    fmt: str,
    facility: str,
    hostname: str,
    app_name: str,
    procid: str,
) -> bytes:
    renderer = render_rfc3164 if fmt == FORMAT_RFC3164 else render_rfc5424
    return renderer(message, facility=facility, hostname=hostname, app_name=app_name, procid=procid)


def audit_event_dict(row: AuditEvent) -> dict[str, Any]:
    created = row.created_at
    return {
        "id": row.id,
        "created_at": _utc(created).isoformat().replace("+00:00", "Z"),
        "event": row.event,
        "category": row.category,
        "outcome": row.outcome,
        "actor": row.actor_username or None,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "ip_address": row.ip_address,
        "user_agent": row.user_agent,
        "request_id": row.request_id,
        "details": row.details,
        "chain_hash": row.chain_hash,
    }


def audit_message(row: AuditEvent) -> SyslogMessage:
    target = f"{row.target_type}:{row.target_id}" if row.target_type else None
    return SyslogMessage(
        severity=SEVERITY_NOTICE if row.outcome == "success" else SEVERITY_WARNING,
        timestamp=row.created_at,
        msgid=row.category,
        text=json.dumps(audit_event_dict(row), sort_keys=True, ensure_ascii=False, default=str),
        structured={
            "id": str(row.id),
            "event": row.event,
            "category": row.category,
            "outcome": row.outcome,
            "actor": row.actor_username or None,
            "target": target,
            "ip": row.ip_address,
            "request_id": row.request_id,
        },
    )


def log_message(
    *, levelno: int, logger_name: str, created: float, text: str, request_id: str | None
) -> SyslogMessage:
    return SyslogMessage(
        severity=level_severity(levelno),
        timestamp=datetime.fromtimestamp(created, tz=UTC),
        msgid="log",
        text=text,
        structured={
            "logger": logger_name,
            "level": logging.getLevelName(levelno),
            "request_id": request_id if request_id and request_id != "-" else None,
        },
    )


def probe_message(name: str) -> SyslogMessage:
    return SyslogMessage(
        severity=SEVERITY_NOTICE,
        timestamp=datetime.now(UTC),
        msgid="test",
        text=f"NAS Manager syslog test message for destination {name!r}",
        structured={"test": "1"},
    )
