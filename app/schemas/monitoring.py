"""API contracts for system monitoring (Phase 10)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.disks import RaidArray
from app.schemas.system import SystemSummary


class ProcessInfo(BaseModel):
    pid: int
    name: str
    username: str | None
    status: str
    cpu_percent: float
    memory_percent: float
    memory_rss: int
    started_at: datetime | None


class ServiceStatus(BaseModel):
    unit: str
    status: str
    summary: str
    description: str | None = None
    load_state: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    unit_file_state: str | None = None
    active_since: str | None = None
    checked_at: datetime | None = None


class CheckOut(BaseModel):
    key: str
    kind: str
    target: str
    status: str
    summary: str
    details: dict[str, Any]
    checked_at: datetime


class RaidStatus(BaseModel):
    available: bool
    error: str | None = None
    arrays: list[RaidArray] = []


class HistoryPoint(BaseModel):
    at: datetime
    cpu_percent: float
    cpu_percent_max: float
    memory_percent: float
    swap_percent: float
    load_per_core: float | None
    net_sent_bps: float | None
    net_recv_bps: float | None
    disks: dict[str, float]


class History(BaseModel):
    range: str
    bucket_seconds: int
    sample_seconds: int
    points: list[HistoryPoint]


class AlertCounts(BaseModel):
    firing: int
    pending: int
    critical: int
    warning: int
    unacknowledged: int


class SyntheticAlert(BaseModel):
    """A condition reported by the web process itself (not stored)."""

    key: str
    severity: str
    message: str


class AlertSummary(BaseModel):
    counts: AlertCounts
    synthetic: list[SyntheticAlert]
    worker_online: bool
    last_sample_at: datetime | None


class MonitoringOverview(BaseModel):
    enabled: bool
    summary: SystemSummary
    checks: list[CheckOut]
    alerts: AlertSummary
