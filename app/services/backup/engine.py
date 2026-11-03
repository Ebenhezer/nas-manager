"""Backup, verify, prune and restore runs (Phase 11). Executed by the worker.

Every loop calls ``heartbeat()`` between files; it extends the run's lease
and returns False when the run was cancelled or the worker is stopping. A
cancelled backup writes no snapshot (the blobs it already uploaded are
reused by the next run or garbage-collected).

Safety rules:

* Retention only removes snapshots of jobs that have retention configured,
  and garbage collection refuses to delete any blob unless every snapshot
  in the repository could be read (an unreadable snapshot might still
  reference it).
* A restore never deletes anything. Existing files are skipped unless the
  run was confirmed with ``overwrite``, and then they are moved into
  ``.nas-backup-replaced/<timestamp>/`` first. Every restored file is
  written to a temporary name, checked against its SHA-256 and renamed into
  place.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app import __version__
from app.core.config import Settings
from app.core.errors import BadRequestError, RemoteError
from app.db.base import utcnow_naive
from app.models import BackupBlob, BackupJob, BackupRepository, BackupRun, BackupSnapshot
from app.models.backup import VERIFY_FAILED, VERIFY_OK
from app.models.remote import RUN_CANCELLED, RUN_FAILED, RUN_PARTIAL, RUN_SUCCEEDED
from app.services import files
from app.services.backup import crypto
from app.services.backup.repository import Repository, new_snapshot_id, snapshot_time
from app.services.backup.sources import SourceScan
from app.services.remote.base import RemoteProvider, join, normalize_remote_path, parent_of

Heartbeat = Callable[[], bool]

REPLACED_DIR = ".nas-backup-replaced"
RESTORE_MARKER = "restore.json"
_MAX_PROBLEMS = 100


class Problems:
    """Per-file problems of a run (capped) and the error counter."""

    def __init__(self, run: BackupRun) -> None:
        self.run = run
        self.items: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self.run.errors += 1
        if len(self.items) < _MAX_PROBLEMS:
            self.items.append({"path": path, "error": message})


def _merge_summary(run: BackupRun, values: dict[str, Any]) -> None:
    run.summary = run.summary | values


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def _parent_entries(
    session: Session, repo: Repository, repository_id: int, job: BackupJob
) -> dict[str, dict[str, Any]]:
    """Entries of the job's latest snapshot, to skip unchanged files."""
    row = session.execute(
        select(BackupSnapshot)
        .where(BackupSnapshot.repository_id == repository_id, BackupSnapshot.job_id == job.id)
        .order_by(BackupSnapshot.taken_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return {}
    try:
        manifest = repo.read_snapshot(row.snapshot_id)
    except RemoteError:
        return {}  # fall back to re-reading every file
    return {str(e["path"]): e for e in manifest["entries"] if isinstance(e, dict)}


def run_backup(
    session: Session,
    settings: Settings,
    run: BackupRun,
    job: BackupJob,
    repository: BackupRepository,
    repo: Repository,
    scan: SourceScan,
    *,
    heartbeat: Heartbeat,
) -> str:
    parent = _parent_entries(session, repo, repository.id, job)
    known = set(
        session.execute(
            select(BackupBlob.blob).where(BackupBlob.repository_id == repository.id)
        ).scalars()
    )
    problems = Problems(run)
    entries: list[dict[str, Any]] = []
    run.skipped = len(scan.rejected)
    for item in scan.files:
        if not heartbeat():
            _merge_summary(run, {"problems": problems.items})
            return RUN_CANCELLED
        previous = parent.get(item.path)
        if (
            previous is not None
            and item.mtime_ns
            and previous.get("size") == item.size
            and previous.get("mtime_ns") == item.mtime_ns
            and previous.get("blob") in known
        ):
            entries.append(previous | {"mode": item.mode})
            run.files += 1
            run.reused += 1
            run.bytes_processed += item.size
            continue
        with repo.temp_file() as encrypted:
            try:
                with item.open() as handle:
                    sha256, size = crypto.encrypt_stream(handle, encrypted, repo.keys)
            except (OSError, RemoteError) as exc:
                problems.add(item.path, f"Could not read the file: {exc}")
                continue
            blob = repo.keys.blob_name(sha256)
            if blob not in known:
                stored = encrypted.seek(0, os.SEEK_END)
                if not repo.has_blob(blob):
                    repo.put_blob(blob, encrypted)
                    run.new_blobs += 1
                    run.bytes_uploaded += stored
                session.add(BackupBlob(repository_id=repository.id, blob=blob, size=stored))
                known.add(blob)
        entries.append(
            {
                "path": item.path,
                "size": size,
                "mtime_ns": item.mtime_ns,
                "mode": item.mode,
                "sha256": sha256,
                "blob": blob,
            }
        )
        run.files += 1
        run.bytes_processed += size

    taken = utcnow_naive()
    snapshot_id = new_snapshot_id(taken)
    rejected = [{"path": path, "reason": reason} for path, reason in scan.rejected]
    manifest = {
        "snapshot_id": snapshot_id,
        "created_at": taken.isoformat() + "Z",
        "host": socket.gethostname(),
        "app_version": __version__,
        "job": {"id": job.id, "name": job.name},
        "source": {
            "kind": job.source_kind,
            "label": scan.label,
            "root": job.source_root,
            "path": job.source_path,
        },
        "entries": entries,
        "rejected": rejected[:_MAX_PROBLEMS],
        "problems": problems.items,
    }
    repo.write_snapshot(snapshot_id, manifest)
    session.add(
        BackupSnapshot(
            repository_id=repository.id,
            snapshot_id=snapshot_id,
            job_id=job.id,
            job_name=job.name,
            source_kind=job.source_kind,
            source_label=scan.label,
            taken_at=snapshot_time(snapshot_id).replace(tzinfo=None),
            file_count=len(entries),
            total_bytes=sum(int(e["size"]) for e in entries),
        )
    )
    run.snapshot_id = snapshot_id
    _merge_summary(
        run,
        {"problems": problems.items, "rejected": rejected[: settings.remote_sync_preview_limit]},
    )
    session.commit()
    return RUN_PARTIAL if run.errors else RUN_SUCCEEDED


# ---------------------------------------------------------------------------
# Retention and garbage collection
# ---------------------------------------------------------------------------


def select_retained(
    snapshots: Iterable[tuple[str, datetime]],
    *,
    keep_last: int,
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
) -> set[str]:
    """Snapshot ids a keep-last / daily / weekly / monthly policy retains.

    Each bucket rule keeps the newest snapshot of each of the most recent N
    days, ISO weeks or months that have one. The newest snapshot is always
    kept.
    """
    ordered = sorted(snapshots, key=lambda item: (item[1], item[0]), reverse=True)
    keep = {sid for sid, _ in ordered[: max(keep_last, 0)]}

    def by_day(t: datetime) -> tuple[int, ...]:
        return (t.year, t.month, t.day)

    def by_week(t: datetime) -> tuple[int, ...]:
        iso = t.isocalendar()
        return (iso.year, iso.week)

    def by_month(t: datetime) -> tuple[int, ...]:
        return (t.year, t.month)

    rules: list[tuple[int, Callable[[datetime], tuple[int, ...]]]] = [
        (keep_daily, by_day),
        (keep_weekly, by_week),
        (keep_monthly, by_month),
    ]
    for count, bucket in rules:
        if count <= 0:
            continue
        seen: set[tuple[int, ...]] = set()
        for sid, taken in ordered:
            key = bucket(taken)
            if key in seen:
                continue
            seen.add(key)
            keep.add(sid)
            if len(seen) >= count:
                break
    if ordered:
        keep.add(ordered[0][0])
    return keep


def plan_retention(
    session: Session, repository_id: int, *, job_ids: list[int] | None = None
) -> list[BackupSnapshot]:
    """Catalogue rows that the jobs' retention policies no longer keep."""
    stmt = select(BackupJob).where(BackupJob.repository_id == repository_id)
    if job_ids is not None:
        stmt = stmt.where(BackupJob.id.in_(job_ids))
    forget: list[BackupSnapshot] = []
    for job in session.execute(stmt.order_by(BackupJob.id)).scalars():
        if not job.retention_enabled:
            continue
        rows = list(
            session.execute(
                select(BackupSnapshot).where(
                    BackupSnapshot.repository_id == repository_id,
                    BackupSnapshot.job_id == job.id,
                )
            ).scalars()
        )
        keep = select_retained(
            [(row.snapshot_id, row.taken_at) for row in rows],
            keep_last=job.keep_last,
            keep_daily=job.keep_daily,
            keep_weekly=job.keep_weekly,
            keep_monthly=job.keep_monthly,
        )
        forget.extend(row for row in rows if row.snapshot_id not in keep)
    return sorted(forget, key=lambda row: row.taken_at)


def run_prune(
    session: Session,
    run: BackupRun,
    repository: BackupRepository,
    repo: Repository,
    *,
    heartbeat: Heartbeat,
    job_ids: list[int] | None = None,
) -> str:
    forget = plan_retention(session, repository.id, job_ids=job_ids)
    forgotten = [row.snapshot_id for row in forget]
    _merge_summary(run, {"forget": forgotten})
    if not run.dry_run:
        for row in forget:
            if not heartbeat():
                return RUN_CANCELLED
            repo.delete_snapshot(row.snapshot_id)
            session.delete(row)
            session.commit()
    gc = collect_garbage(
        session,
        run,
        repository,
        repo,
        heartbeat=heartbeat,
        ignore=set(forgotten) if run.dry_run else set(),
    )
    if gc is None:
        return RUN_CANCELLED
    _merge_summary(run, gc)
    session.commit()
    return RUN_SUCCEEDED


def collect_garbage(
    session: Session,
    run: BackupRun,
    repository: BackupRepository,
    repo: Repository,
    *,
    heartbeat: Heartbeat,
    ignore: set[str],
) -> dict[str, int] | None:
    """Delete blobs no snapshot references (counted only on a dry run).

    Returns None when cancelled. Raises RemoteError, deleting nothing, when
    any snapshot cannot be read.
    """
    referenced: set[str] = set()
    for snapshot_id in repo.list_snapshot_ids():
        if snapshot_id in ignore:
            continue
        if not heartbeat():
            return None
        try:
            manifest = repo.read_snapshot(snapshot_id)
        except RemoteError as exc:
            raise RemoteError(
                "Garbage collection refused: a snapshot could not be read, so no blob is "
                f"provably unused ({exc.message})",
                details={"snapshot_id": snapshot_id},
            ) from exc
        referenced.update(str(e.get("blob")) for e in manifest["entries"] if isinstance(e, dict))
    existing = repo.list_blobs()
    unused = sorted(set(existing) - referenced)
    freed = sum(existing[name] for name in unused)
    if not run.dry_run:
        for index, name in enumerate(unused):
            if index % 50 == 0 and not heartbeat():
                return None
            repo.delete_blob(name)
            session.execute(
                delete(BackupBlob).where(
                    BackupBlob.repository_id == repository.id, BackupBlob.blob == name
                )
            )
        session.commit()
    return {
        "blobs_total": len(existing),
        "blobs_unreferenced": len(unused),
        "bytes_unreferenced": freed,
    }


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def sync_catalogue(
    session: Session, repository: BackupRepository, repo: Repository
) -> tuple[set[str], list[str]]:
    """Add catalogue rows for snapshots found on the target (e.g. after a
    database restore or when adopting an existing repository).

    Returns the snapshot ids on the target and the ids that could not be read.
    """
    on_target = set(repo.list_snapshot_ids())
    known = set(
        session.execute(
            select(BackupSnapshot.snapshot_id).where(BackupSnapshot.repository_id == repository.id)
        ).scalars()
    )
    unreadable: list[str] = []
    for snapshot_id in sorted(on_target - known):
        try:
            manifest = repo.read_snapshot(snapshot_id)
        except RemoteError:
            unreadable.append(snapshot_id)
            continue
        job_info: dict[str, Any] = _as_dict(manifest.get("job"))
        job_name = str(job_info.get("name") or "") or None
        job = (
            session.execute(
                select(BackupJob).where(
                    BackupJob.name == job_name, BackupJob.repository_id == repository.id
                )
            ).scalar_one_or_none()
            if job_name
            else None
        )
        source = _as_dict(manifest.get("source"))
        entries = [e for e in manifest["entries"] if isinstance(e, dict)]
        session.add(
            BackupSnapshot(
                repository_id=repository.id,
                snapshot_id=snapshot_id,
                job_id=job.id if job is not None else None,
                job_name=job_name,
                source_kind=str(source.get("kind") or "unknown")[:16],
                source_label=str(source.get("label") or ""),
                taken_at=snapshot_time(snapshot_id).replace(tzinfo=None),
                file_count=len(entries),
                total_bytes=sum(int(e.get("size") or 0) for e in entries),
            )
        )
    session.commit()
    return on_target, unreadable


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def run_verify(
    session: Session,
    run: BackupRun,
    repository: BackupRepository,
    repo: Repository,
    *,
    heartbeat: Heartbeat,
) -> str:
    """Check snapshots against the stored blobs.

    ``params.full`` decrypts every referenced blob and compares its SHA-256;
    otherwise only presence is checked. The blob cache is rebuilt from the
    target and missing snapshots are added to the catalogue.
    """
    params = run.params
    full = bool(params.get("full", True))
    only = params.get("snapshot_id")
    on_target, unreadable = sync_catalogue(session, repository, repo)
    blobs = repo.list_blobs()
    session.execute(delete(BackupBlob).where(BackupBlob.repository_id == repository.id))
    if blobs:
        session.execute(
            insert(BackupBlob),
            [{"repository_id": repository.id, "blob": n, "size": s} for n, s in blobs.items()],
        )
    session.commit()

    stmt = select(BackupSnapshot).where(BackupSnapshot.repository_id == repository.id)
    if only:
        stmt = stmt.where(BackupSnapshot.snapshot_id == str(only))
    rows = list(session.execute(stmt.order_by(BackupSnapshot.taken_at)).scalars())
    if only and not rows:
        raise BadRequestError("Unknown snapshot", details={"snapshot_id": only})
    checked: dict[str, str | None] = {}
    failed_snapshots: list[str] = list(unreadable)
    for row in rows:
        errors: list[str] = []
        if row.snapshot_id not in on_target:
            errors.append("The snapshot file is missing from the repository")
        else:
            try:
                manifest = repo.read_snapshot(row.snapshot_id)
            except RemoteError as exc:
                errors.append(exc.message)
                manifest = {"entries": []}
            for entry in manifest["entries"]:
                if not heartbeat():
                    return RUN_CANCELLED
                problem = _verify_entry(repo, entry, blobs, checked, full=full, run=run)
                run.files += 1
                if problem is not None:
                    errors.append(problem)
        row.verified_at = utcnow_naive()
        row.verify_status = VERIFY_FAILED if errors else VERIFY_OK
        row.verify_error = "; ".join(errors[:20]) or None
        if errors:
            run.errors += len(errors)
            failed_snapshots.append(row.snapshot_id)
        session.commit()
    repository.last_verified_at = utcnow_naive()
    repository.last_verify_status = VERIFY_FAILED if failed_snapshots else VERIFY_OK
    _merge_summary(
        run,
        {
            "full": full,
            "snapshots_checked": len(rows),
            "failed_snapshots": failed_snapshots[:_MAX_PROBLEMS],
            "unreadable_snapshots": unreadable,
            "blobs": len(blobs),
        },
    )
    session.commit()
    return RUN_FAILED if failed_snapshots else RUN_SUCCEEDED


def _verify_entry(
    repo: Repository,
    entry: Any,
    blobs: dict[str, int],
    checked: dict[str, str | None],
    *,
    full: bool,
    run: BackupRun,
) -> str | None:
    if not isinstance(entry, dict):
        return "Malformed manifest entry"
    path, blob, sha256 = str(entry.get("path")), str(entry.get("blob")), entry.get("sha256")
    if blob not in blobs:
        return f"{path}: content blob is missing"
    if not full:
        return None
    if blob not in checked:
        with repo.temp_file() as out:
            try:
                digest, size = repo.get_blob(blob, out)
            except crypto.DecryptionError as exc:
                checked[blob] = f"content blob is corrupted ({exc})"
            except RemoteError as exc:
                checked[blob] = f"content blob cannot be read ({exc.message})"
            else:
                run.bytes_processed += size
                checked[blob] = None if digest == sha256 else "content checksum mismatch"
    problem = checked[blob]
    return None if problem is None else f"{path}: {problem}"


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def select_entries(manifest: dict[str, Any], paths: list[str]) -> list[dict[str, Any]]:
    """Manifest entries matching ``paths`` (files or folder prefixes); all when empty."""
    entries = [e for e in manifest["entries"] if isinstance(e, dict)]
    if not paths:
        return entries
    wanted = [normalize_remote_path(p) for p in paths]
    return [
        e
        for e in entries
        if any(not w or e["path"] == w or str(e["path"]).startswith(w + "/") for w in wanted)
    ]


def _safe_rel(path: Any) -> str:
    rel = normalize_remote_path(str(path))
    if not rel or rel != str(path):
        raise BadRequestError("Unsafe path in the snapshot", details={"path": str(path)})
    return rel


def restore_base(settings: Settings, run: BackupRun, target: dict[str, Any]) -> Path:
    """The local directory a restore writes into (jailed)."""
    kind = target.get("kind")
    if kind == "files":
        root = files.resolve_root(settings, str(target.get("root") or ""))
        return files.resolve_in_root(root, str(target.get("path") or ""))
    if kind == "media":
        media = settings.resolved_media_dir.resolve(strict=False)
        return files.resolve_in_root(media, str(target.get("path") or ""))
    if kind == "staging":
        return staging_restore_dir(settings, run.id)
    raise BadRequestError("Unknown restore target", details={"kind": kind})


def staging_restore_dir(settings: Settings, run_id: int) -> Path:
    return settings.resolved_backup_staging_dir / "restore" / str(run_id)


def run_restore(
    session: Session,
    settings: Settings,
    run: BackupRun,
    repo: Repository,
    *,
    heartbeat: Heartbeat,
    target_provider: RemoteProvider | None = None,
) -> str:
    params = run.params
    snapshot_id = str(params.get("snapshot_id"))
    manifest = repo.read_snapshot(snapshot_id)
    entries = select_entries(manifest, list(params.get("paths") or []))
    target: dict[str, Any] = dict(params.get("target") or {})
    overwrite = bool(params.get("overwrite"))
    stamp = utcnow_naive().strftime("%Y%m%dT%H%M%SZ")
    problems = Problems(run)
    replaced: list[str] = []
    restored: dict[str, str] = {}
    writer: _Writer
    if target.get("kind") == "remote":
        if target_provider is None:  # pragma: no cover - built by the caller
            raise BadRequestError("A remote restore target needs a provider")
        writer = _RemoteWriter(
            target_provider, normalize_remote_path(str(target.get("path") or "")), stamp
        )
    else:
        base = restore_base(settings, run, target)
        if not run.dry_run:
            base.mkdir(parents=True, exist_ok=True, mode=0o750)
        writer = _LocalWriter(base, stamp)
    for entry in entries:
        if not heartbeat():
            _merge_summary(run, {"problems": problems.items, "replaced": replaced[:500]})
            return RUN_CANCELLED
        try:
            rel = _safe_rel(entry.get("path"))
            exists = writer.exists(rel)
            if exists and not overwrite:
                run.skipped += 1
                continue
            if run.dry_run:
                run.files += 1
                run.bytes_processed += int(entry.get("size") or 0)
                if exists:
                    replaced.append(rel)
                continue
            with repo.temp_file() as plain:
                digest, size = repo.get_blob(str(entry.get("blob")), plain)
                if digest != entry.get("sha256"):
                    problems.add(rel, "Content checksum mismatch; the file was not restored")
                    continue
                plain.seek(0)
                writer.write(rel, plain, entry, replace=exists)
            if exists:
                replaced.append(rel)
            restored[rel] = digest
            run.files += 1
            run.bytes_processed += size
        except (BadRequestError, RemoteError, OSError, crypto.DecryptionError) as exc:
            message = getattr(exc, "message", None) or str(exc)
            problems.add(str(entry.get("path")), message)
    if target.get("kind") == "staging" and not run.dry_run:
        marker = {
            "run_id": run.id,
            "snapshot_id": snapshot_id,
            "repository_id": run.repository_id,
            "source": manifest.get("source"),
            "files": restored,
            "staged_at": utcnow_naive().isoformat() + "Z",
        }
        (restore_base(settings, run, target) / RESTORE_MARKER).write_text(
            json.dumps(marker, indent=2)
        )
    _merge_summary(
        run,
        {
            "problems": problems.items,
            "replaced": replaced[:500],
            "replaced_count": len(replaced),
            "replaced_dir": join(REPLACED_DIR, stamp) if replaced and not run.dry_run else None,
            "selected": len(entries),
        },
    )
    session.commit()
    return RUN_PARTIAL if run.errors else RUN_SUCCEEDED


class _Writer:
    def exists(self, rel: str) -> bool:
        raise NotImplementedError

    def write(self, rel: str, plain: Any, entry: dict[str, Any], *, replace: bool) -> None:
        raise NotImplementedError


class _LocalWriter(_Writer):
    def __init__(self, base: Path, stamp: str) -> None:
        self.base = base
        self.stamp = stamp

    def _dest(self, rel: str) -> Path:
        return files.resolve_in_root(self.base, rel)

    def exists(self, rel: str) -> bool:
        dest = self._dest(rel)
        if dest.is_dir() and not dest.is_symlink():
            raise BadRequestError("A folder is in the way", details={"path": rel})
        return dest.exists() or dest.is_symlink()

    def write(self, rel: str, plain: Any, entry: dict[str, Any], *, replace: bool) -> None:
        dest = self._dest(rel)
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = dest.parent / f"{files.REMOTE_TMP_PREFIX}{uuid.uuid4().hex}.part"
        try:
            with temporary.open("xb") as out:
                while chunk := plain.read(1024 * 1024):
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temporary, int(entry.get("mode") or 0o640) & 0o777)
            mtime_ns = int(entry.get("mtime_ns") or 0)
            if mtime_ns > 0:
                os.utime(temporary, ns=(mtime_ns, mtime_ns))
            if replace:
                archive = files.resolve_in_root(self.base, join(REPLACED_DIR, self.stamp, rel))
                archive.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                os.rename(dest, archive)
            os.replace(temporary, dest)
        finally:
            temporary.unlink(missing_ok=True)


class _RemoteWriter(_Writer):
    def __init__(self, provider: RemoteProvider, base: str, stamp: str) -> None:
        self.provider = provider
        self.base = base
        self.stamp = stamp

    def exists(self, rel: str) -> bool:
        entry = self.provider.stat(join(self.base, rel))
        if entry is not None and entry.is_dir:
            raise BadRequestError("A folder is in the way", details={"path": rel})
        return entry is not None

    def write(self, rel: str, plain: Any, entry: dict[str, Any], *, replace: bool) -> None:
        dest = join(self.base, rel)
        if replace:
            archive = join(self.base, REPLACED_DIR, self.stamp, rel)
            self.provider.makedirs(parent_of(archive))
            self.provider.move(dest, archive)
        self.provider.makedirs(parent_of(dest))
        self.provider.upload(plain, dest)


def restore_to_directory(
    repo: Repository, entries: list[dict[str, Any]], base: Path
) -> tuple[int, list[dict[str, str]]]:
    """Restore entries into an empty local directory (disaster recovery CLI).

    Returns the number of restored files and the per-file problems.
    """
    base.mkdir(parents=True, exist_ok=True, mode=0o750)
    writer = _LocalWriter(base, "")
    problems: list[dict[str, str]] = []
    restored = 0
    for entry in entries:
        path = str(entry.get("path"))
        try:
            rel = _safe_rel(path)
            if writer.exists(rel):
                problems.append({"path": rel, "error": "already exists; not replaced"})
                continue
            with repo.temp_file() as plain:
                digest, _ = repo.get_blob(str(entry.get("blob")), plain)
                if digest != entry.get("sha256"):
                    problems.append({"path": rel, "error": "content checksum mismatch"})
                    continue
                plain.seek(0)
                writer.write(rel, plain, entry, replace=False)
            restored += 1
        except (BadRequestError, RemoteError, OSError, crypto.DecryptionError) as exc:
            problems.append({"path": path, "error": getattr(exc, "message", None) or str(exc)})
    return restored, problems
