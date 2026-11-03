"""API routes for sync jobs, runs and conflicts (Phase 7).

The web process only records intent (jobs, queued runs, cancel flags); the
worker process (``python -m app.worker``) performs every transfer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, request_audit_context, require_permission
from app.core.config import Settings
from app.db.dependencies import get_db
from app.models import SyncJob, SyncRun
from app.schemas.remotes import (
    RemoteSyncStatus,
    SyncConflictOut,
    SyncJobCreate,
    SyncJobOut,
    SyncJobUpdate,
    SyncRunDetail,
    SyncRunOut,
    SyncRunRequest,
)
from app.services import remote_sync

jobs_router = APIRouter(prefix="/sync-jobs", tags=["remote-sync"])
runs_router = APIRouter(prefix="/sync-runs", tags=["remote-sync"])
conflicts_router = APIRouter(prefix="/sync-conflicts", tags=["remote-sync"])
status_router = APIRouter(prefix="/remote-sync", tags=["remote-sync"])


def _job_out(row: SyncJob) -> SyncJobOut:
    return SyncJobOut.model_validate(row)


def _run_detail(row: SyncRun) -> SyncRunDetail:
    return SyncRunDetail(**SyncRunOut.model_validate(row).model_dump(), summary=row.summary)


@status_router.get("/status", response_model=RemoteSyncStatus)
def sync_status(
    request: Request,
    current: CurrentUser = Depends(require_permission("remotes:read")),
    db: Session = Depends(get_db),
) -> RemoteSyncStatus:
    settings: Settings = request.app.state.settings
    return RemoteSyncStatus(
        **remote_sync.worker_status(db, settings),
        public_base_url_set=settings.public_base_url is not None,
        file_roots=[str(root) for root in settings.resolved_file_roots],
    )


@jobs_router.get("", response_model=list[SyncJobOut])
def list_jobs(
    current: CurrentUser = Depends(require_permission("remotes:read")),
    db: Session = Depends(get_db),
) -> list[SyncJobOut]:
    return [_job_out(row) for row in remote_sync.list_jobs(db)]


@jobs_router.post("", response_model=SyncJobOut, status_code=201)
def create_job(
    payload: SyncJobCreate,
    request: Request,
    current: CurrentUser = Depends(require_permission("remotes:write")),
    db: Session = Depends(get_db),
) -> SyncJobOut:
    row = remote_sync.create_job(
        db,
        request.app.state.settings,
        values=payload.model_dump(exclude={"confirm"}),
        confirm=payload.confirm,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _job_out(row)


@jobs_router.get("/{job_id}", response_model=SyncJobOut)
def get_job(
    job_id: int,
    current: CurrentUser = Depends(require_permission("remotes:read")),
    db: Session = Depends(get_db),
) -> SyncJobOut:
    return _job_out(remote_sync.get_job_or_404(db, job_id))


@jobs_router.patch("/{job_id}", response_model=SyncJobOut)
def update_job(
    job_id: int,
    payload: SyncJobUpdate,
    request: Request,
    current: CurrentUser = Depends(require_permission("remotes:write")),
    db: Session = Depends(get_db),
) -> SyncJobOut:
    row = remote_sync.update_job(
        db,
        request.app.state.settings,
        remote_sync.get_job_or_404(db, job_id),
        values=payload.model_dump(exclude={"confirm"}, exclude_unset=True),
        confirm=payload.confirm,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _job_out(row)


@jobs_router.delete("/{job_id}", status_code=204)
def delete_job(
    job_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("remotes:write")),
    db: Session = Depends(get_db),
) -> None:
    remote_sync.delete_job(
        db,
        remote_sync.get_job_or_404(db, job_id),
        actor=current.user,
        context=request_audit_context(request),
    )


@jobs_router.post("/{job_id}/run", response_model=SyncRunDetail, status_code=202)
def run_job(
    job_id: int,
    payload: SyncRunRequest,
    request: Request,
    current: CurrentUser = Depends(require_permission("remotes:write")),
    db: Session = Depends(get_db),
) -> SyncRunDetail:
    """Queue a run (``dry_run`` plans without touching anything)."""
    run = remote_sync.queue_run(
        db,
        remote_sync.get_job_or_404(db, job_id),
        dry_run=payload.dry_run,
        override_deletion_guard=payload.override_deletion_guard,
        confirm=payload.confirm,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _run_detail(run)


@jobs_router.get("/{job_id}/runs", response_model=list[SyncRunOut])
def list_runs(
    job_id: int,
    limit: int = Query(default=20, ge=1, le=200),
    current: CurrentUser = Depends(require_permission("remotes:read")),
    db: Session = Depends(get_db),
) -> list[SyncRunOut]:
    remote_sync.get_job_or_404(db, job_id)
    return [
        SyncRunOut.model_validate(row)
        for row in remote_sync.list_runs(db, job_id=job_id, limit=limit)
    ]


@jobs_router.get("/{job_id}/conflicts", response_model=list[SyncConflictOut])
def list_conflicts(
    job_id: int,
    include_acknowledged: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
    current: CurrentUser = Depends(require_permission("remotes:read")),
    db: Session = Depends(get_db),
) -> list[SyncConflictOut]:
    remote_sync.get_job_or_404(db, job_id)
    rows = remote_sync.list_conflicts(
        db, job_id=job_id, include_acknowledged=include_acknowledged, limit=limit
    )
    return [SyncConflictOut.model_validate(row) for row in rows]


@runs_router.get("/{run_id}", response_model=SyncRunDetail)
def get_run(
    run_id: int,
    current: CurrentUser = Depends(require_permission("remotes:read")),
    db: Session = Depends(get_db),
) -> SyncRunDetail:
    return _run_detail(remote_sync.get_run_or_404(db, run_id))


@runs_router.post("/{run_id}/cancel", response_model=SyncRunDetail)
def cancel_run(
    run_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("remotes:write")),
    db: Session = Depends(get_db),
) -> SyncRunDetail:
    run = remote_sync.cancel_run(
        db,
        remote_sync.get_run_or_404(db, run_id),
        actor=current.user,
        context=request_audit_context(request),
    )
    return _run_detail(run)


@conflicts_router.post("/{conflict_id}/acknowledge", response_model=SyncConflictOut)
def acknowledge_conflict(
    conflict_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("remotes:write")),
    db: Session = Depends(get_db),
) -> SyncConflictOut:
    row = remote_sync.acknowledge_conflict(
        db, conflict_id, actor=current.user, context=request_audit_context(request)
    )
    return SyncConflictOut.model_validate(row)
