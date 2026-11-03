"""System monitoring API (Phase 10). Everything here needs ``system:read``.

Live values (host summary, processes, RAID from ``/proc/mdstat``) are read on
request because they are cheap and unprivileged; services and SMART come
from the snapshots the worker stores, so a page view never runs systemctl or
reaches the privileged helper.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, require_permission
from app.core.config import Settings
from app.db.dependencies import get_db
from app.models.monitoring import CHECK_KIND_SERVICE, MonitorCheck
from app.schemas.monitoring import (
    AlertCounts,
    AlertSummary,
    CheckOut,
    History,
    MonitoringOverview,
    ProcessInfo,
    RaidStatus,
    ServiceStatus,
    SyntheticAlert,
)
from app.services import remote_sync
from app.services.monitoring import alerts, checks, processes, samples
from app.services.monitoring.host import collect_system_summary

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


def check_out(row: MonitorCheck) -> CheckOut:
    return CheckOut(
        key=row.key,
        kind=row.kind,
        target=row.target,
        status=row.status,
        summary=row.summary,
        details=row.details,
        checked_at=row.checked_at,
    )


def alert_summary(db: Session, settings: Settings) -> AlertSummary:
    worker = remote_sync.worker_status(db, settings)
    synthetic: list[SyntheticAlert] = []
    if not worker["worker_online"]:
        # Nothing can notify about this one: the worker is what notifies.
        synthetic.append(
            SyntheticAlert(
                key="worker_offline",
                severity="critical",
                message=(
                    "The background worker is not running: metric samples, checks, "
                    "alert notifications and sync jobs are paused"
                ),
            )
        )
    latest = samples.latest_sample(db)
    return AlertSummary(
        counts=AlertCounts(**alerts.counts(db)),
        synthetic=synthetic,
        worker_online=bool(worker["worker_online"]),
        last_sample_at=latest.sampled_at if latest is not None else None,
    )


@router.get("/overview", response_model=MonitoringOverview)
def overview(
    request: Request,
    current: CurrentUser = Depends(require_permission("system:read")),
    db: Session = Depends(get_db),
) -> MonitoringOverview:
    settings: Settings = request.app.state.settings
    return MonitoringOverview(
        enabled=settings.monitor_enabled,
        summary=collect_system_summary(samples.sample_paths(settings)),
        checks=[check_out(row) for row in checks.list_checks(db)],
        alerts=alert_summary(db, settings),
    )


@router.get("/history", response_model=History)
def history(
    request: Request,
    range_name: Literal["1h", "6h", "24h", "7d"] = Query(default="1h", alias="range"),
    current: CurrentUser = Depends(require_permission("system:read")),
    db: Session = Depends(get_db),
) -> History:
    return samples.history(db, request.app.state.settings, range_name)


@router.get("/processes", response_model=list[ProcessInfo])
def list_processes(
    request: Request,
    sort: Literal["cpu", "memory"] = Query(default="cpu"),
    limit: int | None = Query(default=None, ge=1, le=500),
    current: CurrentUser = Depends(require_permission("system:read")),
) -> list[ProcessInfo]:
    settings: Settings = request.app.state.settings
    return processes.top_processes(sort=sort, limit=limit or settings.monitor_process_limit)


@router.get("/services", response_model=list[ServiceStatus])
def list_services(
    current: CurrentUser = Depends(require_permission("system:read")),
    db: Session = Depends(get_db),
) -> list[ServiceStatus]:
    return [
        ServiceStatus(
            unit=row.target,
            status=row.status,
            summary=row.summary,
            description=row.details.get("description"),
            load_state=row.details.get("load_state"),
            active_state=row.details.get("active_state"),
            sub_state=row.details.get("sub_state"),
            unit_file_state=row.details.get("unit_file_state"),
            active_since=row.details.get("active_since"),
            checked_at=row.checked_at,
        )
        for row in checks.list_checks(db, CHECK_KIND_SERVICE)
    ]


@router.get("/raid", response_model=RaidStatus)
def raid_status(
    current: CurrentUser = Depends(require_permission("system:read")),
) -> RaidStatus:
    return checks.read_raid()


@router.get("/checks", response_model=list[CheckOut])
def list_checks(
    kind: str | None = Query(default=None, max_length=16),
    current: CurrentUser = Depends(require_permission("system:read")),
    db: Session = Depends(get_db),
) -> list[CheckOut]:
    return [check_out(row) for row in checks.list_checks(db, kind)]
