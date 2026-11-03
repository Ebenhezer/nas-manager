"""API contracts for alert rules, alerts and notification channels (Phase 10).

Channel secrets (SMTP password, webhook signing secret, extra headers) are
accepted on input only; responses list which secret fields are set.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AlertOut(BaseModel):
    id: int
    rule_id: int
    rule_name: str
    metric: str
    target: str
    state: str
    severity: str
    message: str
    value: float | None
    started_at: datetime
    firing_at: datetime | None
    last_seen_at: datetime
    resolved_at: datetime | None
    acknowledged_at: datetime | None
    acknowledged_by: str | None


class AlertPage(BaseModel):
    items: list[AlertOut]
    total: int
    limit: int
    offset: int


class AlertRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    metric: str = Field(min_length=1, max_length=32)
    target: str | None = Field(default=None, max_length=255)
    comparison: str = Field(default="gt", max_length=4)
    threshold: float | None = None
    duration_seconds: int = Field(default=0, ge=0, le=7 * 86400)
    severity: str = Field(default="warning", max_length=16)
    enabled: bool = True
    notify: bool = True


class AlertRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    # Send "" to clear the target (every target).
    target: str | None = Field(default=None, max_length=255)
    comparison: str | None = Field(default=None, max_length=4)
    threshold: float | None = None
    duration_seconds: int | None = Field(default=None, ge=0, le=7 * 86400)
    severity: str | None = Field(default=None, max_length=16)
    enabled: bool | None = None
    notify: bool | None = None


class AlertRuleOut(BaseModel):
    id: int
    name: str
    metric: str
    target: str | None
    comparison: str
    threshold: float | None
    duration_seconds: int
    severity: str
    enabled: bool
    notify: bool
    builtin: bool
    open_alerts: int
    created_at: datetime
    updated_at: datetime


class AlertChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=16)
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str | None] = Field(default_factory=dict)
    min_severity: str = Field(default="warning", max_length=16)
    allow_insecure: bool = False
    send_resolved: bool = True
    enabled: bool = True


class AlertChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    # Keys merge over the stored config.
    config: dict[str, Any] | None = None
    # Missing key: keep; null or "": clear; string: replace.
    secrets: dict[str, str | None] | None = None
    min_severity: str | None = Field(default=None, max_length=16)
    allow_insecure: bool | None = None
    send_resolved: bool | None = None
    enabled: bool | None = None


class AlertChannelOut(BaseModel):
    id: int
    name: str
    kind: str
    config: dict[str, Any]
    secrets_set: list[str]
    credentials_readable: bool
    min_severity: str
    allow_insecure: bool
    send_resolved: bool
    enabled: bool
    last_test_at: datetime | None
    last_test_ok: bool | None
    last_test_message: str | None
    created_at: datetime
    updated_at: datetime


class ChannelTestResult(BaseModel):
    ok: bool
    message: str
