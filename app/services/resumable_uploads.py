"""Resumable (chunked) uploads for mobile and unreliable links (Phase 6).

Protocol (all routes under ``/api/v1/uploads``)::

    POST   /uploads                 declare target, name and total size -> id
    HEAD   /uploads/{id}            Upload-Offset: bytes durably received
    PATCH  /uploads/{id}            Upload-Offset: n, body = the next bytes
    POST   /uploads/{id}/complete   verify (size, optional SHA-256) and commit
    DELETE /uploads/{id}            abort and discard

Bytes land in ``.nas-resumable-<id>.part`` inside the destination directory
(the managed directory for the ``files`` target, the uploader's directory for
the ``public`` target), so the final ``os.replace`` never crosses a
filesystem. Each chunk is fsync'ed before ``received_bytes`` advances, so the
offset the server reports is always backed by data on disk. A chunk that is
cut off by a client disconnect keeps the bytes that did arrive; a chunk that
breaks a limit is rolled back to its starting offset.

Completion goes through the same commit helpers as single-shot uploads
(``files.commit_staged`` / ``public_uploads.commit_staged``): the jail and
overwrite rules are re-checked and, for the public target, the quota is
checked and reserved under the row lock at that point. The quota check at
creation time is only advisory, to fail early.

A per-upload in-process lock serialises PATCH/complete/abort for one id; a
concurrent second request is answered with 409 rather than queued.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from app.core.config import Settings
from app.core.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    QuotaExceededError,
)
from app.db.base import utcnow_naive
from app.models import ApiToken, PublicUploadFile, UploadSession, User
from app.models.upload_session import TARGET_FILES, TARGET_PUBLIC
from app.schemas.files import FileEntry
from app.services import audit, files, public_uploads, sync

_HASH_CHUNK = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@contextlib.contextmanager
def _upload_lock(upload_id: str) -> Iterator[None]:
    with _locks_guard:
        lock = _locks.setdefault(upload_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise ConflictError(
            "Another request for this upload is in progress", details={"id": upload_id}
        )
    try:
        yield
    finally:
        lock.release()
        with _locks_guard:
            if _locks.get(upload_id) is lock and not lock.locked():
                _locks.pop(upload_id, None)


def _normalize_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    digest = value.strip().lower()
    if not _SHA256_RE.match(digest):
        raise BadRequestError("sha256 must be 64 hexadecimal characters")
    return digest


def staged_path(settings: Settings, row: UploadSession) -> Path:
    """Where the bytes of ``row`` are accumulated."""
    if row.target == TARGET_FILES:
        root = files.resolve_root(settings, row.root)
        directory = files.resolve_in_root(root, row.rel_dir)
    else:
        directory = public_uploads.uploader_dir(settings, row.root)
    return directory / f"{files.RESUMABLE_TMP_PREFIX}{row.id}.part"


def _expiry(settings: Settings) -> timedelta:
    return timedelta(hours=settings.resumable_upload_ttl_hours)


def create_upload(
    session: Session,
    settings: Settings,
    *,
    user: User,
    token: ApiToken | None,
    target: str,
    filename: str,
    size: int,
    root: str | None = None,
    path: str = "",
    overwrite: bool = False,
    sha256: str | None = None,
    context: dict[str, str | None] | None = None,
) -> UploadSession:
    """Validate the destination up front and open an upload session.

    The ``files`` target needs ``files:write`` (checked by the router); the
    ``public`` target is charged to the token's quota (``token:<id>``) or the
    user's (``user:<name>``).
    """
    sweep_expired(session, settings)
    if size < 0:
        raise BadRequestError("size must not be negative")
    digest = _normalize_sha256(sha256)
    open_count = int(
        session.execute(
            select(func.count(UploadSession.id)).where(UploadSession.user_id == user.id)
        ).scalar_one()
    )
    if open_count >= settings.resumable_max_open_per_user:
        raise ConflictError(
            "Too many open uploads; complete or abort one first",
            details={"max_open": settings.resumable_max_open_per_user},
        )

    if target == TARGET_FILES:
        if size > settings.max_upload_bytes:
            raise BadRequestError(
                f"Upload exceeds the limit of {settings.max_upload_bytes} bytes",
                details={"max_bytes": settings.max_upload_bytes},
            )
        managed_root, final = files.prepare_upload_target(
            settings,
            root_str=root or "",
            rel_dir=path,
            filename=filename,
            overwrite=overwrite,
        )
        rel_dir = str(final.parent.relative_to(managed_root))
        row = UploadSession(
            target=TARGET_FILES,
            root=str(managed_root),
            rel_dir="" if rel_dir == "." else rel_dir,
            filename=final.name,
        )
    elif target == TARGET_PUBLIC:
        key = (
            public_uploads.uploader_key_for_token(token.id)
            if token is not None
            else public_uploads.uploader_key_for_user(user.username)
        )
        key, name = public_uploads.validate_upload_request(
            session, settings, uploader_key=key, filename=filename
        )
        cap = settings.public_upload_max_file_bytes
        if size > cap:
            raise BadRequestError(
                f"File exceeds the folder limit of {cap} bytes", details={"max_file_bytes": cap}
            )
        remaining = public_uploads.remaining_quota(session, settings, key)
        if size > remaining:
            raise QuotaExceededError(
                "Upload rejected: the per-uploader quota is exhausted",
                details={"remaining_bytes": remaining, "attempted_bytes": size},
            )
        public_uploads.uploader_dir(settings, key).mkdir(parents=True, exist_ok=True, mode=0o750)
        row = UploadSession(target=TARGET_PUBLIC, root=key, rel_dir="", filename=name)
    else:
        raise BadRequestError("target must be 'files' or 'public'")

    now = utcnow_naive()
    row.id = uuid.uuid4().hex
    row.user_id = user.id
    row.token_id = token.id if token is not None else None
    row.total_bytes = size
    row.received_bytes = 0
    row.overwrite = overwrite
    row.sha256 = digest
    row.created_at = now
    row.updated_at = now
    row.expires_at = now + _expiry(settings)
    staged = staged_path(settings, row)
    staged.touch(mode=0o640, exist_ok=False)
    try:
        session.add(row)
        audit.record_audit_event(
            session,
            event=audit.UPLOAD_SESSION_CREATED,
            category=audit.CATEGORY_UPLOAD,
            actor=user,
            target_type="upload",
            target_id=row.id,
            details={
                "target": row.target,
                "root": row.root,
                "path": row.rel_dir,
                "filename": row.filename,
                "total_bytes": size,
            },
            **audit.request_context(context),
            commit=False,
        )
        session.commit()
    except Exception:
        staged.unlink(missing_ok=True)
        session.rollback()
        raise
    return row


def get_owned(session: Session, upload_id: str, user: User) -> UploadSession:
    """The caller's live upload, or 404 (other users' ids are indistinguishable)."""
    row = session.get(UploadSession, upload_id)
    if row is None or row.user_id != user.id or row.is_expired:
        raise NotFoundError("Upload not found or expired", details={"id": upload_id})
    return row


def _sync_received_with_disk(session: Session, row: UploadSession, staged: Path) -> None:
    """Never claim more than the disk holds (e.g. after a crash or tampering)."""
    try:
        on_disk = staged.stat().st_size
    except FileNotFoundError:
        session.delete(row)
        session.commit()
        raise NotFoundError(
            "Upload data is gone; start a new upload", details={"id": row.id}
        ) from None
    if on_disk < row.received_bytes:
        row.received_bytes = on_disk
        session.commit()


async def append_chunk(
    session: Session,
    settings: Settings,
    row: UploadSession,
    *,
    offset: int,
    chunks: AsyncIterator[bytes],
) -> UploadSession:
    """Append the request body at ``offset`` (which must equal the received size)."""
    with _upload_lock(row.id):
        staged = staged_path(settings, row)
        _sync_received_with_disk(session, row, staged)
        if offset != row.received_bytes:
            raise ConflictError(
                "Upload-Offset does not match the received size",
                details={"offset": row.received_bytes},
            )
        budget = min(settings.resumable_chunk_max_bytes, row.total_bytes - offset)
        written = 0
        with staged.open("r+b") as out:
            # Drop any bytes past the acknowledged offset (a chunk whose
            # acknowledgement was lost) so the retry overwrites them cleanly.
            out.truncate(offset)
            out.seek(offset)
            try:
                async for piece in chunks:
                    if not piece:
                        continue
                    written += len(piece)
                    if written > budget:
                        raise BadRequestError(
                            "Chunk exceeds the chunk limit or the declared size",
                            details={
                                "chunk_max_bytes": settings.resumable_chunk_max_bytes,
                                "remaining_bytes": row.total_bytes - offset,
                            },
                        )
                    out.write(piece)
            except ClientDisconnect:
                # Keep what arrived: that is the point of a resumable upload.
                out.flush()
                os.fsync(out.fileno())
                written = out.tell() - offset
                row.received_bytes = offset + written
                row.expires_at = utcnow_naive() + _expiry(settings)
                session.commit()
                raise
            except AppError:
                out.truncate(offset)
                raise
            out.flush()
            os.fsync(out.fileno())
        row.received_bytes = offset + written
        row.expires_at = utcnow_naive() + _expiry(settings)
        session.commit()
        return row


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _discard(session: Session, row: UploadSession, staged: Path) -> None:
    staged.unlink(missing_ok=True)
    session.delete(row)
    session.commit()


def complete_upload(
    session: Session,
    settings: Settings,
    row: UploadSession,
    *,
    actor: User,
    sha256: str | None = None,
    context: dict[str, str | None] | None = None,
) -> FileEntry | PublicUploadFile:
    """Verify and commit a fully received upload into its destination."""
    with _upload_lock(row.id):
        staged = staged_path(settings, row)
        _sync_received_with_disk(session, row, staged)
        if row.received_bytes != row.total_bytes:
            raise ConflictError(
                "Upload is incomplete",
                details={"offset": row.received_bytes, "total_bytes": row.total_bytes},
            )
        expected = _normalize_sha256(sha256) or row.sha256
        if expected is not None and _file_sha256(staged) != expected:
            _discard(session, row, staged)
            raise BadRequestError(
                "Checksum mismatch; the upload was discarded", details={"id": row.id}
            )

        upload_id, size = row.id, row.total_bytes
        audit.record_audit_event(
            session,
            event=audit.UPLOAD_SESSION_COMPLETED,
            category=audit.CATEGORY_UPLOAD,
            actor=actor,
            target_type="upload",
            target_id=upload_id,
            details={"target": row.target, "filename": row.filename, "total_bytes": size},
            **audit.request_context(context),
            commit=False,
        )
        session.delete(row)
        try:
            if row.target == TARGET_FILES:
                root = files.resolve_root(settings, row.root)
                final = files.resolve_in_root(root, row.rel_dir) / row.filename
                if final.exists() and not row.overwrite:
                    # Keep the bytes: the client may abort or retry later.
                    session.rollback()
                    raise ConflictError(
                        "A file or directory with that name already exists",
                        details={"name": row.filename},
                    )
                return files.commit_staged(
                    session,
                    root=root,
                    target=final,
                    staged=staged,
                    size=size,
                    overwrite=row.overwrite,
                    actor=actor,
                    context=context,
                )
            return public_uploads.commit_staged(
                session,
                settings,
                uploader_key=row.root,
                filename=row.filename,
                staged=staged,
                size=size,
                ip_address=(context or {}).get("ip_address"),
                actor=actor,
                context=context,
            )
        except Exception:
            session.rollback()
            if not staged.exists():
                # The commit helper discarded the data (quota, jail...):
                # the session cannot be resumed any more.
                leftover = session.get(UploadSession, upload_id)
                if leftover is not None:
                    session.delete(leftover)
                    session.commit()
            raise


def abort_upload(
    session: Session,
    settings: Settings,
    row: UploadSession,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    with _upload_lock(row.id):
        try:
            staged: Path | None = staged_path(settings, row)
        except AppError:
            staged = None  # the root was removed from configuration
        if staged is not None:
            staged.unlink(missing_ok=True)
        audit.record_audit_event(
            session,
            event=audit.UPLOAD_SESSION_ABORTED,
            category=audit.CATEGORY_UPLOAD,
            actor=actor,
            target_type="upload",
            target_id=row.id,
            details={"target": row.target, "received_bytes": row.received_bytes},
            **audit.request_context(context),
            commit=False,
        )
        session.delete(row)
        session.commit()


def sweep_expired(session: Session, settings: Settings) -> dict[str, int]:
    """Discard expired upload sessions and prune the sync journal."""
    now = utcnow_naive()
    rows = list(
        session.execute(select(UploadSession).where(UploadSession.expires_at <= now))
        .scalars()
        .all()
    )
    for row in rows:
        # An unreachable root leaves nothing we can remove; drop the row anyway.
        with contextlib.suppress(AppError, OSError):
            staged_path(settings, row).unlink(missing_ok=True)
        session.delete(row)
    if rows:
        audit.record_audit_event(
            session,
            event=audit.UPLOAD_SESSION_EXPIRED,
            category=audit.CATEGORY_UPLOAD,
            details={"count": len(rows), "ids": [row.id for row in rows][:50]},
            commit=False,
        )
    session.commit()
    pruned = sync.prune(session, settings)
    return {"expired_uploads": len(rows), "pruned_changes": pruned}
