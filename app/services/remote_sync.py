"""Sync jobs and runs (Phase 7): CRUD, queueing, and run processing.

The web process only *queues* runs (``sync_runs`` rows in state
``queued``); the worker process (``python -m app.worker``) claims and runs
them. Claiming is an atomic conditional ``UPDATE`` and a running run holds a
lease that the worker renews after every action, so a crashed worker's run
is reclaimed (marked failed) instead of being run twice concurrently.

Changing a job into anything that can move or overwrite existing data
(enabling it, two-way sync, archiving deletions, ``prefer_*`` conflict
policies) requires ``confirm: true``; without it the API answers 400 with
the list of risks so the UI can show them first.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError, RemoteError
from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.models import RemoteConnection, SyncConflict, SyncJob, SyncRun, User
from app.models.remote import (
    CONFLICT_KEEP_BOTH,
    CONFLICT_POLICIES,
    DELETE_ARCHIVE,
    DELETE_IGNORE,
    DELETE_POLICIES,
    DIRECTION_BIDIRECTIONAL,
    DIRECTIONS,
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
from app.services import audit, files, remotes, settings_service
from app.services.remote.base import RemoteProvider, WalkResult, normalize_remote_path
from app.services.sync_engine import (
    OP_ARCHIVE_LOCAL,
    OP_ARCHIVE_REMOTE,
    OP_DOWNLOAD,
    OP_RESOLVE,
    OP_SKIP,
    OP_UPLOAD,
    DeletionGuardError,
    SyncExecutor,
    baseline_view,
    check_deletion_guard,
    is_excluded,
    load_baseline,
    plan,
    scan_local,
    summarize,
)

logger = logging.getLogger(__name__)

WORKER_HEARTBEAT_KEY = "worker.heartbeat"
_MAX_EXCLUDES = 50
_PATTERN_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_SCHEDULE_MAX_MINUTES = 60 * 24 * 31


class ConfirmationRequiredError(BadRequestError):
    code = "confirmation_required"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def list_jobs(session: Session) -> list[SyncJob]:
    return list(session.execute(select(SyncJob).order_by(SyncJob.name)).scalars())


def get_job_or_404(session: Session, job_id: int) -> SyncJob:
    row = session.get(SyncJob, job_id)
    if row is None:
        raise NotFoundError("Sync job not found", details={"id": job_id})
    return row


def active_run(session: Session, job_id: int) -> SyncRun | None:
    return session.execute(
        select(SyncRun).where(SyncRun.job_id == job_id, SyncRun.status.in_(RUN_ACTIVE_STATES))
    ).scalar_one_or_none()


def _clean_name(name: str) -> str:
    value = name.strip()
    if not value or len(value) > 64 or any(ord(c) < 32 for c in value):
        raise BadRequestError("Job name must be 1-64 printable characters")
    return value


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


def _clean_local(settings: Settings, root_str: str, rel_path: str) -> tuple[str, str]:
    root = files.resolve_root(settings, root_str)
    target = files.resolve_in_root(root, rel_path)
    if not target.is_dir():
        raise BadRequestError(
            "Local path must be an existing directory", details={"path": rel_path}
        )
    rel = target.relative_to(root).as_posix()
    return str(root), "" if rel == "." else rel


def risks(job: SyncJob, *, was_enabled: bool) -> list[str]:
    """Human-readable consequences that require explicit confirmation."""
    found: list[str] = []
    if job.enabled and not was_enabled:
        found.append(
            "The job will run on its schedule without further confirmation."
            if job.schedule_minutes
            else "The job is marked enabled (it has no schedule; runs stay manual)."
        )
    if job.direction == DIRECTION_BIDIRECTIONAL:
        found.append(
            "Two-way sync writes to both sides: local files can be replaced by remote versions."
        )
    if job.delete_policy == DELETE_ARCHIVE:
        found.append(
            "Files deleted on one side will be moved into .nas-sync-archive/ on the other side."
        )
    if job.conflict_policy != CONFLICT_KEEP_BOTH:
        found.append(
            "Conflicts overwrite the losing version (it is moved into .nas-sync-archive/ first)."
        )
    return found


def _require_confirmation(found: list[str], confirm: bool) -> None:
    if found and not confirm:
        raise ConfirmationRequiredError(
            "This change needs confirmation; repeat it with confirm=true",
            details={"warnings": found},
        )


def _validate_enums(direction: str, conflict_policy: str, delete_policy: str) -> None:
    if direction not in DIRECTIONS:
        raise BadRequestError("Unknown direction", details={"allowed": list(DIRECTIONS)})
    if conflict_policy not in CONFLICT_POLICIES:
        raise BadRequestError(
            "Unknown conflict policy", details={"allowed": list(CONFLICT_POLICIES)}
        )
    if delete_policy not in DELETE_POLICIES:
        raise BadRequestError("Unknown delete policy", details={"allowed": list(DELETE_POLICIES)})


def _schedule(value: int | None) -> int | None:
    if value is None:
        return None
    if not 1 <= value <= _SCHEDULE_MAX_MINUTES:
        raise BadRequestError("schedule_minutes must be between 1 and 44640 (31 days)")
    return value


def _ensure_unique(session: Session, name: str, *, exclude_id: int | None = None) -> None:
    stmt = select(SyncJob.id).where(func.lower(SyncJob.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(SyncJob.id != exclude_id)
    if session.execute(stmt).first() is not None:
        raise ConflictError("A sync job with that name already exists")


def _job_details(job: SyncJob) -> dict[str, Any]:
    return {
        "name": job.name,
        "connection_id": job.connection_id,
        "local_root": job.local_root,
        "local_path": job.local_path,
        "remote_path": job.remote_path,
        "direction": job.direction,
        "conflict_policy": job.conflict_policy,
        "delete_policy": job.delete_policy,
        "exclude_patterns": job.exclude_patterns,
        "schedule_minutes": job.schedule_minutes,
        "enabled": job.enabled,
    }


def create_job(
    session: Session,
    settings: Settings,
    *,
    values: dict[str, Any],
    confirm: bool,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> SyncJob:
    name = _clean_name(str(values["name"]))
    _ensure_unique(session, name)
    remotes.get_connection_or_404(session, int(values["connection_id"]))
    root, local_path = _clean_local(
        settings, str(values["local_root"]), values.get("local_path", "")
    )
    direction = str(values["direction"])
    conflict_policy = str(values.get("conflict_policy") or CONFLICT_KEEP_BOTH)
    delete_policy = str(values.get("delete_policy") or DELETE_IGNORE)
    _validate_enums(direction, conflict_policy, delete_policy)
    job = SyncJob(
        name=name,
        connection_id=int(values["connection_id"]),
        local_root=root,
        local_path=local_path,
        remote_path=normalize_remote_path(str(values.get("remote_path") or "")),
        direction=direction,
        conflict_policy=conflict_policy,
        delete_policy=delete_policy,
        schedule_minutes=_schedule(values.get("schedule_minutes")),
        enabled=bool(values.get("enabled", False)),
        created_by=actor.id,
    )
    job.exclude_patterns = _clean_excludes(list(values.get("exclude_patterns") or []))
    _require_confirmation(risks(job, was_enabled=False), confirm)
    _reschedule(job, utcnow_naive())
    session.add(job)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.SYNC_JOB_CREATED,
        category=audit.CATEGORY_REMOTE_SYNC,
        actor=actor,
        target_type="sync_job",
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
    job: SyncJob,
    *,
    values: dict[str, Any],
    confirm: bool,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> SyncJob:
    """Apply a partial update. Changing what is synced (connection, paths,
    direction) is refused while a run is active and resets the baseline."""
    before = _job_details(job)
    was_enabled = job.enabled
    previous_risks = set(risks(job, was_enabled=True))
    if "name" in values and values["name"] is not None:
        name = _clean_name(str(values["name"]))
        _ensure_unique(session, name, exclude_id=job.id)
        job.name = name
    if values.get("connection_id") is not None:
        remotes.get_connection_or_404(session, int(values["connection_id"]))
        job.connection_id = int(values["connection_id"])
    if values.get("local_root") is not None or values.get("local_path") is not None:
        job.local_root, job.local_path = _clean_local(
            settings,
            str(values.get("local_root") or job.local_root),
            str(values["local_path"] if values.get("local_path") is not None else job.local_path),
        )
    if values.get("remote_path") is not None:
        job.remote_path = normalize_remote_path(str(values["remote_path"]))
    for key in ("direction", "conflict_policy", "delete_policy"):
        if values.get(key) is not None:
            setattr(job, key, str(values[key]))
    _validate_enums(job.direction, job.conflict_policy, job.delete_policy)
    if "exclude_patterns" in values and values["exclude_patterns"] is not None:
        job.exclude_patterns = _clean_excludes(list(values["exclude_patterns"]))
    if "schedule_minutes" in values:
        job.schedule_minutes = _schedule(values["schedule_minutes"])
    if values.get("enabled") is not None:
        job.enabled = bool(values["enabled"])

    after = _job_details(job)
    changed = sorted(key for key in after if after[key] != before[key])
    new_risks = [r for r in risks(job, was_enabled=was_enabled) if r not in previous_risks]
    _require_confirmation(new_risks, confirm)

    target_changed = {"connection_id", "local_root", "local_path", "remote_path"} & set(changed)
    if (target_changed or "direction" in changed) and active_run(session, job.id) is not None:
        raise ConflictError("The job has an active run; wait for it or cancel it first")
    if target_changed:
        # The baseline describes the old pair of directories; keeping it
        # would make files look deleted on one side.
        reset_baseline(session, job.id)
    if {"enabled", "schedule_minutes"} & set(changed):
        _reschedule(job, utcnow_naive())
    audit.record_audit_event(
        session,
        event=audit.SYNC_JOB_UPDATED,
        category=audit.CATEGORY_REMOTE_SYNC,
        actor=actor,
        target_type="sync_job",
        target_id=job.id,
        details={
            "changed": {key: after[key] for key in changed},
            "confirmed": confirm,
            "baseline_reset": bool(target_changed),
        },
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return job


def reset_baseline(session: Session, job_id: int) -> None:
    from app.models import SyncJobEntry

    session.execute(delete(SyncJobEntry).where(SyncJobEntry.job_id == job_id))


def delete_job(
    session: Session,
    job: SyncJob,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    """Delete the job definition, its baseline and history. Never touches files."""
    if active_run(session, job.id) is not None:
        raise ConflictError("The job has an active run; cancel it first")
    audit.record_audit_event(
        session,
        event=audit.SYNC_JOB_DELETED,
        category=audit.CATEGORY_REMOTE_SYNC,
        actor=actor,
        target_type="sync_job",
        target_id=job.id,
        details=_job_details(job),
        **audit.request_context(context),
        commit=False,
    )
    session.delete(job)
    session.commit()


def _reschedule(job: SyncJob, now: datetime) -> None:
    job.next_run_at = now if job.enabled and job.schedule_minutes else None


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def queue_run(
    session: Session,
    job: SyncJob,
    *,
    dry_run: bool,
    override_deletion_guard: bool,
    confirm: bool,
    actor: User | None,
    trigger: str = TRIGGER_MANUAL,
    context: dict[str, str | None] | None = None,
) -> SyncRun:
    if active_run(session, job.id) is not None:
        raise ConflictError("The job already has a queued or running run")
    if override_deletion_guard and not dry_run and not confirm:
        raise ConfirmationRequiredError(
            "Overriding the deletion guard needs confirm=true",
            details={"warnings": ["Every planned archive will be carried out, however many."]},
        )
    run = SyncRun(
        job_id=job.id,
        status=RUN_QUEUED,
        trigger=trigger,
        dry_run=dry_run,
        requested_by=actor.id if actor is not None else None,
    )
    run.summary = {"override_deletion_guard": bool(override_deletion_guard and not dry_run)}
    session.add(run)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.SYNC_RUN_QUEUED,
        category=audit.CATEGORY_REMOTE_SYNC,
        actor=actor,
        actor_username=None if actor is not None else "sync-worker",
        target_type="sync_job",
        target_id=job.id,
        details={
            "run_id": run.id,
            "dry_run": dry_run,
            "trigger": trigger,
            "override_deletion_guard": run.summary["override_deletion_guard"],
        },
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return run


def list_runs(session: Session, *, job_id: int, limit: int) -> list[SyncRun]:
    return list(
        session.execute(
            select(SyncRun).where(SyncRun.job_id == job_id).order_by(SyncRun.id.desc()).limit(limit)
        ).scalars()
    )


def get_run_or_404(session: Session, run_id: int) -> SyncRun:
    row = session.get(SyncRun, run_id)
    if row is None:
        raise NotFoundError("Sync run not found", details={"id": run_id})
    return row


def cancel_run(
    session: Session,
    run: SyncRun,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> SyncRun:
    """Queued runs are cancelled at once; running ones stop after the
    current file (the worker checks the flag between actions)."""
    if not run.is_active:
        raise ConflictError("The run has already finished", details={"status": run.status})
    if run.status == RUN_QUEUED:
        claimed = session.execute(
            update(SyncRun)
            .where(SyncRun.id == run.id, SyncRun.status == RUN_QUEUED)
            .values(status=RUN_CANCELLED, finished_at=utcnow_naive())
        )
        if claimed.rowcount:  # type: ignore[attr-defined]
            session.expire(run)
    if run.status != RUN_CANCELLED:
        run.cancel_requested = True
    audit.record_audit_event(
        session,
        event=audit.SYNC_RUN_CANCEL_REQUESTED,
        category=audit.CATEGORY_REMOTE_SYNC,
        actor=actor,
        target_type="sync_job",
        target_id=run.job_id,
        details={"run_id": run.id},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    session.refresh(run)
    return run


def list_conflicts(
    session: Session, *, job_id: int, include_acknowledged: bool, limit: int
) -> list[SyncConflict]:
    stmt = select(SyncConflict).where(SyncConflict.job_id == job_id)
    if not include_acknowledged:
        stmt = stmt.where(SyncConflict.acknowledged.is_(False))
    return list(session.execute(stmt.order_by(SyncConflict.id.desc()).limit(limit)).scalars())


def acknowledge_conflict(
    session: Session,
    conflict_id: int,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> SyncConflict:
    row = session.get(SyncConflict, conflict_id)
    if row is None:
        raise NotFoundError("Conflict not found", details={"id": conflict_id})
    row.acknowledged = True
    audit.record_audit_event(
        session,
        event=audit.SYNC_CONFLICT_ACKNOWLEDGED,
        category=audit.CATEGORY_REMOTE_SYNC,
        actor=actor,
        target_type="sync_job",
        target_id=row.job_id,
        details={"conflict_id": row.id, "path": row.path},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def worker_status(session: Session, settings: Settings) -> dict[str, Any]:
    raw = settings_service.get_setting(session, WORKER_HEARTBEAT_KEY)
    heartbeat = datetime.fromisoformat(raw) if raw else None
    age = (utcnow_naive() - heartbeat).total_seconds() if heartbeat else None
    counts = dict(
        session.execute(
            select(SyncRun.status, func.count(SyncRun.id))
            .where(SyncRun.status.in_(RUN_ACTIVE_STATES))
            .group_by(SyncRun.status)
        ).all()
    )
    return {
        "worker_heartbeat_at": heartbeat,
        "worker_heartbeat_age_seconds": age,
        "worker_online": age is not None and age <= 3 * settings.remote_sync_poll_seconds + 30,
        "queued_runs": int(counts.get(RUN_QUEUED, 0)),
        "running_runs": int(counts.get(RUN_RUNNING, 0)),
    }


# ---------------------------------------------------------------------------
# Worker primitives (used by app/worker/runner.py)
# ---------------------------------------------------------------------------


def write_heartbeat(session: Session) -> None:
    settings_service.set_setting(session, WORKER_HEARTBEAT_KEY, utcnow_naive().isoformat())


def enqueue_due_jobs(session: Session, now: datetime) -> list[SyncRun]:
    """Queue a scheduled run for every enabled job that is due and idle."""
    queued: list[SyncRun] = []
    due = session.execute(
        select(SyncJob).where(
            SyncJob.enabled.is_(True),
            SyncJob.schedule_minutes.is_not(None),
            SyncJob.next_run_at.is_not(None),
            SyncJob.next_run_at <= now,
        )
    ).scalars()
    for job in list(due):
        assert job.schedule_minutes is not None  # noqa: S101 - filtered above
        job.next_run_at = now + timedelta(minutes=job.schedule_minutes)
        if active_run(session, job.id) is not None:
            session.commit()
            continue
        queued.append(
            queue_run(
                session,
                job,
                dry_run=False,
                override_deletion_guard=False,
                confirm=False,
                actor=None,
                trigger=TRIGGER_SCHEDULE,
            )
        )
    return queued


def reclaim_expired(session: Session, now: datetime) -> int:
    """Fail runs whose worker stopped renewing its lease."""
    stale = list(
        session.execute(
            select(SyncRun).where(
                SyncRun.status == RUN_RUNNING,
                SyncRun.lease_expires_at.is_not(None),
                SyncRun.lease_expires_at < now,
            )
        ).scalars()
    )
    for run in stale:
        _finish(session, run, RUN_FAILED, error="The worker stopped while running this job")
    return len(stale)


def claim_next(
    session: Session, *, owner: str, lease_seconds: int, busy_job_ids: set[int]
) -> SyncRun | None:
    """Atomically move the oldest queued run to ``running`` for ``owner``."""
    candidates = session.execute(
        select(SyncRun.id, SyncRun.job_id)
        .where(SyncRun.status == RUN_QUEUED)
        .order_by(SyncRun.id)
        .limit(20)
    ).all()
    for run_id, job_id in candidates:
        if job_id in busy_job_ids:
            continue
        now = utcnow_naive()
        result = session.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id, SyncRun.status == RUN_QUEUED)
            .values(
                status=RUN_RUNNING,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                started_at=now,
            )
        )
        session.commit()
        if result.rowcount == 1:  # type: ignore[attr-defined]
            return session.get(SyncRun, run_id, populate_existing=True)
    return None


def renew_leases(session: Session, *, owner: str, lease_seconds: int) -> None:
    """Extend every lease ``owner`` holds. The worker's main loop calls this
    each tick, so a single long transfer never lets its lease lapse; leases
    only expire when the worker process itself is gone."""
    session.execute(
        update(SyncRun)
        .where(SyncRun.status == RUN_RUNNING, SyncRun.lease_owner == owner)
        .values(lease_expires_at=utcnow_naive() + timedelta(seconds=lease_seconds))
    )
    session.commit()


def prune_runs(session: Session, settings: Settings) -> int:
    cutoff = utcnow_naive() - timedelta(days=settings.remote_sync_run_retention_days)
    result = session.execute(
        delete(SyncRun).where(SyncRun.status.not_in(RUN_ACTIVE_STATES), SyncRun.created_at < cutoff)
    )
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def _finish(session: Session, run: SyncRun, status: str, *, error: str | None = None) -> None:
    run.status = status
    run.error = error
    run.finished_at = utcnow_naive()
    run.lease_owner = None
    run.lease_expires_at = None
    job = session.get(SyncJob, run.job_id)
    if job is not None and not run.dry_run:
        job.last_run_at = run.finished_at
        job.last_status = status
    ok = status in (RUN_SUCCEEDED, RUN_PARTIAL, RUN_CANCELLED)
    audit.record_audit_event(
        session,
        event=audit.SYNC_RUN_FINISHED if ok else audit.SYNC_RUN_FAILED,
        category=audit.CATEGORY_REMOTE_SYNC,
        outcome=audit.OUTCOME_SUCCESS if ok else audit.OUTCOME_FAILURE,
        actor_username="sync-worker",
        target_type="sync_job",
        target_id=run.job_id,
        details={
            "run_id": run.id,
            "status": status,
            "dry_run": run.dry_run,
            "uploaded": run.uploaded,
            "downloaded": run.downloaded,
            "conflicts": run.conflicts,
            "archived": run.archived,
            "skipped": run.skipped,
            "errors": run.errors,
            "bytes_transferred": run.bytes_transferred,
            "error": error,
        },
        commit=False,
    )
    session.commit()


def process_run(
    session: Session,
    settings: Settings,
    run: SyncRun,
    *,
    cipher: CredentialCipher,
    stop: threading.Event | None = None,
    provider: RemoteProvider | None = None,
) -> SyncRun:
    """Run one claimed run to completion (the worker calls this per run).

    ``provider`` lets tests inject a provider; normally it is built from the
    job's connection.
    """
    job = session.get(SyncJob, run.job_id)
    if job is None:  # pragma: no cover - cascade deletes runs with their job
        _finish(session, run, RUN_FAILED, error="The job no longer exists")
        return run

    def heartbeat() -> bool:
        session.refresh(run, attribute_names=["cancel_requested"])
        if run.cancel_requested or (stop is not None and stop.is_set()):
            return False
        run.lease_expires_at = utcnow_naive() + timedelta(
            seconds=settings.remote_sync_lease_seconds
        )
        session.commit()
        return True

    summary = run.summary
    override = bool(summary.get("override_deletion_guard"))
    try:
        root = files.resolve_root(settings, job.local_root)
        local_base = files.resolve_in_root(root, job.local_path)
        if not local_base.is_dir():
            raise BadRequestError("The job's local directory no longer exists")
        connection = session.get(RemoteConnection, job.connection_id)
        if connection is None:  # pragma: no cover - FK RESTRICT
            raise NotFoundError("The job's remote connection no longer exists")
        remote_provider = provider or remotes.build_provider(connection, cipher, settings)
        with remote_provider:
            status = _sync(
                session,
                settings,
                job,
                run,
                remote_provider,
                root=root,
                local_base=local_base,
                override=override,
                heartbeat=heartbeat,
            )
        _finish(session, run, status)
    except DeletionGuardError as exc:
        session.rollback()
        audit.record_audit_event(
            session,
            event=audit.SYNC_DELETION_GUARD_TRIPPED,
            category=audit.CATEGORY_REMOTE_SYNC,
            outcome=audit.OUTCOME_FAILURE,
            actor_username="sync-worker",
            target_type="sync_job",
            target_id=job.id,
            details={"run_id": run.id, **(exc.details or {})},
            commit=False,
        )
        _finish(session, run, RUN_FAILED, error=exc.message)
    except AppError as exc:
        session.rollback()
        _finish(session, run, RUN_FAILED, error=exc.message)
    except Exception:
        session.rollback()
        logger.exception("sync run crashed", extra={"run_id": run.id, "job_id": job.id})
        _finish(session, run, RUN_FAILED, error="Internal error; see the worker log")
    return run


def _sync(
    session: Session,
    settings: Settings,
    job: SyncJob,
    run: SyncRun,
    provider: RemoteProvider,
    *,
    root: Path,
    local_base: Path,
    override: bool,
    heartbeat: Any,
) -> str:
    excludes = job.exclude_patterns
    local, local_rejected = scan_local(local_base, excludes)
    rows = load_baseline(session, job.id)
    if provider.dir_exists(job.remote_path):
        walked = provider.walk(job.remote_path)
    elif rows:
        # Files were synced to this folder before. A missing folder is far
        # more likely an unmounted or renamed remote than a deliberate wipe,
        # so never treat it as "everything was deleted".
        raise RemoteError(
            "The remote folder no longer exists; check the connection and remote path",
            details={"remote_path": job.remote_path},
        )
    else:
        walked = WalkResult()  # first sync into a new folder: created on upload
    remote = {path: e for path, e in walked.files.items() if not is_excluded(path, excludes)}
    the_plan = plan(
        local,
        remote,
        baseline_view(rows),
        direction=job.direction,
        delete_policy=job.delete_policy,
    )
    limit = settings.remote_sync_preview_limit
    rejected = [
        {"side": "local", "path": path, "reason": reason} for path, reason in local_rejected
    ] + [{"side": "remote", "path": path, "reason": reason} for path, reason in walked.rejected]
    summary = summarize(the_plan, limit) | {
        "rejected": rejected[:limit],
        "override_deletion_guard": override,
        "local_files": len(local),
        "remote_files": len(remote),
    }
    run.summary = summary
    session.commit()
    if not override:
        check_deletion_guard(
            the_plan,
            max_count=settings.remote_sync_max_delete_count,
            max_percent=settings.remote_sync_max_delete_percent,
        )
    if run.dry_run:
        run.uploaded = the_plan.count(OP_UPLOAD)
        run.downloaded = the_plan.count(OP_DOWNLOAD)
        run.conflicts = the_plan.count(OP_RESOLVE)
        run.archived = the_plan.count(OP_ARCHIVE_LOCAL) + the_plan.count(OP_ARCHIVE_REMOTE)
        run.skipped = the_plan.count(OP_SKIP) + len(rejected)
        return RUN_SUCCEEDED

    executor = SyncExecutor(
        session,
        settings,
        job=job,
        run=run,
        provider=provider,
        root=root,
        local_base=local_base,
        local=local,
        remote=remote,
        baseline_rows=rows,
        heartbeat=heartbeat,
    )
    executor.counters.skipped = len(rejected)
    try:
        executor.execute(the_plan)
    finally:
        executor.flush_counters()
        run.summary = run.summary | {"problems": executor.counters.problems}
        session.commit()
    if executor.cancelled:
        return RUN_CANCELLED
    return RUN_PARTIAL if executor.counters.errors else RUN_SUCCEEDED
