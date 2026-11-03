"""System monitoring and alerting (Phase 10).

``MetricSample`` rows are the worker's periodic host samples (history for
the charts). ``MonitorCheck`` keeps the latest result of every non-metric
check (services, RAID arrays, SMART, helper) so pages read a snapshot
instead of shelling out per request.

``AlertRule`` rows decide when an observation becomes an ``Alert``. An alert
is ``pending`` until its rule's duration has elapsed, then ``firing`` until
the condition clears (``resolved``). Notifications go through an outbox
(``AlertNotification``) that the worker delivers to ``AlertChannel`` rows
(email or webhook); channel secrets are one Fernet token, like remote
connection credentials.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow_naive

CHECK_OK = "ok"
CHECK_WARNING = "warning"
CHECK_CRITICAL = "critical"
CHECK_UNKNOWN = "unknown"
CHECK_STATUSES = (CHECK_OK, CHECK_WARNING, CHECK_CRITICAL, CHECK_UNKNOWN)

CHECK_KIND_SERVICE = "service"
CHECK_KIND_RAID = "raid"
CHECK_KIND_SMART = "smart"
CHECK_KIND_HELPER = "helper"

# Threshold metrics compare a number against the rule's threshold; condition
# metrics are true/false per target (an array, a disk, a unit).
METRIC_CPU = "cpu_percent"
METRIC_LOAD = "load_per_core"
METRIC_MEMORY = "memory_percent"
METRIC_SWAP = "swap_percent"
METRIC_DISK = "disk_percent"
THRESHOLD_METRICS = (METRIC_CPU, METRIC_LOAD, METRIC_MEMORY, METRIC_SWAP, METRIC_DISK)
METRIC_RAID_DEGRADED = "raid_degraded"
METRIC_RAID_FAILED_MEMBER = "raid_failed_member"
METRIC_SMART_FAILED = "smart_failed"
METRIC_SERVICE_DOWN = "service_down"
METRIC_HELPER_UNAVAILABLE = "helper_unavailable"
CONDITION_METRICS = (
    METRIC_RAID_DEGRADED,
    METRIC_RAID_FAILED_MEMBER,
    METRIC_SMART_FAILED,
    METRIC_SERVICE_DOWN,
    METRIC_HELPER_UNAVAILABLE,
)
METRICS = THRESHOLD_METRICS + CONDITION_METRICS

COMPARISON_GT = "gt"
COMPARISON_LT = "lt"
COMPARISONS = (COMPARISON_GT, COMPARISON_LT)

SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SEVERITIES = (SEVERITY_WARNING, SEVERITY_CRITICAL)

ALERT_PENDING = "pending"
ALERT_FIRING = "firing"
ALERT_RESOLVED = "resolved"
ALERT_OPEN_STATES = (ALERT_PENDING, ALERT_FIRING)
ALERT_STATES = (ALERT_PENDING, ALERT_FIRING, ALERT_RESOLVED)

CHANNEL_EMAIL = "email"
CHANNEL_WEBHOOK = "webhook"
CHANNEL_KINDS = (CHANNEL_EMAIL, CHANNEL_WEBHOOK)

EVENT_FIRING = "firing"
EVENT_RESOLVED = "resolved"
EVENT_TEST = "test"

NOTIFY_QUEUED = "queued"
NOTIFY_SENT = "sent"
NOTIFY_FAILED = "failed"


def _load_json(value: str | None, default: Any) -> Any:
    try:
        loaded = json.loads(value) if value else default
    except ValueError:
        return default
    return loaded if isinstance(loaded, type(default)) else default


class MetricSample(Base):
    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False, default=utcnow_naive
    )
    cpu_percent: Mapped[float] = mapped_column(Float, nullable=False)
    load1: Mapped[float | None] = mapped_column(Float, nullable=True)
    load_per_core: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_percent: Mapped[float] = mapped_column(Float, nullable=False)
    swap_percent: Mapped[float] = mapped_column(Float, nullable=False)
    net_bytes_sent: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_bytes_recv: Mapped[int] = mapped_column(BigInteger, nullable=False)
    disks_json: Mapped[str] = mapped_column("disks", Text, nullable=False, default="[]")

    @property
    def disks(self) -> list[dict[str, Any]]:
        value: list[dict[str, Any]] = _load_json(self.disks_json, [])
        return value

    @disks.setter
    def disks(self, value: list[dict[str, Any]]) -> None:
        self.disks_json = json.dumps(value)


class MonitorCheck(Base):
    __tablename__ = "monitor_checks"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details_json: Mapped[str] = mapped_column("details", Text, nullable=False, default="{}")
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive
    )

    @property
    def details(self) -> dict[str, Any]:
        value: dict[str, Any] = _load_json(self.details_json, {})
        return value

    @details.setter
    def details(self, value: dict[str, Any]) -> None:
        self.details_json = json.dumps(value, sort_keys=True, default=str)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    # Disk path, unit name, array or device name; NULL means every target.
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comparison: Mapped[str] = mapped_column(
        String(4), nullable=False, default=COMPARISON_GT, server_default=COMPARISON_GT
    )
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    notify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
        server_default=func.now(),
    )


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True, nullable=False
    )
    target: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    state: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    firing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    acknowledged_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AlertChannel(Base):
    __tablename__ = "alert_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    config_json: Mapped[str] = mapped_column("config", Text, nullable=False, default="{}")
    secrets_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SEVERITY_WARNING, server_default=SEVERITY_WARNING
    )
    allow_insecure: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    send_resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
        server_default=func.now(),
    )

    @property
    def config(self) -> dict[str, Any]:
        value: dict[str, Any] = _load_json(self.config_json, {})
        return value

    @config.setter
    def config(self, value: dict[str, Any]) -> None:
        self.config_json = json.dumps(value, sort_keys=True)


class AlertNotification(Base):
    """Outbox row: one delivery of one alert event to one channel."""

    __tablename__ = "alert_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("alert_channels.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
