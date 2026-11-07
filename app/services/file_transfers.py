"""Copy and move jobs between NAS folders and USB drives (Phase 16).

A transfer is created by the Files page (``POST /files/transfers``) after
the API has checked the caller's access to every source and to the
destination, and then runs in a small thread pool in the web process. The
``file_transfers`` row is its progress, which the page polls.

Copying is careful in the ways a USB stick needs:

* each file is written to a hidden ``.<name>.nas-part-*`` file, fsynced and
  then renamed into place, so a half-written file never shows its real name;
* folders are merged into existing folders, files follow the chosen conflict
  rule (keep both, skip, replace);
* symbolic links are never followed (they are skipped and reported);
* when the destination is a USB drive the helper flushes it at the end, so
  the stick can be pulled out as soon as the transfer shows "done";
* errors are reported per file in plain words (a 4 GB file on FAT32, a name
  the drive cannot store, a full drive, a drive pulled out mid-copy).

A move renames within one file system and otherwise copies, then deletes
each source file only after its copy succeeded.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError
from app.db.base import utcnow_naive
from app.models import User
from app.models.file_change import CHANGE_CREATED, CHANGE_DELETED
from app.models.file_transfer import (
    ACTIVE_STATES,
    CONFLICT_KEEP_BOTH,
    CONFLICT_SKIP,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_INTERRUPTED,
    STATE_QUEUED,
    STATE_RUNNING,
    TRANSFER_COPY,
    TRANSFER_MOVE,
    FileTransfer,
)
from app.services import audit, files, public_uploads

logger = logging.getLogger(__name__)

MAX_SOURCES = 1000
_CHUNK = 1024 * 1024
_PROGRESS_EVERY = 0.5
_MAX_ERRORS = 50
PART_MARKER = ".nas-part-"
_FAT_LIMIT = 4 * 1024**3 - 1


class TransferAborted(Exception):
    """Stops the whole transfer (cancelled, drive full, drive removed)."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state
        self.message = message


@dataclass
class Location:
    """A resolved place: the root folder, the path inside it, the full path."""

    root: Path
    rel: str

    @property
    def path(self) -> Path:
        return files.resolve_in_root(self.root, self.rel)


# ---------------------------------------------------------------------------
# Creating a transfer (request time)
# ---------------------------------------------------------------------------


def _measure(path: Path) -> tuple[int, int]:
    """(bytes, files) below ``path`` without following symlinks."""
    st = path.lstat()
    if stat.S_ISREG(st.st_mode):
        return st.st_size, 1
    if not stat.S_ISDIR(st.st_mode):
        return 0, 0
    total, count = 0, 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            try:
                child = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(child.st_mode):
                total += child.st_size
                count += 1
    return total, count


def _same_device(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def create(
    session: Session,
    settings: Settings,
    *,
    actor: User,
    sources: list[Location],
    destination: Location,
    mode: str,
    conflict: str,
    context: dict[str, str | None] | None = None,
) -> FileTransfer:
    if not sources:
        raise BadRequestError("Nothing to copy")
    if len(sources) > MAX_SOURCES:
        raise BadRequestError(f"At most {MAX_SOURCES} items per transfer")
    dest_dir = destination.path
    if not dest_dir.is_dir():
        raise BadRequestError("The destination is not a folder", details={"path": destination.rel})
    total_bytes, total_files = 0, 0
    for source in sources:
        if not source.rel.strip("/"):
            raise BadRequestError(
                "A whole drive or root cannot be copied; open it and select items"
            )
        path = source.path
        if not os.path.lexists(path):
            raise BadRequestError("A selected item no longer exists", details={"path": source.rel})
        inside = dest_dir.resolve().is_relative_to(path.resolve())
        if path.is_dir() and not path.is_symlink() and inside:
            raise BadRequestError(
                "A folder cannot be copied into itself", details={"path": source.rel}
            )
        if mode == TRANSFER_MOVE and path.parent.resolve() == dest_dir.resolve():
            raise BadRequestError(
                "The items are already in that folder", details={"path": source.rel}
            )
        size, count = _measure(path)
        total_bytes += size
        total_files += count
    needs_space = mode == TRANSFER_COPY or not all(
        _same_device(source.path, dest_dir) for source in sources
    )
    if needs_space:
        usage = shutil.disk_usage(dest_dir)
        if total_bytes > usage.free:
            raise ConflictError(
                "There is not enough free space on the destination",
                details={"needed_bytes": total_bytes, "free_bytes": usage.free},
            )
    row = FileTransfer(
        user_id=actor.id,
        mode=mode,
        sources=json.dumps([{"root": str(s.root), "path": s.rel} for s in sources]),
        dest_root=str(destination.root),
        dest_path=destination.rel,
        conflict=conflict,
        state=STATE_QUEUED,
        total_bytes=total_bytes,
        total_files=total_files,
    )
    session.add(row)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.FILE_TRANSFER_STARTED,
        category=audit.CATEGORY_FILE,
        actor=actor,
        target_type="file_transfer",
        target_id=row.id,
        details={
            "mode": mode,
            "sources": [{"root": str(s.root), "path": s.rel} for s in sources[:50]],
            "source_count": len(sources),
            "dest_root": str(destination.root),
            "dest_path": destination.rel,
            "bytes": total_bytes,
            "files": total_files,
        },
        **files._audit_ctx(context),
        commit=False,
    )
    session.commit()
    return row


# ---------------------------------------------------------------------------
# Running a transfer (pool thread)
# ---------------------------------------------------------------------------


def _friendly(exc: OSError, *, removable_dest: bool) -> str:
    code = exc.errno
    if code == errno.EFBIG:
        return "the file is larger than 4 GB, which this drive's file system (FAT32) cannot store"
    if code in (errno.EINVAL, errno.ENAMETOOLONG, errno.EILSEQ) and removable_dest:
        return "the name contains characters or is too long for this drive's file system"
    if code in (errno.EACCES, errno.EPERM):
        return "permission denied"
    if code == errno.EROFS:
        return "the drive is read-only"
    return exc.strerror or str(exc)


@dataclass
class _Run:
    session: Session
    row: FileTransfer
    settings: Settings
    actor: User | None
    dest_root: Path
    removable_dest: bool
    stop: threading.Event
    errors: list[dict[str, str]] = field(default_factory=list)
    touched_dirs: set[Path] = field(default_factory=set)
    last_save: float = 0.0

    # -- progress ----------------------------------------------------------

    def save(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_save < _PROGRESS_EVERY:
            return
        self.last_save = now
        self.row.errors = json.dumps(self.errors)
        self.session.commit()
        cancelled = self.session.execute(
            select(FileTransfer.cancel_requested).where(FileTransfer.id == self.row.id)
        ).scalar_one()
        if cancelled:
            raise TransferAborted(STATE_CANCELLED, "Cancelled")
        if self.stop.is_set():
            raise TransferAborted(STATE_INTERRUPTED, "The server was restarted during the transfer")

    def error(self, rel: str, message: str) -> None:
        if len(self.errors) < _MAX_ERRORS:
            self.errors.append({"path": rel, "message": message})

    def check_destination(self) -> None:
        from app.services import removable

        if self.removable_dest and removable.find_drive(self.settings, str(self.dest_root)) is None:
            raise TransferAborted(STATE_FAILED, "The USB drive was removed during the transfer")

    def journal(
        self, root: Path, path: Path, kind: str, *, is_dir: bool = False, size: int | None = None
    ) -> None:
        files._journal(
            self.session,
            self.settings,
            root,
            path=str(path.relative_to(root)),
            kind=kind,
            is_dir=is_dir,
            size_bytes=size,
            actor=self.actor,
        )

    # -- copying -------------------------------------------------------------

    def target_for(self, source: Path, target: Path, *, top_level: bool) -> Path | None:
        """Where ``source`` goes, applying the conflict rule; None to skip."""
        exists = os.path.lexists(target)
        if source.resolve() == target.resolve():
            # Copying into the folder it is already in: Windows makes a copy.
            return public_uploads.unique_path(target.parent, target.name)
        if not exists:
            return target
        if (
            source.is_dir()
            and not source.is_symlink()
            and target.is_dir()
            and not target.is_symlink()
        ):
            if top_level and self.row.conflict == CONFLICT_KEEP_BOTH:
                return public_uploads.unique_path(target.parent, target.name)
            return target  # merge folders
        if self.row.conflict == CONFLICT_SKIP:
            return None
        if self.row.conflict == CONFLICT_KEEP_BOTH:
            return public_uploads.unique_path(target.parent, target.name)
        if target.is_dir() and not target.is_symlink():
            return None  # never replace a folder with a file
        return target  # replace (atomically, below)

    def copy_file(self, source: Path, target: Path, rel: str) -> bool:
        self.row.current_name = rel
        partial = target.parent / f".{target.name[:180]}{PART_MARKER}{uuid.uuid4().hex[:8]}"
        try:
            size = source.stat().st_size
            if self.removable_dest and size > _FAT_LIMIT and self._dest_is_fat():
                self.error(rel, _friendly(OSError(errno.EFBIG, "too large"), removable_dest=True))
                self.row.skipped_files += 1
                return False
            with source.open("rb") as reader, partial.open("wb") as writer:
                while chunk := reader.read(_CHUNK):
                    writer.write(chunk)
                    self.row.done_bytes += len(chunk)
                    self.save()
                writer.flush()
                os.fsync(writer.fileno())
            st = source.stat()
            with contextlib.suppress(OSError):  # some drives cannot store exact times
                os.utime(partial, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(partial, target)
        except TransferAborted:
            partial.unlink(missing_ok=True)
            raise
        except OSError as exc:
            partial.unlink(missing_ok=True)
            if exc.errno == errno.ENOSPC:
                raise TransferAborted(STATE_FAILED, "The destination drive is full") from exc
            self.check_destination()
            self.error(rel, _friendly(exc, removable_dest=self.removable_dest))
            self.row.skipped_files += 1
            return False
        self.touched_dirs.add(target.parent)
        self.row.done_files += 1
        self.journal(self.dest_root, target, CHANGE_CREATED, size=size)
        self.save()
        return True

    def _dest_is_fat(self) -> bool:
        from app.services import removable

        drive = removable.find_drive(self.settings, str(self.dest_root))
        return bool(drive and drive.fstype in {"vfat", "msdos"})

    def copy_tree(self, source: Path, target: Path, rel: str, *, top_level: bool) -> bool:
        """Copy ``source`` to ``target`` (resolved by the conflict rule). True when
        everything below it was copied (so a move may delete the source)."""
        if source.is_symlink():
            self.error(rel, "symbolic links are not copied")
            self.row.skipped_files += 1
            return False
        chosen = self.target_for(source, target, top_level=top_level)
        if chosen is None:
            size, count = _measure(source)
            self.row.skipped_files += count or 1
            self.row.done_bytes += size
            return False
        if source.is_file():
            return self.copy_file(source, chosen, rel)
        if not source.is_dir():
            self.error(rel, "not a regular file or folder")
            return False
        try:
            chosen.mkdir(exist_ok=True)
        except OSError as exc:
            self.check_destination()
            self.error(rel, _friendly(exc, removable_dest=self.removable_dest))
            return False
        self.touched_dirs.add(chosen.parent)
        self.journal(self.dest_root, chosen, CHANGE_CREATED, is_dir=True)
        complete = True
        for child in sorted(source.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith(files.TMP_PREFIXES) or PART_MARKER in child.name:
                continue
            complete = (
                self.copy_tree(child, chosen / child.name, f"{rel}/{child.name}", top_level=False)
                and complete
            )
        self.touched_dirs.add(chosen)
        return complete

    # -- moving ----------------------------------------------------------------

    def remove_source(self, root: Path, source: Path) -> None:
        """Delete a fully copied source (files first, then empty folders)."""
        is_dir = source.is_dir() and not source.is_symlink()
        try:
            if is_dir:
                shutil.rmtree(source)
            else:
                source.unlink()
        except OSError as exc:
            self.error(
                str(source.relative_to(root)),
                "copied, but the original could not be removed: "
                + _friendly(exc, removable_dest=False),
            )
            return
        self.touched_dirs.add(source.parent)
        self.journal(root, source, CHANGE_DELETED, is_dir=is_dir)

    def try_rename(self, source: Path, target: Path) -> bool:
        """Same-file-system move by rename, when nothing is in the way."""
        if os.path.lexists(target) or not _same_device(source.parent, target.parent):
            return False
        size, count = _measure(source)
        try:
            os.rename(source, target)
        except OSError:
            return False
        self.row.done_bytes += size
        self.row.done_files += count
        self.touched_dirs.update({source.parent, target.parent})
        return True


def _sync_dirs(paths: set[Path]) -> None:
    for directory in paths:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


def _flush_removable(settings: Settings, root: Path) -> None:
    if not settings.helper_enabled:
        return
    from app.services.helper_client import call_helper

    try:
        call_helper(settings, "removable.flush", {"mountpoint": str(root)})
    except Exception:  # the data is already fsynced file by file
        logger.warning("could not flush %s through the helper", root, exc_info=True)


def _sources(row: FileTransfer) -> Iterator[Location]:
    for item in json.loads(row.sources):
        yield Location(root=Path(item["root"]), rel=item["path"])


def execute(
    factory: sessionmaker[Session], settings: Settings, transfer_id: int, stop: threading.Event
) -> None:
    """Run one queued transfer to completion (pool thread)."""
    from app.services import removable

    with factory() as session:
        row = session.get(FileTransfer, transfer_id)
        if row is None or row.state != STATE_QUEUED:
            return
        actor = session.get(User, row.user_id) if row.user_id else None
        dest_root = Path(row.dest_root)
        run = _Run(
            session=session,
            row=row,
            settings=settings,
            actor=actor,
            dest_root=dest_root,
            removable_dest=files.is_removable_root(settings, dest_root),
            stop=stop,
        )
        row.state = STATE_RUNNING
        row.started_at = utcnow_naive()
        session.commit()
        state, message = STATE_DONE, None
        try:
            if run.removable_dest and removable.find_drive(settings, row.dest_root) is None:
                raise TransferAborted(STATE_FAILED, "The USB drive is no longer connected")
            dest_dir = files.resolve_in_root(dest_root, row.dest_path)
            for source in _sources(row):
                path = source.path
                rel = source.rel.strip("/")
                if not os.path.lexists(path):
                    run.error(rel, "no longer exists")
                    continue
                target = dest_dir / path.name
                if row.mode == TRANSFER_MOVE and run.try_rename(path, target):
                    run.journal(source.root, path, CHANGE_DELETED)
                    run.journal(dest_root, target, CHANGE_CREATED, is_dir=target.is_dir())
                    run.save()
                    continue
                complete = run.copy_tree(path, target, rel, top_level=True)
                if row.mode == TRANSFER_MOVE and complete:
                    run.remove_source(source.root, path)
                run.save()
        except TransferAborted as exc:
            state, message = exc.state, exc.message
        except Exception as exc:  # never leave a row "running"
            logger.exception("transfer %s failed", transfer_id)
            state, message = STATE_FAILED, f"Unexpected error: {exc}"
        _sync_dirs(run.touched_dirs)
        if run.removable_dest and state != STATE_FAILED:
            _flush_removable(settings, dest_root)
        if state == STATE_DONE and run.errors:
            message = f"Finished with {len(run.errors)} problem(s)"
        row.state = state
        row.message = message
        row.current_name = None
        row.errors = json.dumps(run.errors)
        row.finished_at = utcnow_naive()
        audit.record_audit_event(
            session,
            event=audit.FILE_TRANSFER_FINISHED,
            category=audit.CATEGORY_FILE,
            outcome=audit.OUTCOME_SUCCESS if state == STATE_DONE else audit.OUTCOME_FAILURE,
            actor=actor,
            target_type="file_transfer",
            target_id=row.id,
            details={
                "mode": row.mode,
                "state": state,
                "message": message,
                "dest_root": row.dest_root,
                "dest_path": row.dest_path,
                "files": row.done_files,
                "bytes": row.done_bytes,
                "skipped": row.skipped_files,
                "problems": len(run.errors),
            },
            commit=False,
        )
        session.commit()


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


class TransferManager:
    """The web process's transfer pool (``app.state.transfers``)."""

    def __init__(self, settings: Settings, factory: sessionmaker[Session]) -> None:
        self.settings = settings
        self.factory = factory
        self.stop = threading.Event()
        self.pool: ThreadPoolExecutor | None = None
        self._recovered = False

    def start(self) -> int:
        """Start the pool; the first start marks transfers a previous process
        left unfinished as interrupted (they cannot resume). Returns that count."""
        recovered = 0
        if not self._recovered:
            recovered = self.recover()
            self._recovered = True
        self.stop.clear()
        if self.pool is None:
            self.pool = ThreadPoolExecutor(
                max_workers=self.settings.file_transfer_workers, thread_name_prefix="transfer"
            )
        return recovered

    def recover(self) -> int:
        with self.factory() as session:
            result = session.execute(
                update(FileTransfer)
                .where(FileTransfer.state.in_(ACTIVE_STATES))
                .values(
                    state=STATE_INTERRUPTED,
                    message="The server was restarted before the transfer finished",
                    current_name=None,
                    finished_at=utcnow_naive(),
                )
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    def submit(self, transfer_id: int) -> None:
        if self.pool is None:
            self.start()
        assert self.pool is not None  # noqa: S101 - start() created it
        self.pool.submit(self._run, transfer_id)

    def _run(self, transfer_id: int) -> None:
        try:
            execute(self.factory, self.settings, transfer_id, self.stop)
        except Exception:
            logger.exception("transfer %s crashed", transfer_id)

    def shutdown(self) -> None:
        """Running transfers stop at their next progress update and mark
        themselves interrupted; queued ones are recovered on the next start."""
        self.stop.set()
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.pool = None


def request_cancel(
    session: Session, row: FileTransfer, actor: User, context: dict[str, str | None] | None = None
) -> FileTransfer:
    if row.state not in ACTIVE_STATES:
        return row
    row.cancel_requested = True
    if row.state == STATE_QUEUED:
        row.state = STATE_CANCELLED
        row.message = "Cancelled"
        row.finished_at = utcnow_naive()
    audit.record_audit_event(
        session,
        event=audit.FILE_TRANSFER_CANCEL_REQUESTED,
        category=audit.CATEGORY_FILE,
        actor=actor,
        target_type="file_transfer",
        target_id=row.id,
        **files._audit_ctx(context),
        commit=False,
    )
    session.commit()
    return row


def errors_of(row: FileTransfer) -> list[dict[str, str]]:
    try:
        value: Any = json.loads(row.errors or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []
