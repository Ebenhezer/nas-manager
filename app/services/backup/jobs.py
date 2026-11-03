"""Backup repositories, jobs and runs (Phase 11): CRUD, queueing and the
worker primitives.

The web process records intent only: repositories, jobs and queued runs. The
worker claims runs with an atomic conditional ``UPDATE`` (at most one active
run per repository, so garbage collection can never race a backup that is
still uploading blobs), renews its lease every tick and executes them with
``process_run``. Initializing a repository is the one synchronous action:
the administrator sees at once whether the target is reachable and whether
the passphrase unlocks an existing repository.

Anything that can replace data or remove backup versions needs
``confirm: true``: enabling a scheduled job, a prune run, and a restore
that overwrites existing files.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, exists, func, select, update
from sqlalchemy.orm import Session, aliased

from app.core.config import Settings
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.models import BackupJob, BackupRepository, BackupRun, BackupSnapshot, User
from app.models.backup import (
    RUN_BACKUP,
    RUN_PRUNE,
    RUN_RESTORE,
    RUN_VERIFY,
    SOURCE_FILES,
    SOURCE_KINDS,
    SOURCE_MEDIA,
    SOURCE_REMOTE,
    SOURCE_SYSTEM,
    TARGET_KINDS,
    TARGET_LOCAL,
)
from app.models.remote import (
    RUN_ACTIVE_STATES,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
)
from app.services import audit, files, remotes
from app.services.backup import crypto, engine, sources
from app.services.backup.repository import Repository
from app.services.remote.base import RemoteProvider, normalize_remote_path
from app.services.remote.local import LocalProvider
from app.services.remote_sync import ConfirmationRequiredError

logger = logging.getLogger(__name__)

WORKER_NAME = "backup-worker"
_MAX_EXCLUDES = 50
_PATTERN_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_SCHEDULE_MAX_MINUTES = 60 * 24 * 31
_KEEP_MAX = 1000
RESTORE_TARGETS = ("files", "media", "remote", "staging")


# ---------------------------------------------------------------------------
# Shared validation
# ---------------------------------------------------------------------------


def _clean_name(name: str, what: str) -> str:
    value = name.strip()
    if not value or len(value) > 64 or any(ord(c) < 32 for c in value):
        raise BadRequestError(f"{what} name must be 1-64 printable characters")
    return value


def _require_confirmation(found: list[str], confirm: bool) -> None:
    if found and not confirm:
        raise ConfirmationRequiredError(
            "This action needs confirmation; repeat it with confirm=true",
            details={"warnings": found},
        )


def _clean_excludes(patterns: list[str]) -> list[str]:
    if len(patterns) > _MAX_EXCLUDES:
        raise BadRequestError(f"At most {_MAX_EXCLUDES} exclude patterns are allowed")
    cleaned = []
    for pattern in patterns:
        value = pattern.strip()
        if not _PATTERN_RE.match(value):
            raise BadRequestError("Exclude patterns must be single-line, 1-200 characters")
        cleaned.append(value)
    return cleaned


def _schedule(value: int | None) -> int | None:
    if value is None:
        return None
    if not 1 <= value <= _SCHEDULE_MAX_MINUTES:
        raise BadRequestError("schedule_minutes must be between 1 and 44640 (31 days)")
    return value


def _keep(value: Any, field: str) -> int:
    number = int(value)
    if not 0 <= number <= _KEEP_MAX:
        raise BadRequestError(f"{field} must be between 0 and {_KEEP_MAX}")
    return number


def resolve_local_root(settings: Settings, root_str: str) -> Path:
    """The configured backup root matching ``root_str`` exactly."""
    candidate = Path(root_str or "").expanduser().resolve(strict=False)
    for root in settings.resolved_backup_local_roots:
        if candidate == root.resolve(strict=False):
            return candidate
    raise BadRequestError(
        "Unknown backup root; it must be one of NAS_BACKUP_LOCAL_ROOTS",
        details={
            "root": root_str,
            "allowed": [str(r) for r in settings.resolved_backup_local_roots],
        },
    )


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


def list_repositories(session: Session) -> list[BackupRepository]:
    return list(session.execute(select(BackupRepository).order_by(BackupRepository.name)).scalars())


def get_repository_or_404(session: Session, repository_id: int) -> BackupRepository:
    row = session.get(BackupRepository, repository_id)
    if row is None:
        raise NotFoundError("Backup repository not found", details={"id": repository_id})
    return row


def _repository_details(row: BackupRepository) -> dict[str, Any]:
    return {
        "name": row.name,
        "target_kind": row.target_kind,
        "connection_id": row.connection_id,
        "local_root": row.local_root,
        "repo_path": row.repo_path,
    }


def _ensure_unique_repository(session: Session, name: str, exclude_id: int | None = None) -> None:
    stmt = select(BackupRepository.id).where(func.lower(BackupRepository.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(BackupRepository.id != exclude_id)
    if session.execute(stmt).first() is not None:
        raise ConflictError("A backup repository with that name already exists")


def _check_passphrase(passphrase: str) -> str:
    if len(passphrase) < crypto.MIN_PASSPHRASE_LENGTH or len(passphrase) > 1024:
        raise BadRequestError(
            f"The passphrase must be {crypto.MIN_PASSPHRASE_LENGTH}-1024 characters long"
        )
    return passphrase


def _apply_target(
    session: Session, settings: Settings, row: BackupRepository, values: dict[str, Any]
) -> None:
    kind = str(values.get("target_kind") or row.target_kind or "")
    if kind not in TARGET_KINDS:
        raise BadRequestError("Unknown target kind", details={"allowed": list(TARGET_KINDS)})
    row.target_kind = kind
    if kind == TARGET_LOCAL:
        row.local_root = str(
            resolve_local_root(settings, str(values.get("local_root") or row.local_root or ""))
        )
        row.connection_id = None
    else:
        connection_id = values.get("connection_id") or row.connection_id
        if connection_id is None:
            raise BadRequestError("A remote repository needs connection_id")
        remotes.get_connection_or_404(session, int(connection_id))
        row.connection_id = int(connection_id)
        row.local_root = None
    if values.get("repo_path") is not None:
        row.repo_path = normalize_remote_path(str(values["repo_path"]))


def create_repository(
    session: Session,
    settings: Settings,
    cipher: CredentialCipher,
    *,
    values: dict[str, Any],
    actor: User,
    context: dict[str, str | None] | None = None,
) -> BackupRepository:
    name = _clean_name(str(values["name"]), "Repository")
    _ensure_unique_repository(session, name)
    passphrase = _check_passphrase(str(values["passphrase"]))
    row = BackupRepository(
        name=name,
        target_kind="",
        repo_path="",
        passphrase_encrypted=cipher.encrypt_json({"passphrase": passphrase}),
        created_by=actor.id,
    )
    _apply_target(session, settings, row, values | {"repo_path": values.get("repo_path") or ""})
    if row.target_kind == TARGET_LOCAL and not row.repo_path:
        raise BadRequestError(
            "A local repository needs repo_path (a folder inside the backup root)"
        )
    session.add(row)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.BACKUP_REPOSITORY_CREATED,
        category=audit.CATEGORY_BACKUP,
        actor=actor,
        target_type="backup_repository",
        target_id=row.id,
        details=_repository_details(row),
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def _has_active_run(session: Session, repository_id: int) -> bool:
    return (
        session.execute(
            select(BackupRun.id).where(
                BackupRun.repository_id == repository_id,
                BackupRun.status.in_(RUN_ACTIVE_STATES),
            )
        ).first()
        is not None
    )


def update_repository(
    session: Session,
    settings: Settings,
    cipher: CredentialCipher,
    row: BackupRepository,
    *,
    values: dict[str, Any],
    actor: User,
    context: dict[str, str | None] | None = None,
) -> BackupRepository:
    """Rename, or fix the location or passphrase of a repository.

    Moving or re-keying an initialized repository marks it uninitialized
    again, so the next ``initialize`` proves the new location and
    passphrase before any run uses them.
    """
    before = _repository_details(row)
    if values.get("name") is not None:
        name = _clean_name(str(values["name"]), "Repository")
        _ensure_unique_repository(session, name, exclude_id=row.id)
        row.name = name
    location_keys = {"target_kind", "local_root", "connection_id", "repo_path"}
    location_changed = any(values.get(key) is not None for key in location_keys)
    passphrase_changed = values.get("passphrase") is not None
    if (location_changed or passphrase_changed) and _has_active_run(session, row.id):
        raise ConflictError("The repository has an active run; wait for it or cancel it first")
    if location_changed:
        _apply_target(session, settings, row, values)
    if passphrase_changed:
        row.passphrase_encrypted = cipher.encrypt_json(
            {"passphrase": _check_passphrase(str(values["passphrase"]))}
        )
    after = _repository_details(row)
    changed = sorted(key for key in after if after[key] != before[key])
    if (set(changed) & location_keys) or passphrase_changed:
        row.initialized_at = None
    audit.record_audit_event(
        session,
        event=audit.BACKUP_REPOSITORY_UPDATED,
        category=audit.CATEGORY_BACKUP,
        actor=actor,
        target_type="backup_repository",
        target_id=row.id,
        details={
            "changed": {key: after[key] for key in changed},
            "passphrase_changed": passphrase_changed,
        },
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def delete_repository(
    session: Session,
    row: BackupRepository,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    """Forget the repository (catalogue and history). Never touches the target."""
    if session.execute(select(BackupJob.id).where(BackupJob.repository_id == row.id)).first():
        raise ConflictError("Backup jobs still use this repository; delete them first")
    if _has_active_run(session, row.id):
        raise ConflictError("The repository has an active run; cancel it first")
    audit.record_audit_event(
        session,
        event=audit.BACKUP_REPOSITORY_DELETED,
        category=audit.CATEGORY_BACKUP,
        actor=actor,
        target_type="backup_repository",
        target_id=row.id,
        details=_repository_details(row),
        **audit.request_context(context),
        commit=False,
    )
    session.delete(row)
    session.commit()


def passphrase_of(row: BackupRepository, cipher: CredentialCipher) -> str:
    return str(cipher.decrypt_json(row.passphrase_encrypted).get("passphrase") or "")


def build_target_provider(
    session: Session, settings: Settings, row: BackupRepository, cipher: CredentialCipher
) -> RemoteProvider:
    if row.target_kind == TARGET_LOCAL:
        return LocalProvider(resolve_local_root(settings, row.local_root or ""))
    from app.models import RemoteConnection

    connection = session.get(RemoteConnection, row.connection_id)
    if connection is None:  # pragma: no cover - FK RESTRICT
        raise NotFoundError("The repository's remote connection no longer exists")
    return remotes.build_provider(connection, cipher, settings)


def open_repository(
    session: Session,
    settings: Settings,
    row: BackupRepository,
    cipher: CredentialCipher,
    provider: RemoteProvider,
) -> Repository:
    return Repository.open(
        provider,
        row.repo_path,
        passphrase_of(row, cipher),
        staging=settings.resolved_backup_staging_dir / "tmp",
    )


def initialize_repository(
    session: Session,
    settings: Settings,
    cipher: CredentialCipher,
    row: BackupRepository,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
    provider: RemoteProvider | None = None,
) -> tuple[BackupRepository, bool]:
    """Create the repository on its target, or adopt an existing one."""
    if _has_active_run(session, row.id):
        raise ConflictError("The repository has an active run; wait for it first")
    target = provider or build_target_provider(session, settings, row, cipher)
    with target:
        repo, created = Repository.initialize(
            target,
            row.repo_path,
            passphrase_of(row, cipher),
            staging=settings.resolved_backup_staging_dir / "tmp",
            scrypt_n=settings.backup_scrypt_n,
        )
        on_target, unreadable = engine.sync_catalogue(session, row, repo)
    row.initialized_at = utcnow_naive()
    audit.record_audit_event(
        session,
        event=audit.BACKUP_REPOSITORY_INITIALIZED,
        category=audit.CATEGORY_BACKUP,
        actor=actor,
        target_type="backup_repository",
        target_id=row.id,
        details={
            "created": created,
            "snapshots_found": len(on_target),
            "unreadable_snapshots": len(unreadable),
        },
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row, created


def list_snapshots(
    session: Session, repository_id: int, *, job_id: int | None = None, limit: int = 500
) -> list[BackupSnapshot]:
    stmt = select(BackupSnapshot).where(BackupSnapshot.repository_id == repository_id)
    if job_id is not None:
        stmt = stmt.where(BackupSnapshot.job_id == job_id)
    return list(
        session.execute(stmt.order_by(BackupSnapshot.taken_at.desc()).limit(limit)).scalars()
    )


def get_snapshot_or_404(session: Session, snapshot_row_id: int) -> BackupSnapshot:
    row = session.get(BackupSnapshot, snapshot_row_id)
    if row is None:
        raise NotFoundError("Snapshot not found", details={"id": snapshot_row_id})
    return row


def snapshot_entries(
    session: Session,
    settings: Settings,
    cipher: CredentialCipher,
    snapshot: BackupSnapshot,
    *,
    prefix: str,
    limit: int,
    provider: RemoteProvider | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Entries of a snapshot under ``prefix`` (read from the repository)."""
    repository = get_repository_or_404(session, snapshot.repository_id)
    _require_initialized(repository)
    target = provider or build_target_provider(session, settings, repository, cipher)
    with target:
        repo = open_repository(session, settings, repository, cipher, target)
        manifest = repo.read_snapshot(snapshot.snapshot_id)
    selected = engine.select_entries(manifest, [prefix] if prefix else [])
    return [
        {"path": e.get("path"), "size": e.get("size"), "mtime_ns": e.get("mtime_ns")}
        for e in selected[:limit]
    ], len(selected)


def _require_initialized(row: BackupRepository) -> None:
    if row.initialized_at is None:
        raise ConflictError("Initialize the repository first", details={"repository_id": row.id})


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def list_jobs(session: Session) -> list[BackupJob]:
    return list(session.execute(select(BackupJob).order_by(BackupJob.name)).scalars())


def get_job_or_404(session: Session, job_id: int) -> BackupJob:
    row = session.get(BackupJob, job_id)
    if row is None:
        raise NotFoundError("Backup job not found", details={"id": job_id})
    return row


def _job_details(job: BackupJob) -> dict[str, Any]:
    return {
        "name": job.name,
        "repository_id": job.repository_id,
        "source_kind": job.source_kind,
        "source_root": job.source_root,
        "source_path": job.source_path,
        "source_connection_id": job.source_connection_id,
        "exclude_patterns": job.exclude_patterns,
        "schedule_minutes": job.schedule_minutes,
        "enabled": job.enabled,
        "keep_last": job.keep_last,
        "keep_daily": job.keep_daily,
        "keep_weekly": job.keep_weekly,
        "keep_monthly": job.keep_monthly,
    }


def _ensure_unique_job(session: Session, name: str, exclude_id: int | None = None) -> None:
    stmt = select(BackupJob.id).where(func.lower(BackupJob.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(BackupJob.id != exclude_id)
    if session.execute(stmt).first() is not None:
        raise ConflictError("A backup job with that name already exists")


def _apply_source(session: Session, settings: Settings, job: BackupJob) -> None:
    """Validate and normalize the job's source fields in place."""
    kind = job.source_kind
    if kind not in SOURCE_KINDS:
        raise BadRequestError("Unknown source kind", details={"allowed": list(SOURCE_KINDS)})
    if kind == SOURCE_FILES:
        root = files.resolve_root(settings, job.source_root or "")
        target = files.resolve_in_root(root, job.source_path)
        if not target.is_dir():
            raise BadRequestError(
                "The source must be an existing folder", details={"path": job.source_path}
            )
        rel = target.relative_to(root).as_posix()
        job.source_root, job.source_path = str(root), "" if rel == "." else rel
        job.source_connection_id = None
    elif kind == SOURCE_MEDIA:
        media = settings.resolved_media_dir.resolve(strict=False)
        target = files.resolve_in_root(media, job.source_path)
        rel = target.relative_to(media).as_posix()
        job.source_root, job.source_path = None, "" if rel == "." else rel
        job.source_connection_id = None
    elif kind == SOURCE_SYSTEM:
        sources.sqlite_path(settings)  # refuses non-SQLite deployments up front
        job.source_root, job.source_path, job.source_connection_id = None, "", None
    else:
        if job.source_connection_id is None:
            raise BadRequestError("A pull backup needs source_connection_id")
        remotes.get_connection_or_404(session, job.source_connection_id)
        job.source_root = None
        job.source_path = normalize_remote_path(job.source_path)
    repository = get_repository_or_404(session, job.repository_id)
    if kind == SOURCE_REMOTE and repository.target_kind != TARGET_LOCAL:
        raise BadRequestError(
            "Pull backups copy remote data onto this NAS: choose a local repository"
        )


def job_risks(job: BackupJob, *, was_enabled: bool) -> list[str]:
    found: list[str] = []
    if job.enabled and not was_enabled and job.schedule_minutes:
        found.append("The job will run on its schedule without further confirmation.")
    if job.enabled and job.retention_enabled and not was_enabled:
        found.append(
            "After each backup, snapshots outside the retention policy "
            f"(last {job.keep_last}, daily {job.keep_daily}, weekly {job.keep_weekly}, "
            f"monthly {job.keep_monthly}) are deleted from the repository."
        )
    return found


def _reschedule(job: BackupJob, now: datetime) -> None:
    job.next_run_at = now if job.enabled and job.schedule_minutes else None


def create_job(
    session: Session,
    settings: Settings,
    *,
    values: dict[str, Any],
    confirm: bool,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> BackupJob:
    name = _clean_name(str(values["name"]), "Job")
    _ensure_unique_job(session, name)
    job = BackupJob(
        name=name,
        repository_id=int(values["repository_id"]),
        source_kind=str(values["source_kind"]),
        source_root=values.get("source_root"),
        source_path=str(values.get("source_path") or ""),
        source_connection_id=values.get("source_connection_id"),
        schedule_minutes=_schedule(values.get("schedule_minutes")),
        enabled=bool(values.get("enabled", False)),
        keep_last=_keep(values.get("keep_last", 7), "keep_last"),
        keep_daily=_keep(values.get("keep_daily", 7), "keep_daily"),
        keep_weekly=_keep(values.get("keep_weekly", 4), "keep_weekly"),
        keep_monthly=_keep(values.get("keep_monthly", 6), "keep_monthly"),
        created_by=actor.id,
    )
    job.exclude_patterns = _clean_excludes(list(values.get("exclude_patterns") or []))
    _apply_source(session, settings, job)
    _require_confirmation(job_risks(job, was_enabled=False), confirm)
    _reschedule(job, utcnow_naive())
    session.add(job)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.BACKUP_JOB_CREATED,
        category=audit.CATEGORY_BACKUP,
        actor=actor,
        target_type="backup_job",
        target_id=job.id,
        details=_job_details(job) | {"confirmed": confirm},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return job


def update_job(
    session: Session,
    settings: Settings,
    job: BackupJob,
    *,
    values: dict[str, Any],
    confirm: bool,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> BackupJob:
    before = _job_details(job)
    was_enabled = job.enabled
    if values.get("name") is not None:
        name = _clean_name(str(values["name"]), "Job")
        _ensure_unique_job(session, name, exclude_id=job.id)
        job.name = name
    for key in ("repository_id", "source_kind", "source_root", "source_connection_id"):
        if values.get(key) is not None:
            setattr(job, key, values[key])
    if values.get("source_path") is not None:
        job.source_path = str(values["source_path"])
    if "exclude_patterns" in values and values["exclude_patterns"] is not None:
        job.exclude_patterns = _clean_excludes(list(values["exclude_patterns"]))
    if "schedule_minutes" in values:
        job.schedule_minutes = _schedule(values["schedule_minutes"])
    if values.get("enabled") is not None:
        job.enabled = bool(values["enabled"])
    for key in ("keep_last", "keep_daily", "keep_weekly", "keep_monthly"):
        if values.get(key) is not None:
            setattr(job, key, _keep(values[key], key))
    _apply_source(session, settings, job)
    after = _job_details(job)
    changed = sorted(key for key in after if after[key] != before[key])
    risks = job_risks(job, was_enabled=was_enabled)
    shrinking = [
        key
        for key in ("keep_last", "keep_daily", "keep_weekly", "keep_monthly")
        if key in changed and int(after[key]) < int(before[key]) and job.retention_enabled
    ]
    if shrinking:
        risks.append(
            "The retention policy now keeps fewer snapshots; older ones are deleted "
            "after the next backup or prune."
        )
    _require_confirmation(risks, confirm)
    if "repository_id" in changed and _has_active_run(session, before["repository_id"]):
        raise ConflictError("The job's repository has an active run; wait for it first")
    if {"enabled", "schedule_minutes"} & set(changed):
        _reschedule(job, utcnow_naive())
    audit.record_audit_event(
        session,
        event=audit.BACKUP_JOB_UPDATED,
        category=audit.CATEGORY_BACKUP,
        actor=actor,
        target_type="backup_job",
        target_id=job.id,
        details={"changed": {key: after[key] for key in changed}, "confirmed": confirm},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return job


def delete_job(
    session: Session,
    job: BackupJob,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    """Delete the job definition. Its snapshots stay in the repository (and
    are then never pruned automatically)."""
    active = session.execute(
        select(BackupRun.id).where(
            BackupRun.job_id == job.id, BackupRun.status.in_(RUN_ACTIVE_STATES)
        )
    ).first()
    if active is not None:
        raise ConflictError("The job has an active run; cancel it first")
    audit.record_audit_event(
        session,
        event=audit.BACKUP_JOB_DELETED,
        category=audit.CATEGORY_BACKUP,
        actor=actor,
        target_type="backup_job",
        target_id=job.id,
        details=_job_details(job),
        **audit.request_context(context),
        commit=False,
    )
    session.delete(job)
    session.commit()


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def queue_run(
    session: Session,
    *,
    repository: BackupRepository,
    kind: str,
    job: BackupJob | None = None,
    dry_run: bool = False,
    params: dict[str, Any] | None = None,
    actor: User | None,
    trigger: str = TRIGGER_MANUAL,
    context: dict[str, str | None] | None = None,
) -> BackupRun:
    _require_initialized(repository)
    duplicate = select(BackupRun.id).where(
        BackupRun.repository_id == repository.id,
        BackupRun.kind == kind,
        BackupRun.status.in_(RUN_ACTIVE_STATES),
    )
    if job is not None:
        duplicate = duplicate.where(BackupRun.job_id == job.id)
    if kind != RUN_RESTORE and session.execute(duplicate).first() is not None:
        raise ConflictError("An identical run is already queued or running")
    run = BackupRun(
        repository_id=repository.id,
        job_id=job.id if job is not None else None,
        kind=kind,
        status=RUN_QUEUED,
        trigger=trigger,
        dry_run=dry_run,
        requested_by=actor.id if actor is not None else None,
    )
    run.params = params or {}
    session.add(run)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.BACKUP_RUN_QUEUED,
        category=audit.CATEGORY_BACKUP,
        actor=actor,
        actor_username=None if actor is not None else WORKER_NAME,
        target_type="backup_repository",
        target_id=repository.id,
        details={
            "run_id": run.id,
            "kind": kind,
            "job_id": run.job_id,
            "dry_run": dry_run,
            "trigger": trigger,
            "params": run.params,
        },
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return run


def queue_backup(
    session: Session,
    job: BackupJob,
    *,
    actor: User | None,
    trigger: str = TRIGGER_MANUAL,
    context: dict[str, str | None] | None = None,
) -> BackupRun:
    return queue_run(
        session,
        repository=get_repository_or_404(session, job.repository_id),
        kind=RUN_BACKUP,
        job=job,
        actor=actor,
        trigger=trigger,
        context=context,
    )


def queue_verify(
    session: Session,
    repository: BackupRepository,
    *,
    full: bool,
    snapshot: BackupSnapshot | None,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> BackupRun:
    params: dict[str, Any] = {"full": full}
    if snapshot is not None:
        params["snapshot_id"] = snapshot.snapshot_id
    return queue_run(
        session,
        repository=repository,
        kind=RUN_VERIFY,
        params=params,
        actor=actor,
        context=context,
    )


def queue_prune(
    session: Session,
    repository: BackupRepository,
    *,
    dry_run: bool,
    confirm: bool,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> BackupRun:
    if not dry_run:
        forget = engine.plan_retention(session, repository.id)
        _require_confirmation(
            [
                f"{len(forget)} snapshot(s) outside their job's retention policy will be "
                "deleted, then content no snapshot references is removed. This cannot be "
                "undone."
            ],
            confirm,
        )
    return queue_run(
        session,
        repository=repository,
        kind=RUN_PRUNE,
        dry_run=dry_run,
        actor=actor,
        context=context,
    )


def queue_restore(
    session: Session,
    settings: Settings,
    snapshot: BackupSnapshot,
    *,
    paths: list[str],
    target: dict[str, Any],
    overwrite: bool,
    dry_run: bool,
    confirm: bool,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> BackupRun:
    repository = get_repository_or_404(session, snapshot.repository_id)
    kind = str(target.get("kind") or "")
    if kind not in RESTORE_TARGETS:
        raise BadRequestError("Unknown restore target", details={"allowed": list(RESTORE_TARGETS)})
    cleaned_paths = [normalize_remote_path(p) for p in paths]
    if len(cleaned_paths) > 1000:
        raise BadRequestError("At most 1000 paths can be selected")
    clean_target: dict[str, Any] = {"kind": kind}
    if kind == "files":
        root = files.resolve_root(settings, str(target.get("root") or ""))
        base = files.resolve_in_root(root, str(target.get("path") or ""))
        rel = base.relative_to(root).as_posix()
        clean_target |= {"root": str(root), "path": "" if rel == "." else rel}
    elif kind == "media":
        media = settings.resolved_media_dir.resolve(strict=False)
        media_rel = files.resolve_in_root(media, str(target.get("path") or "")).relative_to(media)
        clean_target |= {"path": "" if media_rel.as_posix() == "." else media_rel.as_posix()}
    elif kind == "remote":
        connection_id = target.get("connection_id")
        if connection_id is None:
            raise BadRequestError("A remote restore target needs connection_id")
        remotes.get_connection_or_404(session, int(connection_id))
        clean_target |= {
            "connection_id": int(connection_id),
            "path": normalize_remote_path(str(target.get("path") or "")),
        }
    elif overwrite:
        raise BadRequestError("Staged restores never overwrite")
    if overwrite and not dry_run:
        _require_confirmation(
            [
                "Existing files with the same names will be replaced. The current versions "
                f"are moved into {engine.REPLACED_DIR}/<timestamp>/ inside the target folder, "
                "never deleted."
            ],
            confirm,
        )
    return queue_run(
        session,
        repository=repository,
        kind=RUN_RESTORE,
        dry_run=dry_run,
        params={
            "snapshot_id": snapshot.snapshot_id,
            "paths": cleaned_paths,
            "target": clean_target,
            "overwrite": overwrite,
        },
        actor=actor,
        context=context,
    )


def list_runs(
    session: Session,
    *,
    repository_id: int | None = None,
    job_id: int | None = None,
    limit: int = 50,
) -> list[BackupRun]:
    stmt = select(BackupRun)
    if repository_id is not None:
        stmt = stmt.where(BackupRun.repository_id == repository_id)
    if job_id is not None:
        stmt = stmt.where(BackupRun.job_id == job_id)
    return list(session.execute(stmt.order_by(BackupRun.id.desc()).limit(limit)).scalars())


def get_run_or_404(session: Session, run_id: int) -> BackupRun:
    row = session.get(BackupRun, run_id)
    if row is None:
        raise NotFoundError("Backup run not found", details={"id": run_id})
    return row


def cancel_run(
    session: Session,
    run: BackupRun,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> BackupRun:
    if not run.is_active:
        raise ConflictError("The run has already finished", details={"status": run.status})
    if run.status == RUN_QUEUED:
        result = session.execute(
            update(BackupRun)
            .where(BackupRun.id == run.id, BackupRun.status == RUN_QUEUED)
            .values(status=RUN_CANCELLED, finished_at=utcnow_naive())
        )
        if result.rowcount:  # type: ignore[attr-defined]
            session.expire(run)
    if run.status != RUN_CANCELLED:
        run.cancel_requested = True
    audit.record_audit_event(
        session,
        event=audit.BACKUP_RUN_CANCEL_REQUESTED,
        category=audit.CATEGORY_BACKUP,
        actor=actor,
        target_type="backup_repository",
        target_id=run.repository_id,
        details={"run_id": run.id, "kind": run.kind},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    session.refresh(run)
    return run


def status(session: Session) -> dict[str, int]:
    counts = dict(
        session.execute(
            select(BackupRun.status, func.count(BackupRun.id))
            .where(BackupRun.status.in_(RUN_ACTIVE_STATES))
            .group_by(BackupRun.status)
        ).all()
    )
    return {
        "queued_runs": int(counts.get(RUN_QUEUED, 0)),
        "running_runs": int(counts.get(RUN_RUNNING, 0)),
    }


# ---------------------------------------------------------------------------
# Worker primitives
# ---------------------------------------------------------------------------


def enqueue_due_jobs(session: Session, now: datetime) -> list[BackupRun]:
    queued: list[BackupRun] = []
    due = session.execute(
        select(BackupJob).where(
            BackupJob.enabled.is_(True),
            BackupJob.schedule_minutes.is_not(None),
            BackupJob.next_run_at.is_not(None),
            BackupJob.next_run_at <= now,
        )
    ).scalars()
    for job in list(due):
        assert job.schedule_minutes is not None  # noqa: S101 - filtered above
        job.next_run_at = now + timedelta(minutes=job.schedule_minutes)
        try:
            queued.append(queue_backup(session, job, actor=None, trigger=TRIGGER_SCHEDULE))
        except (ConflictError, AppError) as exc:
            session.rollback()
            job = session.get(BackupJob, job.id) or job
            job.next_run_at = now + timedelta(minutes=job.schedule_minutes or 1)
            session.commit()
            logger.info(
                "scheduled backup not queued", extra={"job_id": job.id, "reason": exc.message}
            )
    return queued


def reclaim_expired(session: Session, now: datetime) -> int:
    stale = list(
        session.execute(
            select(BackupRun).where(
                BackupRun.status == RUN_RUNNING,
                BackupRun.lease_expires_at.is_not(None),
                BackupRun.lease_expires_at < now,
            )
        ).scalars()
    )
    for run in stale:
        finish(session, run, RUN_FAILED, error="The worker stopped while running this job")
    return len(stale)


def claim_next(session: Session, *, owner: str, lease_seconds: int) -> BackupRun | None:
    """Atomically start the oldest queued run whose repository is idle."""
    other = aliased(BackupRun)
    busy = exists().where(
        and_(other.repository_id == BackupRun.repository_id, other.status == RUN_RUNNING)
    )
    candidates = session.execute(
        select(BackupRun.id).where(BackupRun.status == RUN_QUEUED).order_by(BackupRun.id).limit(20)
    ).scalars()
    for run_id in list(candidates):
        now = utcnow_naive()
        result = session.execute(
            update(BackupRun)
            .where(BackupRun.id == run_id, BackupRun.status == RUN_QUEUED, ~busy)
            .values(
                status=RUN_RUNNING,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                started_at=now,
            )
        )
        session.commit()
        if result.rowcount == 1:  # type: ignore[attr-defined]
            return session.get(BackupRun, run_id, populate_existing=True)
    return None


def renew_leases(session: Session, *, owner: str, lease_seconds: int) -> None:
    session.execute(
        update(BackupRun)
        .where(BackupRun.status == RUN_RUNNING, BackupRun.lease_owner == owner)
        .values(lease_expires_at=utcnow_naive() + timedelta(seconds=lease_seconds))
    )
    session.commit()


def prune_runs(session: Session, settings: Settings) -> int:
    cutoff = utcnow_naive() - timedelta(days=settings.backup_run_retention_days)
    result = session.execute(
        delete(BackupRun).where(
            BackupRun.status.not_in(RUN_ACTIVE_STATES), BackupRun.created_at < cutoff
        )
    )
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def finish(session: Session, run: BackupRun, state: str, *, error: str | None = None) -> None:
    run.status = state
    run.error = error
    run.finished_at = utcnow_naive()
    run.lease_owner = None
    run.lease_expires_at = None
    job = session.get(BackupJob, run.job_id) if run.job_id is not None else None
    if job is not None and run.kind == RUN_BACKUP:
        job.last_run_at = run.finished_at
        job.last_status = state
    ok = state in (RUN_SUCCEEDED, RUN_PARTIAL, RUN_CANCELLED)
    audit.record_audit_event(
        session,
        event=audit.BACKUP_RUN_FINISHED if ok else audit.BACKUP_RUN_FAILED,
        category=audit.CATEGORY_BACKUP,
        outcome=audit.OUTCOME_SUCCESS if ok else audit.OUTCOME_FAILURE,
        actor_username=WORKER_NAME,
        target_type="backup_repository",
        target_id=run.repository_id,
        details={
            "run_id": run.id,
            "kind": run.kind,
            "job_id": run.job_id,
            "status": state,
            "dry_run": run.dry_run,
            "snapshot_id": run.snapshot_id,
            "files": run.files,
            "new_blobs": run.new_blobs,
            "reused": run.reused,
            "skipped": run.skipped,
            "errors": run.errors,
            "bytes_processed": run.bytes_processed,
            "bytes_uploaded": run.bytes_uploaded,
            "error": error,
        },
        commit=False,
    )
    if run.kind == RUN_VERIFY and state == RUN_FAILED and run.summary.get("failed_snapshots"):
        audit.record_audit_event(
            session,
            event=audit.BACKUP_VERIFY_FAILED,
            category=audit.CATEGORY_BACKUP,
            outcome=audit.OUTCOME_FAILURE,
            actor_username=WORKER_NAME,
            target_type="backup_repository",
            target_id=run.repository_id,
            details={"run_id": run.id, "failed_snapshots": run.summary["failed_snapshots"]},
            commit=False,
        )
    if run.kind in (RUN_PRUNE, RUN_BACKUP) and run.summary.get("forget") and not run.dry_run:
        audit.record_audit_event(
            session,
            event=audit.BACKUP_SNAPSHOT_DELETED,
            category=audit.CATEGORY_BACKUP,
            actor_username=WORKER_NAME,
            target_type="backup_repository",
            target_id=run.repository_id,
            details={"run_id": run.id, "snapshots": run.summary["forget"]},
            commit=False,
        )
    if run.kind == RUN_RESTORE and run.summary.get("replaced_count") and not run.dry_run:
        audit.record_audit_event(
            session,
            event=audit.BACKUP_RESTORE_REPLACED,
            category=audit.CATEGORY_BACKUP,
            actor_username=WORKER_NAME,
            target_type="backup_repository",
            target_id=run.repository_id,
            details={
                "run_id": run.id,
                "replaced_count": run.summary["replaced_count"],
                "replaced_dir": run.summary.get("replaced_dir"),
            },
            commit=False,
        )
    session.commit()


def process_run(
    session: Session,
    settings: Settings,
    run: BackupRun,
    *,
    cipher: CredentialCipher,
    stop: threading.Event | None = None,
    provider: RemoteProvider | None = None,
    other_provider: RemoteProvider | None = None,
) -> BackupRun:
    """Execute one claimed run to completion (the worker calls this).

    ``provider`` replaces the repository's target and ``other_provider`` the
    pull source or remote restore target (tests inject both).
    """

    def heartbeat() -> bool:
        session.refresh(run, attribute_names=["cancel_requested"])
        if run.cancel_requested or (stop is not None and stop.is_set()):
            return False
        run.lease_expires_at = utcnow_naive() + timedelta(seconds=settings.backup_lease_seconds)
        session.commit()
        return True

    workdir = settings.resolved_backup_staging_dir / f"run-{run.id}"
    try:
        repository = get_repository_or_404(session, run.repository_id)
        target = provider or build_target_provider(session, settings, repository, cipher)
        with target:
            repo = open_repository(session, settings, repository, cipher, target)
            state = _dispatch(
                session,
                settings,
                run,
                repository,
                repo,
                cipher=cipher,
                heartbeat=heartbeat,
                workdir=workdir,
                other_provider=other_provider,
            )
        finish(session, run, state, error=_state_error(run, state))
    except AppError as exc:
        session.rollback()
        finish(session, run, RUN_FAILED, error=exc.message)
    except Exception:
        session.rollback()
        logger.exception("backup run crashed", extra={"run_id": run.id})
        finish(session, run, RUN_FAILED, error="Internal error; see the worker log")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return run


def _state_error(run: BackupRun, state: str) -> str | None:
    if state == RUN_FAILED and run.kind == RUN_VERIFY:
        failed = len(run.summary.get("failed_snapshots") or [])
        return f"Integrity problems found in {failed} snapshot(s)"
    return None


def _dispatch(
    session: Session,
    settings: Settings,
    run: BackupRun,
    repository: BackupRepository,
    repo: Repository,
    *,
    cipher: CredentialCipher,
    heartbeat: engine.Heartbeat,
    workdir: Path,
    other_provider: RemoteProvider | None,
) -> str:
    if run.kind == RUN_VERIFY:
        return engine.run_verify(session, run, repository, repo, heartbeat=heartbeat)
    if run.kind == RUN_PRUNE:
        return engine.run_prune(session, run, repository, repo, heartbeat=heartbeat)
    if run.kind == RUN_RESTORE:
        target = dict(run.params.get("target") or {})
        if target.get("kind") == "remote":
            remote = other_provider or _connection_provider(
                session, settings, cipher, int(target["connection_id"])
            )
            with remote:
                return engine.run_restore(
                    session, settings, run, repo, heartbeat=heartbeat, target_provider=remote
                )
        return engine.run_restore(session, settings, run, repo, heartbeat=heartbeat)
    job = session.get(BackupJob, run.job_id) if run.job_id is not None else None
    if job is None:
        raise NotFoundError("The backup job no longer exists")
    if job.source_kind == SOURCE_REMOTE:
        assert job.source_connection_id is not None  # noqa: S101 - validated on save
        source = other_provider or _connection_provider(
            session, settings, cipher, job.source_connection_id
        )
        with source:
            scan = sources.scan_remote(
                source, job.source_path, job.exclude_patterns, workdir / "pull"
            )
            state = engine.run_backup(
                session, settings, run, job, repository, repo, scan, heartbeat=heartbeat
            )
    else:
        scan = _local_scan(settings, job, workdir)
        state = engine.run_backup(
            session, settings, run, job, repository, repo, scan, heartbeat=heartbeat
        )
    if state in (RUN_SUCCEEDED, RUN_PARTIAL) and job.retention_enabled:
        try:
            engine.run_prune(session, run, repository, repo, heartbeat=heartbeat, job_ids=[job.id])
        except AppError as exc:
            session.rollback()
            run.summary = run.summary | {"prune_error": exc.message}
            session.commit()
    return state


def _local_scan(settings: Settings, job: BackupJob, workdir: Path) -> sources.SourceScan:
    if job.source_kind == SOURCE_SYSTEM:
        return sources.scan_system(settings, workdir / "system")
    if job.source_kind == SOURCE_MEDIA:
        media = settings.resolved_media_dir.resolve(strict=False)
        base = files.resolve_in_root(media, job.source_path)
        return sources.scan_directory(base, job.exclude_patterns, label=f"media:{job.source_path}")
    root = files.resolve_root(settings, job.source_root or "")
    base = files.resolve_in_root(root, job.source_path)
    return sources.scan_directory(
        base, job.exclude_patterns, label=f"{root}/{job.source_path}".rstrip("/")
    )


def _connection_provider(
    session: Session, settings: Settings, cipher: CredentialCipher, connection_id: int
) -> RemoteProvider:
    connection = remotes.get_connection_or_404(session, connection_id)
    return remotes.build_provider(connection, cipher, settings)
