"""Remote syslog destinations and logging status (Phase 13).

Reading needs ``system:read``; creating, changing, deleting and testing
destinations needs ``system:write``. Destinations carry no secrets, so API
tokens with the scope may manage them. After every change this process
reloads its application-log forwarding; the worker notices the new
configuration version on its next tick.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, request_audit_context, require_permission
from app.core.logging import logging_status
from app.db.dependencies import get_db
from app.models import SyslogDestination
from app.schemas.syslog import (
    SyslogDestinationCreate,
    SyslogDestinationOut,
    SyslogDestinationUpdate,
    SyslogStatus,
    SyslogTestResult,
)
from app.services import remote_sync
from app.services.syslog import destinations
from app.services.syslog.forwarder import destination_status
from app.services.syslog.log_handler import reload_log_forwarding

router = APIRouter(prefix="/syslog", tags=["syslog"])


def _out(db: Session, row: SyslogDestination) -> SyslogDestinationOut:
    return SyslogDestinationOut(
        id=row.id,
        name=row.name,
        enabled=row.enabled,
        host=row.host,
        port=row.port,
        protocol=row.protocol,
        has_ca_pem=bool(row.ca_pem),
        format=row.format,
        facility=row.facility,
        forward_audit=row.forward_audit,
        audit_categories=row.audit_categories,
        audit_failures_only=row.audit_failures_only,
        forward_logs=row.forward_logs,
        log_min_level=row.log_min_level,
        last_audit_id=row.last_audit_id,
        last_success_at=row.last_success_at,
        last_error=row.last_error,
        last_error_at=row.last_error_at,
        consecutive_failures=row.consecutive_failures,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        **destination_status(db, row),
    )


@router.get("/destinations", response_model=list[SyslogDestinationOut])
def list_destinations(
    current: CurrentUser = Depends(require_permission("system:read")),
    db: Session = Depends(get_db),
) -> list[SyslogDestinationOut]:
    return [_out(db, row) for row in destinations.list_destinations(db)]


@router.post("/destinations", response_model=SyslogDestinationOut, status_code=201)
def create_destination(
    payload: SyslogDestinationCreate,
    request: Request,
    current: CurrentUser = Depends(require_permission("system:write")),
    db: Session = Depends(get_db),
) -> SyslogDestinationOut:
    row = destinations.create_destination(
        db, values=payload.model_dump(), actor=current.user, context=request_audit_context(request)
    )
    reload_log_forwarding(db)
    return _out(db, row)


@router.patch("/destinations/{destination_id}", response_model=SyslogDestinationOut)
def update_destination(
    destination_id: int,
    payload: SyslogDestinationUpdate,
    request: Request,
    current: CurrentUser = Depends(require_permission("system:write")),
    db: Session = Depends(get_db),
) -> SyslogDestinationOut:
    row = destinations.update_destination(
        db,
        destinations.get_destination_or_404(db, destination_id),
        changes=payload.model_dump(exclude_unset=True),
        actor=current.user,
        context=request_audit_context(request),
    )
    reload_log_forwarding(db)
    return _out(db, row)


@router.delete("/destinations/{destination_id}", status_code=204)
def delete_destination(
    destination_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("system:write")),
    db: Session = Depends(get_db),
) -> None:
    destinations.delete_destination(
        db,
        destinations.get_destination_or_404(db, destination_id),
        actor=current.user,
        context=request_audit_context(request),
    )
    reload_log_forwarding(db)


@router.post("/destinations/{destination_id}/test", response_model=SyslogTestResult)
def test_destination(
    destination_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("system:write")),
    db: Session = Depends(get_db),
) -> SyslogTestResult:
    ok, message = destinations.send_test(
        db,
        destinations.get_destination_or_404(db, destination_id),
        settings=request.app.state.settings,
        actor=current.user,
        context=request_audit_context(request),
    )
    return SyslogTestResult(ok=ok, message=message)


@router.get("/status", response_model=SyslogStatus)
def status(
    request: Request,
    current: CurrentUser = Depends(require_permission("system:read")),
    db: Session = Depends(get_db),
) -> SyslogStatus:
    settings = request.app.state.settings
    return SyslogStatus(
        destinations=[_out(db, row) for row in destinations.list_destinations(db)],
        logging=logging_status(settings),
        worker_online=bool(remote_sync.worker_status(db, settings)["worker_online"]),
    )
