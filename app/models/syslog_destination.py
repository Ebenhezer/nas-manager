"""Remote syslog destinations (Phase 13).

Each row is one collector that receives audit events (reliably, by the
worker, from ``last_audit_id`` onwards) and optionally application log
records (best effort, from every process). There are no secrets: TLS
destinations authenticate the server with the system trust store or an
optional CA certificate in ``ca_pem``.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow_naive

PROTOCOL_UDP = "udp"
PROTOCOL_TCP = "tcp"
PROTOCOL_TLS = "tls"
PROTOCOLS = (PROTOCOL_UDP, PROTOCOL_TCP, PROTOCOL_TLS)

FORMAT_RFC5424 = "rfc5424"
FORMAT_RFC3164 = "rfc3164"
FORMATS = (FORMAT_RFC5424, FORMAT_RFC3164)

# RFC 5424 facility codes for the facilities an administrator may pick.
FACILITIES: dict[str, int] = {
    "user": 1,
    "daemon": 3,
    "auth": 4,
    "authpriv": 10,
    "local0": 16,
    "local1": 17,
    "local2": 18,
    "local3": 19,
    "local4": 20,
    "local5": 21,
    "local6": 22,
    "local7": 23,
}

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class SyslogDestination(Base):
    __tablename__ = "syslog_destinations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol: Mapped[str] = mapped_column(String(8), nullable=False)
    ca_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    format: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FORMAT_RFC5424, server_default=FORMAT_RFC5424
    )
    facility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="local0", server_default="local0"
    )
    forward_audit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    audit_categories_json: Mapped[str] = mapped_column(
        "audit_categories", Text, nullable=False, default="[]", server_default="[]"
    )
    audit_failures_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    forward_logs: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    log_min_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default="WARNING", server_default="WARNING"
    )
    # Forwarding state (written by the worker).
    last_audit_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    # Configuration changes only; the worker's cursor updates leave it alone.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )

    @property
    def audit_categories(self) -> list[str]:
        try:
            value = json.loads(self.audit_categories_json or "[]")
        except ValueError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []

    @audit_categories.setter
    def audit_categories(self, value: list[str]) -> None:
        self.audit_categories_json = json.dumps(sorted(set(value)))
