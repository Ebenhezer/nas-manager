"""API routes for backup repositories, jobs, runs, snapshots and restores (Phase 11).

The web process records intent (repositories, jobs, queued runs, cancel
flags); the worker performs every backup, verify, prune and restore. Two
calls reach the target synchronously: initializing a repository and listing
a snapshot's files. Endpoints that carry a repository passphrase are
session-only, so a leaked API token can neither plant nor learn one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    CurrentUser,
    request_audit_context,
    require_permission,
    require_session_permission,
)
from app.api.v1.remotes import get_cipher
from app.core.config import Settings
from app.core.errors import ForbiddenError, NotFoundError
from app.core.secrets import CredentialCipher
from app.db.dependencies import get_db
from app.models import BackupRun
from app.models.backup import RUN_RESTORE
from app.schemas.backups import (
    BackupJobCreate,
    BackupJobOut,
    BackupJobUpdate,
    BackupPruneRequest,
    BackupRepositoryCreate,
    BackupRepositoryInitResult,
    BackupRepositoryOut,
    BackupRepositoryUpdate,
    BackupRestoreRequest,
    BackupRunDetail,
    BackupRunOut,
    BackupSnapshotEntries,
    BackupSnapshotEntry,
    BackupSnapshotOut,
    BackupStatus,
    BackupVerifyRequest,
)
from app.services import remote_sync
from app.services.backup import jobs

status_router = APIRouter(prefix="/backup", tags=["backup"])
repositories_router = APIRouter(prefix="/backup-repositories", tags=["backup"])
jobs_router = APIRouter(prefix="/backup-jobs", tags=["backup"])
runs_router = APIRouter(prefix="/backup-runs", tags=["backup"])
snapshots_router = APIRouter(prefix="/backup-snapshots", tags=["backup"])


def _run_detail(row: BackupRun) -> BackupRunDetail:
    return BackupRunDetail(
        **BackupRunOut.model_validate(row).model_dump(), params=row.params, summary=row.summary
    )


@status_router.get("/status", response_model=BackupStatus)
def backup_status(
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:read")),
    db: Session = Depends(get_db),
) -> BackupStatus:
    settings: Settings = request.app.state.settings
    worker = remote_sync.worker_status(db, settings)
    return BackupStatus(
        worker_online=worker["worker_online"],
        worker_heartbeat_at=worker["worker_heartbeat_at"],
        **jobs.status(db),
        local_roots=[str(root) for root in settings.resolved_backup_local_roots],
        file_roots=[str(root) for root in settings.resolved_file_roots],
        staging_dir=str(settings.resolved_backup_staging_dir),
    )


# -- repositories ---------------------------------------------------------------


@repositories_router.get("", response_model=list[BackupRepositoryOut])
def list_repositories(
    current: CurrentUser = Depends(require_permission("backup:read")),
    db: Session = Depends(get_db),
) -> list[BackupRepositoryOut]:
    return [BackupRepositoryOut.model_validate(row) for row in jobs.list_repositories(db)]


@repositories_router.post("", response_model=BackupRepositoryOut, status_code=201)
def create_repository(
    payload: BackupRepositoryCreate,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("backup:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> BackupRepositoryOut:
    row = jobs.create_repository(
        db,
        request.app.state.settings,
        cipher,
        values=payload.model_dump(),
        actor=current.user,
        context=request_audit_context(request),
    )
    return BackupRepositoryOut.model_validate(row)


@repositories_router.get("/{repository_id}", response_model=BackupRepositoryOut)
def get_repository(
    repository_id: int,
    current: CurrentUser = Depends(require_permission("backup:read")),
    db: Session = Depends(get_db),
) -> BackupRepositoryOut:
    return BackupRepositoryOut.model_validate(jobs.get_repository_or_404(db, repository_id))


@repositories_router.patch("/{repository_id}", response_model=BackupRepositoryOut)
def update_repository(
    repository_id: int,
    payload: BackupRepositoryUpdate,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("backup:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> BackupRepositoryOut:
    row = jobs.update_repository(
        db,
        request.app.state.settings,
        cipher,
        jobs.get_repository_or_404(db, repository_id),
        values=payload.model_dump(exclude_unset=True),
        actor=current.user,
        context=request_audit_context(request),
    )
    return BackupRepositoryOut.model_validate(row)


@repositories_router.delete("/{repository_id}", status_code=204)
def delete_repository(
    repository_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:write")),
    db: Session = Depends(get_db),
) -> None:
    """Forget the repository here; the data on the target is left alone."""
    jobs.delete_repository(
        db,
        jobs.get_repository_or_404(db, repository_id),
        actor=current.user,
        context=request_audit_context(request),
    )


@repositories_router.post("/{repository_id}/init", response_model=BackupRepositoryInitResult)
def initialize_repository(
    repository_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> BackupRepositoryInitResult:
    """Create the repository on its target, or adopt the one already there."""
    row, created = jobs.initialize_repository(
        db,
        request.app.state.settings,
        cipher,
        jobs.get_repository_or_404(db, repository_id),
        actor=current.user,
        context=request_audit_context(request),
    )
    return BackupRepositoryInitResult(
        repository=BackupRepositoryOut.model_validate(row),
        created=created,
        snapshots=len(jobs.list_snapshots(db, row.id)),
    )


@repositories_router.post(
    "/{repository_id}/verify", response_model=BackupRunDetail, status_code=202
)
def verify_repository(
    repository_id: int,
    payload: BackupVerifyRequest,
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:write")),
    db: Session = Depends(get_db),
) -> BackupRunDetail:
    repository = jobs.get_repository_or_404(db, repository_id)
    snapshot = None
    if payload.snapshot_id is not None:
        snapshot = jobs.get_snapshot_or_404(db, payload.snapshot_id)
        if snapshot.repository_id != repository.id:
            raise NotFoundError("Snapshot not found", details={"id": payload.snapshot_id})
    run = jobs.queue_verify(
        db,
        repository,
        full=payload.full,
        snapshot=snapshot,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _run_detail(run)


@repositories_router.post("/{repository_id}/prune", response_model=BackupRunDetail, status_code=202)
def prune_repository(
    repository_id: int,
    payload: BackupPruneRequest,
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:write")),
    db: Session = Depends(get_db),
) -> BackupRunDetail:
    """Apply every job's retention policy, then remove unreferenced content.
    ``dry_run`` (the default) only reports what would be removed."""
    run = jobs.queue_prune(
        db,
        jobs.get_repository_or_404(db, repository_id),
        dry_run=payload.dry_run,
        confirm=payload.confirm,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _run_detail(run)


@repositories_router.get("/{repository_id}/snapshots", response_model=list[BackupSnapshotOut])
def list_snapshots(
    repository_id: int,
    job_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=200, ge=1, le=2000),
    current: CurrentUser = Depends(require_permission("backup:read")),
    db: Session = Depends(get_db),
) -> list[BackupSnapshotOut]:
    jobs.get_repository_or_404(db, repository_id)
    return [
        BackupSnapshotOut.model_validate(row)
        for row in jobs.list_snapshots(db, repository_id, job_id=job_id, limit=limit)
    ]


# -- jobs -----------------------------------------------------------------------


@jobs_router.get("", response_model=list[BackupJobOut])
def list_jobs(
    current: CurrentUser = Depends(require_permission("backup:read")),
    db: Session = Depends(get_db),
) -> list[BackupJobOut]:
    return [BackupJobOut.model_validate(row) for row in jobs.list_jobs(db)]


@jobs_router.post("", response_model=BackupJobOut, status_code=201)
def create_job(
    payload: BackupJobCreate,
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:write")),
    db: Session = Depends(get_db),
) -> BackupJobOut:
    row = jobs.create_job(
        db,
        request.app.state.settings,
        values=payload.model_dump(exclude={"confirm"}),
        confirm=payload.confirm,
        actor=current.user,
        context=request_audit_context(request),
    )
    return BackupJobOut.model_validate(row)


@jobs_router.get("/{job_id}", response_model=BackupJobOut)
def get_job(
    job_id: int,
    current: CurrentUser = Depends(require_permission("backup:read")),
    db: Session = Depends(get_db),
) -> BackupJobOut:
    return BackupJobOut.model_validate(jobs.get_job_or_404(db, job_id))


@jobs_router.patch("/{job_id}", response_model=BackupJobOut)
def update_job(
    job_id: int,
    payload: BackupJobUpdate,
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:write")),
    db: Session = Depends(get_db),
) -> BackupJobOut:
    row = jobs.update_job(
        db,
        request.app.state.settings,
        jobs.get_job_or_404(db, job_id),
        values=payload.model_dump(exclude={"confirm"}, exclude_unset=True),
        confirm=payload.confirm,
        actor=current.user,
        context=request_audit_context(request),
    )
    return BackupJobOut.model_validate(row)


@jobs_router.delete("/{job_id}", status_code=204)
def delete_job(
    job_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:write")),
    db: Session = Depends(get_db),
) -> None:
    jobs.delete_job(
        db,
        jobs.get_job_or_404(db, job_id),
        actor=current.user,
        context=request_audit_context(request),
    )


@jobs_router.post("/{job_id}/run", response_model=BackupRunDetail, status_code=202)
def run_job(
    job_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:write")),
    db: Session = Depends(get_db),
) -> BackupRunDetail:
    run = jobs.queue_backup(
        db,
        jobs.get_job_or_404(db, job_id),
        actor=current.user,
        context=request_audit_context(request),
    )
    return _run_detail(run)


# -- runs -----------------------------------------------------------------------


@runs_router.get("", response_model=list[BackupRunOut])
def list_runs(
    repository_id: int | None = Query(default=None, ge=1),
    job_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    current: CurrentUser = Depends(require_permission("backup:read")),
    db: Session = Depends(get_db),
) -> list[BackupRunOut]:
    rows = jobs.list_runs(db, repository_id=repository_id, job_id=job_id, limit=limit)
    return [BackupRunOut.model_validate(row) for row in rows]


@runs_router.get("/{run_id}", response_model=BackupRunDetail)
def get_run(
    run_id: int,
    current: CurrentUser = Depends(require_permission("backup:read")),
    db: Session = Depends(get_db),
) -> BackupRunDetail:
    return _run_detail(jobs.get_run_or_404(db, run_id))


@runs_router.post("/{run_id}/cancel", response_model=BackupRunDetail)
def cancel_run(
    run_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:write")),
    db: Session = Depends(get_db),
) -> BackupRunDetail:
    run = jobs.get_run_or_404(db, run_id)
    if run.kind == RUN_RESTORE and "backup:restore" not in current.permissions:
        raise ForbiddenError(
            "Missing required permission: backup:restore",
            details={"permission": "backup:restore"},
        )
    return _run_detail(
        jobs.cancel_run(db, run, actor=current.user, context=request_audit_context(request))
    )


# -- snapshots ------------------------------------------------------------------


@snapshots_router.get("/{snapshot_id}", response_model=BackupSnapshotOut)
def get_snapshot(
    snapshot_id: int,
    current: CurrentUser = Depends(require_permission("backup:read")),
    db: Session = Depends(get_db),
) -> BackupSnapshotOut:
    return BackupSnapshotOut.model_validate(jobs.get_snapshot_or_404(db, snapshot_id))


@snapshots_router.get("/{snapshot_id}/entries", response_model=BackupSnapshotEntries)
def snapshot_entries(
    snapshot_id: int,
    request: Request,
    prefix: str = Query(default="", max_length=4096),
    current: CurrentUser = Depends(require_permission("backup:read")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> BackupSnapshotEntries:
    """The files in a snapshot (decrypted from the repository on demand)."""
    settings: Settings = request.app.state.settings
    snapshot = jobs.get_snapshot_or_404(db, snapshot_id)
    entries, total = jobs.snapshot_entries(
        db, settings, cipher, snapshot, prefix=prefix, limit=settings.backup_browse_limit
    )
    return BackupSnapshotEntries(
        snapshot=BackupSnapshotOut.model_validate(snapshot),
        prefix=prefix,
        total=total,
        truncated=total > len(entries),
        entries=[BackupSnapshotEntry.model_validate(entry) for entry in entries],
    )


@snapshots_router.post("/{snapshot_id}/restore", response_model=BackupRunDetail, status_code=202)
def restore_snapshot(
    snapshot_id: int,
    payload: BackupRestoreRequest,
    request: Request,
    current: CurrentUser = Depends(require_permission("backup:restore")),
    db: Session = Depends(get_db),
) -> BackupRunDetail:
    """Queue a restore. By default files go into a new folder and existing
    files are never replaced; ``overwrite`` needs ``confirm: true`` and moves
    the current versions aside instead of deleting them."""
    run = jobs.queue_restore(
        db,
        request.app.state.settings,
        jobs.get_snapshot_or_404(db, snapshot_id),
        paths=payload.paths,
        target=payload.target.model_dump(),
        overwrite=payload.overwrite,
        dry_run=payload.dry_run,
        confirm=payload.confirm,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _run_detail(run)
