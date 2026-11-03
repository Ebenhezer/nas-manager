"""Three-way remote sync engine (Phase 7).

A run compares three views of a job's tree:

* the local directory (``scan_local``: regular files only, symlinks and
  in-flight temp files skipped),
* the remote directory (``RemoteProvider.walk``), and
* the baseline: what both sides looked like right after each file was last
  synced (``sync_job_entries``).

``plan`` is a pure function from those three views to a list of actions, so
every rule is unit-testable without I/O. The rules never delete data:

* A side "changed" when its size, or its etag/mtime, differs from the
  baseline. One changed side is copied to the other (when the job's
  direction allows it). In one-way jobs a changed *destination* is never
  overwritten: the file is skipped and reported.
* Both sides changed, or both exist without a baseline: ``resolve``. The
  executor first compares content (MD5 when the provider reports one,
  otherwise a byte comparison); identical files are simply adopted. Real
  conflicts follow the job's policy: ``keep_both`` keeps both versions under
  distinct names, ``prefer_local`` / ``prefer_remote`` move the losing
  version into the archive before overwriting it.
* A file deleted on one side is never deleted on the other. With
  ``delete_policy=ignore`` the deletion is reported; with ``archive`` the
  other side's copy is moved into ``.nas-sync-archive/``. A deletion on one
  side while the other side modified the file restores the modified file.
* The deletion guard fails a run before any action when it would archive
  more than the configured count or share of the synced files.

``SyncExecutor`` applies a plan. Downloads stream into a ``.nas-remote-*``
temp file next to the target and are moved into place atomically after a
jail re-check; uploads go to a remote temp name first where the protocol
allows it. The baseline row and the run's counters are committed after
every action, so a crash or cancellation leaves a consistent state and the
next run carries on from there.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import posixpath
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, RemoteError
from app.db.base import utcnow_naive
from app.models import SyncConflict, SyncJob, SyncJobEntry, SyncRun
from app.models.file_change import CHANGE_CREATED, CHANGE_MODIFIED, CHANGE_RENAMED
from app.models.remote import (
    CONFLICT_BOTH_MODIFIED,
    CONFLICT_FIRST_SYNC_DIFFERS,
    CONFLICT_KEEP_BOTH,
    CONFLICT_MODIFIED_VS_DELETED,
    CONFLICT_PREFER_LOCAL,
    CONFLICT_PREFER_REMOTE,
    DELETE_ARCHIVE,
    DIRECTION_BIDIRECTIONAL,
    DIRECTION_PULL,
    DIRECTION_PUSH,
)
from app.services import audit, files, sync
from app.services.remote.base import (
    ARCHIVE_DIR_NAME,
    RemoteEntry,
    RemoteProvider,
    check_name,
    join,
    parent_of,
)

logger = logging.getLogger(__name__)

OP_UPLOAD = "upload"
OP_DOWNLOAD = "download"
OP_RESOLVE = "resolve"
OP_ARCHIVE_LOCAL = "archive_local"
OP_ARCHIVE_REMOTE = "archive_remote"
OP_FORGET = "forget"
OP_SKIP = "skip"

# Below this many archives the percentage rule of the deletion guard does
# not apply, so pruning a handful of files from a small tree never trips it.
GUARD_MIN_ARCHIVES = 5
# A run stops (failed) after this many consecutive per-file errors: the
# remote is most likely unreachable and every further action would fail too.
MAX_CONSECUTIVE_ERRORS = 10
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class LocalFile:
    path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class Baseline:
    local_size: int
    local_mtime_ns: int
    remote_size: int
    remote_mtime: datetime | None
    remote_etag: str | None


@dataclass(frozen=True)
class Action:
    op: str
    path: str
    reason: str = ""
    # Set when the action resolves a conflict (resolve, or a restore).
    conflict: str | None = None

    def as_dict(self) -> dict[str, str]:
        data = {"op": self.op, "path": self.path}
        if self.reason:
            data["reason"] = self.reason
        if self.conflict:
            data["conflict"] = self.conflict
        return data


@dataclass
class Plan:
    actions: list[Action]
    baseline_count: int

    def count(self, op: str) -> int:
        return sum(1 for action in self.actions if action.op == op)

    @property
    def archive_count(self) -> int:
        return self.count(OP_ARCHIVE_LOCAL) + self.count(OP_ARCHIVE_REMOTE)


class DeletionGuardError(AppError):
    status_code = 409
    code = "deletion_guard"


class _Skip(Exception):
    """A per-file condition that makes the action unsafe right now."""


# ---------------------------------------------------------------------------
# Scanning and planning (pure)
# ---------------------------------------------------------------------------


def naive_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def is_excluded(rel: str, patterns: list[str]) -> bool:
    """A path is excluded when the whole path or any segment matches."""
    if not patterns:
        return False
    segments = rel.split("/")
    for pattern in patterns:
        if fnmatch.fnmatchcase(rel, pattern):
            return True
        if any(fnmatch.fnmatchcase(segment, pattern) for segment in segments):
            return True
    return False


def scan_local(
    base: Path, excludes: list[str]
) -> tuple[dict[str, LocalFile], list[tuple[str, str]]]:
    """Regular files under ``base`` keyed by POSIX relative path.

    Never follows symlinks; skips temp files, the archive directory and
    excluded paths. Returns ``(files, rejected)``.
    """
    found: dict[str, LocalFile] = {}
    rejected: list[tuple[str, str]] = []
    if not base.is_dir():
        return found, rejected
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = Path(dirpath).relative_to(base).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        kept: list[str] = []
        for name in dirnames:
            rel = join(rel_dir, name)
            if (not rel_dir and name == ARCHIVE_DIR_NAME) or name.startswith(files.TMP_PREFIXES):
                continue
            if os.path.islink(os.path.join(dirpath, name)) or is_excluded(rel, excludes):
                continue
            reason = check_name(name)
            if reason is not None:
                rejected.append((rel, reason))
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            rel = join(rel_dir, name)
            if name.startswith(files.TMP_PREFIXES) or is_excluded(rel, excludes):
                continue
            reason = check_name(name)
            if reason is not None:
                rejected.append((rel, reason))
                continue
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            found[rel] = LocalFile(path=rel, size=info.st_size, mtime_ns=info.st_mtime_ns)
    return found, rejected


def local_changed(base: Baseline, current: LocalFile) -> bool:
    return current.size != base.local_size or current.mtime_ns != base.local_mtime_ns


def remote_changed(base: Baseline, current: RemoteEntry) -> bool:
    if current.size != base.remote_size:
        return True
    if current.etag and base.remote_etag:
        return current.etag != base.remote_etag
    return naive_utc(current.mtime) != naive_utc(base.remote_mtime)


def plan(
    local: dict[str, LocalFile],
    remote: dict[str, RemoteEntry],
    baseline: dict[str, Baseline],
    *,
    direction: str,
    delete_policy: str,
) -> Plan:
    """Decide what to do with every path (see the module docstring)."""
    push = direction in (DIRECTION_PUSH, DIRECTION_BIDIRECTIONAL)
    pull = direction in (DIRECTION_PULL, DIRECTION_BIDIRECTIONAL)
    archive = delete_policy == DELETE_ARCHIVE
    actions: list[Action] = []

    for path in sorted(set(local) | set(remote) | set(baseline)):
        lf = local.get(path)
        rf = remote.get(path)
        base = baseline.get(path)

        if base is None:
            if lf is not None and rf is None:
                if push:
                    actions.append(Action(OP_UPLOAD, path, "new local file"))
            elif rf is not None and lf is None:
                if pull:
                    actions.append(Action(OP_DOWNLOAD, path, "new remote file"))
            elif lf is not None and rf is not None:
                actions.append(
                    Action(
                        OP_RESOLVE,
                        path,
                        "exists on both sides without a sync record",
                        conflict=CONFLICT_FIRST_SYNC_DIFFERS,
                    )
                )
            continue

        lc = lf is not None and local_changed(base, lf)
        rc = rf is not None and remote_changed(base, rf)

        if lf is not None and rf is not None:
            if lc and rc:
                actions.append(
                    Action(
                        OP_RESOLVE, path, "changed on both sides", conflict=CONFLICT_BOTH_MODIFIED
                    )
                )
            elif lc:
                if push:
                    actions.append(Action(OP_UPLOAD, path, "local file changed"))
                else:
                    actions.append(
                        Action(OP_SKIP, path, "local copy changed; a pull job never overwrites it")
                    )
            elif rc:
                if pull:
                    actions.append(Action(OP_DOWNLOAD, path, "remote file changed"))
                else:
                    actions.append(
                        Action(OP_SKIP, path, "remote copy changed; a push job never overwrites it")
                    )
        elif lf is not None:  # gone on the remote side
            if lc:
                if push:
                    actions.append(
                        Action(
                            OP_UPLOAD,
                            path,
                            "deleted remotely but modified locally; restoring",
                            conflict=CONFLICT_MODIFIED_VS_DELETED,
                        )
                    )
                else:
                    actions.append(
                        Action(OP_SKIP, path, "deleted remotely but modified locally; kept")
                    )
            elif pull and archive:
                actions.append(Action(OP_ARCHIVE_LOCAL, path, "deleted remotely"))
            else:
                actions.append(Action(OP_SKIP, path, "deleted remotely; deletion not propagated"))
        elif rf is not None:  # gone on the local side
            if rc:
                if pull:
                    actions.append(
                        Action(
                            OP_DOWNLOAD,
                            path,
                            "deleted locally but modified remotely; restoring",
                            conflict=CONFLICT_MODIFIED_VS_DELETED,
                        )
                    )
                else:
                    actions.append(
                        Action(OP_SKIP, path, "deleted locally but modified remotely; kept")
                    )
            elif push and archive:
                actions.append(Action(OP_ARCHIVE_REMOTE, path, "deleted locally"))
            else:
                actions.append(Action(OP_SKIP, path, "deleted locally; deletion not propagated"))
        else:
            actions.append(Action(OP_FORGET, path, "gone on both sides"))
    return Plan(actions=actions, baseline_count=len(baseline))


def check_deletion_guard(plan_: Plan, *, max_count: int, max_percent: int) -> None:
    """Refuse plans that would archive too much (before any action runs)."""
    archives = plan_.archive_count
    if archives == 0:
        return
    details = {
        "archives": archives,
        "baseline_files": plan_.baseline_count,
        "max_count": max_count,
        "max_percent": max_percent,
    }
    if archives > max_count:
        raise DeletionGuardError(
            f"Run would archive {archives} files (limit {max_count})", details=details
        )
    if (
        archives > GUARD_MIN_ARCHIVES
        and plan_.baseline_count > 0
        and archives * 100 > max_percent * plan_.baseline_count
    ):
        raise DeletionGuardError(
            f"Run would archive {archives} of {plan_.baseline_count} synced files"
            f" (limit {max_percent}%)",
            details=details,
        )


def conflict_name(path: str, side: str, stamp: str, exists: Callable[[str], bool]) -> str:
    """``dir/name (conflict remote 20261103-101500).ext``, uniquified."""
    directory, name = posixpath.split(path)
    stem, dot, suffix = name.rpartition(".")
    if not stem:  # no extension, or a dotfile
        stem, dot, suffix = name, "", ""
    for counter in range(0, 1000):
        extra = f" {counter}" if counter else ""
        candidate_name = f"{stem} (conflict {side} {stamp}{extra}){dot}{suffix}"
        candidate = join(directory, candidate_name)
        if not exists(candidate):
            return candidate
    raise RemoteError("Could not find a free conflict-copy name", details={"path": path})


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class RunCounters:
    uploaded: int = 0
    downloaded: int = 0
    conflicts: int = 0
    archived: int = 0
    skipped: int = 0
    errors: int = 0
    bytes_transferred: int = 0
    problems: list[dict[str, str]] = field(default_factory=list)


class SyncExecutor:
    """Applies a plan for one job and run. Not thread-safe; one per run."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        job: SyncJob,
        run: SyncRun,
        provider: RemoteProvider,
        root: Path,
        local_base: Path,
        local: dict[str, LocalFile],
        remote: dict[str, RemoteEntry],
        baseline_rows: dict[str, SyncJobEntry],
        heartbeat: Callable[[], bool],
    ) -> None:
        self.session = session
        self.settings = settings
        self.job = job
        self.run = run
        self.provider = provider
        self.root = root
        self.local_base = local_base
        self.local = local
        self.remote = remote
        self.baseline_rows = baseline_rows
        self.heartbeat = heartbeat
        self.counters = RunCounters()
        self.stamp = utcnow_naive().strftime("%Y%m%d-%H%M%S")
        self.cancelled = False

    # -- path helpers -------------------------------------------------------

    def remote_path(self, rel: str) -> str:
        return join(self.job.remote_path, rel)

    def local_path(self, rel: str) -> Path:
        prefix = self.local_base.relative_to(self.root).as_posix()
        target = files.resolve_in_root(self.root, join("" if prefix == "." else prefix, rel))
        if not target.is_relative_to(self.local_base):
            raise _Skip("path escapes the job directory")
        return target

    def journal_path(self, target: Path) -> str:
        return target.relative_to(self.root).as_posix()

    # -- main loop ------------------------------------------------------------

    def execute(self, plan_: Plan) -> None:
        consecutive_errors = 0
        for action in plan_.actions:
            if action.op == OP_FORGET:
                self._forget(action.path)
                self.session.commit()
                continue
            if action.op == OP_SKIP:
                self.counters.skipped += 1
                self._problem(action, action.reason, kind="skipped")
                continue
            if not self.heartbeat():
                self.cancelled = True
                return
            try:
                self._apply(action)
                consecutive_errors = 0
            except _Skip as exc:
                self.session.rollback()
                self.counters.skipped += 1
                self._problem(action, str(exc), kind="skipped")
            except (AppError, OSError) as exc:
                self.session.rollback()
                self.counters.errors += 1
                consecutive_errors += 1
                message = exc.message if isinstance(exc, AppError) else exc.strerror or str(exc)
                self._problem(action, message, kind="error")
                logger.warning(
                    "sync action failed",
                    extra={"job_id": self.job.id, "op": action.op, "path": action.path},
                )
            self.flush_counters()
            self.session.commit()
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                raise RemoteError(
                    f"Stopped after {consecutive_errors} consecutive errors",
                    details={"last_path": action.path},
                )

    def _apply(self, action: Action) -> None:
        if action.op == OP_UPLOAD:
            self._upload(action.path, overwrite_expected=self.remote.get(action.path))
            if action.conflict:
                self._record_conflict(action.path, action.conflict, "restored_to_remote")
        elif action.op == OP_DOWNLOAD:
            self._download(action.path, expect_local=self.local.get(action.path))
            if action.conflict:
                self._record_conflict(action.path, action.conflict, "restored_to_local")
        elif action.op == OP_RESOLVE:
            self._resolve(action)
        elif action.op == OP_ARCHIVE_LOCAL:
            self._archive_local(action.path)
            self._forget(action.path)
        elif action.op == OP_ARCHIVE_REMOTE:
            self._archive_remote(action.path)
            self._forget(action.path)
        else:  # pragma: no cover - plan never emits anything else
            raise RemoteError(f"unknown sync action {action.op}")

    # -- primitive operations -----------------------------------------------

    def _local_stat(self, rel: str) -> LocalFile | None:
        try:
            info = os.lstat(self.local_path(rel))
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise _Skip("local path is not a regular file")
        return LocalFile(path=rel, size=info.st_size, mtime_ns=info.st_mtime_ns)

    def _upload(self, rel: str, *, overwrite_expected: RemoteEntry | None) -> None:
        scanned = self.local.get(rel)
        before = self._local_stat(rel)
        if before is None or (scanned is not None and before != scanned):
            raise _Skip("local file changed since the scan; retrying next run")
        full = self.remote_path(rel)
        current = self.provider.stat(full)
        if not _same_remote(current, overwrite_expected):
            raise _Skip("remote file changed since the scan; retrying next run")
        self.provider.makedirs(parent_of(full))
        with self.local_path(rel).open("rb") as source:
            self.provider.upload(source, full)
        after = self._local_stat(rel)
        if after != before:
            raise _Skip("local file changed during the upload; retrying next run")
        uploaded = self.provider.stat(full)
        if uploaded is None:
            raise RemoteError("Uploaded file is missing on the remote", details={"path": rel})
        self.counters.uploaded += 1
        self.counters.bytes_transferred += before.size
        self._save_baseline(rel, before, uploaded)

    def _download_temp(self, rel: str) -> Path:
        target = self.local_path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(self.local_base):
            raise _Skip("path escapes the job directory")
        temporary = target.parent / f"{files.REMOTE_TMP_PREFIX}{uuid.uuid4().hex}.part"
        try:
            with temporary.open("xb") as out:
                self.provider.download(self.remote_path(rel), out)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.counters.bytes_transferred += temporary.stat().st_size
        return temporary

    def _place(self, temporary: Path, rel: str, *, expect_local: LocalFile | None) -> LocalFile:
        """Move a downloaded temp file to ``rel`` if the local file is still
        what the scan saw (absent, or unchanged); journal the change."""
        try:
            current = self._local_stat(rel)
            if current != expect_local:
                raise _Skip("local file changed since the scan; retrying next run")
            target = self.local_path(rel)
            if not target.parent.resolve().is_relative_to(self.local_base):
                raise _Skip("path escapes the job directory")
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        placed = self._local_stat(rel)
        assert placed is not None  # noqa: S101 - just written
        sync.record_change(
            self.session,
            root=str(self.root),
            path=self.journal_path(target),
            kind=CHANGE_MODIFIED if expect_local is not None else CHANGE_CREATED,
            size_bytes=placed.size,
        )
        return placed

    def _download(self, rel: str, *, expect_local: LocalFile | None) -> None:
        entry = self.remote.get(rel)
        if entry is None:
            raise _Skip("remote file vanished")
        temporary = self._download_temp(rel)
        placed = self._place(temporary, rel, expect_local=expect_local)
        self.counters.downloaded += 1
        self._save_baseline(rel, placed, entry)

    def _archive_local(self, rel: str) -> str:
        source = self.local_path(rel)
        archive_rel = join(ARCHIVE_DIR_NAME, f"job-{self.job.id}", self.stamp, rel)
        destination = self.local_path(archive_rel)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise _Skip("archive destination already exists")
        os.rename(source, destination)
        sync.record_change(
            self.session,
            root=str(self.root),
            path=self.journal_path(destination),
            old_path=self.journal_path(source),
            kind=CHANGE_RENAMED,
        )
        self._audit_archive(rel, "local", archive_rel)
        self.counters.archived += 1
        return archive_rel

    def _archive_remote(self, rel: str) -> str:
        archive_rel = join(ARCHIVE_DIR_NAME, f"job-{self.job.id}", self.stamp, rel)
        destination = self.remote_path(archive_rel)
        self.provider.makedirs(parent_of(destination))
        self.provider.move(self.remote_path(rel), destination)
        self._audit_archive(rel, "remote", archive_rel)
        self.counters.archived += 1
        return archive_rel

    # -- conflicts -------------------------------------------------------------

    def _content_equal(self, rel: str, entry: RemoteEntry) -> tuple[bool, Path | None]:
        """Compare content; returns ``(equal, downloaded_temp_or_None)``."""
        lf = self.local[rel]
        if lf.size != entry.size:
            return False, None
        if entry.md5:
            return _md5(self.local_path(rel)) == entry.md5.lower(), None
        temporary = self._download_temp(rel)
        try:
            equal = _files_equal(temporary, self.local_path(rel))
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return equal, temporary

    def _resolve(self, action: Action) -> None:
        rel = action.path
        lf = self.local.get(rel)
        entry = self.remote.get(rel)
        if lf is None or entry is None:
            raise _Skip("file vanished while resolving")
        equal, temporary = self._content_equal(rel, entry)
        try:
            if equal:
                current = self._local_stat(rel)
                if current != lf:
                    raise _Skip("local file changed since the scan; retrying next run")
                self._save_baseline(rel, lf, entry)
                return
            self._resolve_conflict(rel, action.conflict or CONFLICT_BOTH_MODIFIED, lf, temporary)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _resolve_conflict(self, rel: str, kind: str, lf: LocalFile, temporary: Path | None) -> None:
        policy = self.job.conflict_policy
        direction = self.job.direction
        copy_path: str | None = None
        if policy == CONFLICT_KEEP_BOTH:
            if direction == DIRECTION_PULL:
                # The source (remote) keeps the name; the local edit is kept aside.
                copy_path = self._rename_local_aside(rel)
                self._download(rel, expect_local=None)
            elif direction == DIRECTION_PUSH:
                copy_path = self._move_remote_aside(rel)
                self._upload(rel, overwrite_expected=None)
            else:
                # Two-way: the local file keeps its name; the remote version is
                # saved next to it, and both reach the remote on the next run.
                copy_path = self._save_remote_copy_locally(rel, temporary)
                self._upload(rel, overwrite_expected=self.remote[rel])
            resolution = "kept_both"
        elif policy == CONFLICT_PREFER_LOCAL:
            if direction == DIRECTION_PULL:
                resolution = "kept_local"
                self._save_baseline(rel, lf, self.remote[rel])
            else:
                copy_path = self._archive_remote(rel)
                self._upload(rel, overwrite_expected=None)
                resolution = "local_won_remote_archived"
        elif policy == CONFLICT_PREFER_REMOTE:
            if direction == DIRECTION_PUSH:
                resolution = "kept_remote"
                self._save_baseline(rel, lf, self.remote[rel])
            else:
                copy_path = self._archive_local(rel)
                self._download(rel, expect_local=None)
                resolution = "remote_won_local_archived"
        else:  # pragma: no cover - validated on input
            raise RemoteError(f"unknown conflict policy {policy}")
        self._record_conflict(rel, kind, resolution, copy_path)

    def _rename_local_aside(self, rel: str) -> str:
        copy_rel = conflict_name(rel, "local", self.stamp, lambda p: self.local_path(p).exists())
        source = self.local_path(rel)
        destination = self.local_path(copy_rel)
        os.rename(source, destination)
        sync.record_change(
            self.session,
            root=str(self.root),
            path=self.journal_path(destination),
            old_path=self.journal_path(source),
            kind=CHANGE_RENAMED,
        )
        return copy_rel

    def _move_remote_aside(self, rel: str) -> str:
        copy_rel = conflict_name(
            rel,
            "remote",
            self.stamp,
            lambda p: p in self.remote or self.provider.stat(self.remote_path(p)) is not None,
        )
        self.provider.move(self.remote_path(rel), self.remote_path(copy_rel))
        return copy_rel

    def _save_remote_copy_locally(self, rel: str, temporary: Path | None) -> str:
        copy_rel = conflict_name(
            rel, "remote", self.stamp, lambda p: p in self.local or self.local_path(p).exists()
        )
        staged = temporary if temporary is not None else self._download_temp(rel)
        target = self.local_path(copy_rel)
        try:
            os.replace(staged, target)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        sync.record_change(
            self.session,
            root=str(self.root),
            path=self.journal_path(target),
            kind=CHANGE_CREATED,
            size_bytes=target.stat().st_size,
        )
        self.counters.downloaded += 1
        return copy_rel

    def _record_conflict(
        self, rel: str, kind: str, resolution: str, copy_path: str | None = None
    ) -> None:
        self.counters.conflicts += 1
        self.session.add(
            SyncConflict(
                job_id=self.job.id,
                run_id=self.run.id,
                path=rel,
                kind=kind,
                resolution=resolution,
                conflict_copy_path=copy_path,
            )
        )
        audit.record_audit_event(
            self.session,
            event=audit.SYNC_CONFLICT,
            category=audit.CATEGORY_REMOTE_SYNC,
            actor_username="sync-worker",
            target_type="sync_job",
            target_id=self.job.id,
            details={
                "run_id": self.run.id,
                "path": rel,
                "kind": kind,
                "resolution": resolution,
                "conflict_copy_path": copy_path,
            },
            commit=False,
        )

    def _audit_archive(self, rel: str, side: str, archive_rel: str) -> None:
        audit.record_audit_event(
            self.session,
            event=audit.SYNC_ARCHIVED,
            category=audit.CATEGORY_REMOTE_SYNC,
            actor_username="sync-worker",
            target_type="sync_job",
            target_id=self.job.id,
            details={"run_id": self.run.id, "path": rel, "side": side, "archive_path": archive_rel},
            commit=False,
        )

    # -- bookkeeping ----------------------------------------------------------

    def _save_baseline(self, rel: str, lf: LocalFile, entry: RemoteEntry) -> None:
        row = self.baseline_rows.get(rel)
        if row is None:
            row = SyncJobEntry(job_id=self.job.id, path=rel)
            self.session.add(row)
            self.baseline_rows[rel] = row
        row.local_size = lf.size
        row.local_mtime_ns = lf.mtime_ns
        row.remote_size = entry.size
        row.remote_mtime = naive_utc(entry.mtime)
        row.remote_etag = entry.etag
        row.synced_at = utcnow_naive()

    def _forget(self, rel: str) -> None:
        row = self.baseline_rows.pop(rel, None)
        if row is not None:
            self.session.delete(row)

    def _problem(self, action: Action, message: str, *, kind: str) -> None:
        if len(self.counters.problems) < self.settings.remote_sync_preview_limit:
            self.counters.problems.append(
                {"kind": kind, "op": action.op, "path": action.path, "message": message}
            )

    def flush_counters(self) -> None:
        run = self.run
        counters = self.counters
        run.uploaded = counters.uploaded
        run.downloaded = counters.downloaded
        run.conflicts = counters.conflicts
        run.archived = counters.archived
        run.skipped = counters.skipped
        run.errors = counters.errors
        run.bytes_transferred = counters.bytes_transferred


def _same_remote(current: RemoteEntry | None, expected: RemoteEntry | None) -> bool:
    if current is None or expected is None:
        return current is None and expected is None
    if current.size != expected.size:
        return False
    if current.etag and expected.etag:
        return current.etag == expected.etag
    return naive_utc(current.mtime) == naive_utc(expected.mtime)


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _files_equal(a: Path, b: Path) -> bool:
    with a.open("rb") as left, b.open("rb") as right:
        while True:
            chunk_a = left.read(_CHUNK)
            chunk_b = right.read(_CHUNK)
            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


def load_baseline(session: Session, job_id: int) -> dict[str, SyncJobEntry]:
    rows = session.execute(select(SyncJobEntry).where(SyncJobEntry.job_id == job_id)).scalars()
    return {row.path: row for row in rows}


def baseline_view(rows: dict[str, SyncJobEntry]) -> dict[str, Baseline]:
    return {
        path: Baseline(
            local_size=row.local_size,
            local_mtime_ns=row.local_mtime_ns,
            remote_size=row.remote_size,
            remote_mtime=naive_utc(row.remote_mtime),
            remote_etag=row.remote_etag,
        )
        for path, row in rows.items()
    }


def summarize(plan_: Plan, limit: int) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for action in plan_.actions:
        counts[action.op] = counts.get(action.op, 0) + 1
    shown = [action.as_dict() for action in plan_.actions if action.op != OP_FORGET][:limit]
    return {
        "counts": counts,
        "actions": shown,
        "truncated": len(plan_.actions) - plan_.count(OP_FORGET) > len(shown),
        "baseline_files": plan_.baseline_count,
    }
