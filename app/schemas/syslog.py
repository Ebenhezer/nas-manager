"""API contracts for remote syslog destinations (Phase 13)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Protocol = Literal["udp", "tcp", "tls"]
SyslogFormat = Literal["rfc5424", "rfc3164"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class SyslogDestinationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    host: str = Field(min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: Protocol = "tls"
    ca_pem: str | None = Field(default=None, max_length=65536)
    format: SyslogFormat = "rfc5424"
    facility: str = "local0"
    forward_audit: bool = True
    audit_categories: list[str] = Field(default_factory=list, max_length=64)
    audit_failures_only: bool = False
    forward_logs: bool = False
    log_min_level: LogLevel = "WARNING"
    # Send the existing audit log too (default: start at the current end).
    backfill: bool = False
    confirm: bool = False


class SyslogDestinationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: Protocol | None = None
    ca_pem: str | None = Field(default=None, max_length=65536)
    format: SyslogFormat | None = None
    facility: str | None = None
    forward_audit: bool | None = None
    audit_categories: list[str] | None = Field(default=None, max_length=64)
    audit_failures_only: bool | None = None
    forward_logs: bool | None = None
    log_min_level: LogLevel | None = None
    confirm: bool = False


class SyslogDestinationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    enabled: bool
    host: str
    port: int
    protocol: str
    has_ca_pem: bool
    format: str
    facility: str
    forward_audit: bool
    audit_categories: list[str]
    audit_failures_only: bool
    forward_logs: bool
    log_min_level: str
    last_audit_id: int
    lag: int
    failing: bool
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int
    next_attempt_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SyslogTestResult(BaseModel):
    ok: bool
    message: str


class SyslogStatus(BaseModel):
    destinations: list[SyslogDestinationOut]
    logging: dict[str, Any]
    worker_online: bool
