"""Public upload folder service (Phase 4).

A dedicated drop folder that clients may write to without an account
(``public uploads``). It is *upload-only*: there is no public listing,
download or search endpoint. Usage is accounted per uploader — an
authenticated request counts against ``user:<username>``, an anonymous one
against ``ip:<client address>`` — and enforced against a per-uploader quota.

Atomicity / concurrency
    The quota check and the file reservation live in the same database
    transaction that records the accepted file. Every upload first locks its
    uploader's quota row (an ``UPDATE`` of the row itself) and then re-reads
    the row under that lock: on SQLite (WAL, single writer) and on PostgreSQL
    (row lock) no other uploader transaction can commit a change for the same
    key while the lock is held, so concurrent uploads cannot collectively
    exceed the quota. A failed step rolls the reservation back and removes
    the temporary file, so a rejected upload never consumes quota.

Filesystem layout
    ``<NAS_PUBLIC_UPLOAD_DIR>/<sha256(uploader_key)[:24]>/<filename>`` —
    one digest-named subdirectory per uploader (collision-safe, leaks nothing
    about the key) and sanitized, auto-uniquified filenames inside it.
    Uploads stream to a ``.nas-upload-*.part`` temp file and are atomically
    renamed, so a partial upload never appears under the final name and the
    per-file cap cannot be bypassed.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    QuotaExceededError,
)
from app.models import PublicUploadFile, PublicUploadQuota, User
from app.schemas.public_uploads import MAX_FILENAME_LENGTH, UploaderSummary
from app.services import audit
from app.services.files import sanitize_entry_name
from app.services.settings_service import public_uploads_enabled

_CHUNK_SIZE = 1024 * 1024
_UPLOAD_TMP_PREFIX = ".nas-upload-"
_DIR_HASH_LENGTH = 24
_USER_PREFIX = "user:"
_IP_PREFIX = "ip:"


def uploader_key_for_user(username: str) -> str:
    """Quota key for an authenticated uploader (a session-carrying client)."""
    return f"{_USER_PREFIX}{username}"


def uploader_key_for_ip(ip_address: str | None) -> str:
    """Quota key for an anonymous uploader, derived from the client address."""
    return f"{_IP_PREFIX}{ip_address or 'unknown'}"


def validate_uploader_key(key: str) -> str:
    """Reject malformed uploader keys (server-generated; defence in depth)."""
    if not key or len(key) > 128 or not key.isascii():
        raise BadRequestError("Invalid uploader identifier", details={"uploader_key": key})
    if not all(char.isalnum() or char in "._:-" for char in key):
        raise BadRequestError("Invalid uploader identifier", details={"uploader_key": key})
    return key


def uploader_dir(settings: Settings, uploader_key: str) -> Path:
    """The (filesystem-safe, collision-resistant) per-uploader directory."""
    digest = hashlib.sha256(uploader_key.encode("utf-8")).hexdigest()[:_DIR_HASH_LENGTH]
    return settings.resolved_public_upload_dir / digest


def _get_or_create_quota(session: Session, settings: Settings, key: str) -> PublicUploadQuota:
    row = session.get(PublicUploadQuota, key)
    if row is None:
        row = PublicUploadQuota(
            uploader_key=key,
            quota_bytes=settings.public_upload_default_quota_bytes,
            used_bytes=0,
        )
        session.add(row)
        session.flush()
    return row


def _truncate_to_fit(text: str, budget: int) -> str:
    """Cut ``text`` so it fits in at most ``budget`` UTF-8 bytes."""
    budget = max(budget, 0)
    while len(text.encode("utf-8")) > budget and text:
        text = text[:-1]
    return text


def _unique_path(directory: Path, name: str) -> Path:
    """Return a free basename inside ``directory``, never overwriting.

    The first collision becomes ``stem (1).ext``, then ``(2)`` and so on.
    Names that would exceed the 255-byte filesystem limit are shortened
    (the stem is truncated; the extension is preserved).
    """
    candidate = directory / name
    if not candidate.exists():
        return candidate
    dot = name.rfind(".")
    stem, suffix = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    counter = 1
    while True:
        label = f" ({counter})"
        budget = 255 - len(label.encode("utf-8")) - len(suffix.encode("utf-8"))
        candidate = directory / f"{_truncate_to_fit(stem, budget)}{label}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def upload(
    session: Session,
    settings: Settings,
    *,
    uploader_key: str,
    filename: str,
    source: BinaryIO,
    ip_address: str | None = None,
    actor: User | None = None,
    context: dict[str, str | None] | None = None,
) -> PublicUploadFile:
    """Accept one public upload against the caller's quota.

    The quota is checked and reserved atomically with the file's database
    record (see the module docstring); on any failure the reservation is
    rolled back and the temporary file removed.
    """
    key = validate_uploader_key(uploader_key)
    if not public_uploads_enabled(session, settings):
        raise ForbiddenError("Public uploads are currently disabled")
    name = sanitize_entry_name(filename)
    if len(name.encode("utf-8")) > MAX_FILENAME_LENGTH:
        raise BadRequestError("Filename is too long")

    directory = uploader_dir(settings, key)
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = directory / f"{_UPLOAD_TMP_PREFIX}{uuid.uuid4().hex}.part"
    total = 0
    try:
        with temporary.open("wb") as out:
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > settings.public_upload_max_file_bytes:
                    cap = settings.public_upload_max_file_bytes
                    raise BadRequestError(
                        f"File exceeds the folder limit of {cap} bytes",
                        details={"max_file_bytes": cap},
                    )
                out.write(chunk)

        # Quota check + reservation in one transaction. Touching the uploader
        # row with UPDATE takes the writer lock for this key (SQLite: the
        # single-writer WAL lock; PostgreSQL: the row lock), then refresh
        # re-reads the committed state under the lock so two concurrent
        # uploads cannot both pass a quota that only one of them fits in.
        quota = _get_or_create_quota(session, settings, key)
        session.execute(
            update(PublicUploadQuota)
            .where(PublicUploadQuota.uploader_key == key)
            .values(used_bytes=PublicUploadQuota.used_bytes)
        )
        session.flush()
        session.refresh(quota)
        if quota.used_bytes + total > quota.quota_bytes:
            # The quota check runs *inside* the reservation transaction; record
            # the rejection with its own commit so the failure is auditable
            # even though the reservation is rolled back below.
            ctx = context or {}
            audit.record_audit_event(
                session,
                event=audit.PUBLIC_UPLOAD_REJECTED,
                category=audit.CATEGORY_PUBLIC_UPLOAD,
                outcome=audit.OUTCOME_FAILURE,
                actor=actor,
                target_type="file",
                target_id=f"{key}/{name}",
                details={
                    "reason": "quota",
                    "uploader_key": key,
                    "filename": name,
                    "attempted_bytes": total,
                    "quota_bytes": quota.quota_bytes,
                    "used_bytes": quota.used_bytes,
                },
                ip_address=ip_address,
                user_agent=ctx.get("user_agent"),
                request_id=ctx.get("request_id"),
                commit=True,
            )
            raise QuotaExceededError(
                "Upload rejected: the per-uploader quota is exhausted",
                details={
                    "quota_bytes": quota.quota_bytes,
                    "used_bytes": quota.used_bytes,
                    "attempted_bytes": total,
                },
            )

        final = _unique_path(directory, name)
        os.replace(temporary, final)
        row = PublicUploadFile(
            uploader_key=key,
            filename=final.name,
            size_bytes=total,
            ip_address=ip_address,
        )
        session.add(row)
        quota.used_bytes += total
        ctx = context or {}
        audit.record_audit_event(
            session,
            event=audit.PUBLIC_UPLOAD_UPLOADED,
            category=audit.CATEGORY_PUBLIC_UPLOAD,
            actor=actor,
            target_type="file",
            target_id=f"{key}/{final.name}",
            details={
                "uploader_key": key,
                "filename": final.name,
                "size": total,
                "quota_bytes": quota.quota_bytes,
                "used_bytes": quota.used_bytes,
            },
            ip_address=ip_address,
            user_agent=ctx.get("user_agent"),
            request_id=ctx.get("request_id"),
            commit=False,
        )
        session.commit()
        return row
    except Exception:
        temporary.unlink(missing_ok=True)
        session.rollback()
        raise


def summary_for(session: Session, row: PublicUploadQuota) -> UploaderSummary:
    """The API summary of one quota row (including its live file count)."""
    count = session.execute(
        select(func.count(PublicUploadFile.id)).where(
            PublicUploadFile.uploader_key == row.uploader_key
        )
    ).scalar_one()
    return UploaderSummary(
        uploader_key=row.uploader_key,
        quota_bytes=row.quota_bytes,
        used_bytes=row.used_bytes,
        file_count=int(count),
        over_quota=row.used_bytes > row.quota_bytes,
    )


def list_uploaders(
    session: Session, *, limit: int = 100, offset: int = 0
) -> tuple[list[UploaderSummary], int]:
    """Paginated per-uploader summaries (newest activity first)."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    total = session.execute(select(func.count(PublicUploadQuota.uploader_key))).scalar_one()
    rows = (
        session.execute(
            select(PublicUploadQuota)
            .order_by(PublicUploadQuota.updated_at.desc(), PublicUploadQuota.uploader_key)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return [summary_for(session, row) for row in rows], int(total)


def status(
    session: Session, settings: Settings, *, limit: int = 100, offset: int = 0
) -> dict[str, object]:
    """Full admin view of the drop folder (policy + per-uploader usage)."""
    uploaders, total_uploaders = list_uploaders(session, limit=limit, offset=offset)
    total_files = session.execute(select(func.count(PublicUploadFile.id))).scalar_one()
    total_used = session.execute(
        select(func.coalesce(func.sum(PublicUploadFile.size_bytes), 0))
    ).scalar_one()
    return {
        "enabled": public_uploads_enabled(session, settings),
        "dir": str(settings.resolved_public_upload_dir),
        "max_file_bytes": settings.public_upload_max_file_bytes,
        "default_quota_bytes": settings.public_upload_default_quota_bytes,
        "total_uploaders": int(total_uploaders),
        "total_files": int(total_files),
        "total_used_bytes": int(total_used),
        "uploaders": uploaders,
        "limit": limit,
        "offset": offset,
    }


def list_files(
    session: Session,
    *,
    uploader_key: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[PublicUploadFile], int]:
    """Paginated file records, newest first; optionally filtered per uploader."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    stmt = select(PublicUploadFile)
    total_stmt = select(func.count(PublicUploadFile.id))
    if uploader_key is not None:
        key = validate_uploader_key(uploader_key)
        stmt = stmt.where(PublicUploadFile.uploader_key == key)
        total_stmt = total_stmt.where(PublicUploadFile.uploader_key == key)
    total = session.execute(total_stmt).scalar_one()
    rows = (
        session.execute(
            stmt.order_by(PublicUploadFile.created_at.desc(), PublicUploadFile.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


def quota_row_or_404(session: Session, uploader_key: str) -> PublicUploadQuota:
    key = validate_uploader_key(uploader_key)
    row = session.get(PublicUploadQuota, key)
    if row is None:
        raise NotFoundError("Unknown uploader", details={"uploader_key": key})
    return row


def set_quota(
    session: Session,
    *,
    uploader_key: str,
    quota_bytes: int,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> PublicUploadQuota:
    """Raise or lower an uploader's quota (audited; lowering below current use
    simply blocks new uploads until usage drops)."""
    row = quota_row_or_404(session, uploader_key)
    previous = row.quota_bytes
    row.quota_bytes = quota_bytes
    ctx = context or {}
    audit.record_audit_event(
        session,
        event=audit.PUBLIC_UPLOAD_QUOTA_CHANGED,
        category=audit.CATEGORY_PUBLIC_UPLOAD,
        actor=actor,
        target_type="uploader",
        target_id=row.uploader_key,
        details={
            "uploader_key": row.uploader_key,
            "quota_bytes": quota_bytes,
            "previous_quota_bytes": previous,
            "used_bytes": row.used_bytes,
        },
        ip_address=ctx.get("ip_address"),
        user_agent=ctx.get("user_agent"),
        request_id=ctx.get("request_id"),
        commit=False,
    )
    session.commit()
    return row


def file_row_or_404(session: Session, file_id: int) -> PublicUploadFile:
    row = session.get(PublicUploadFile, file_id)
    if row is None:
        raise NotFoundError("Upload not found", details={"file_id": file_id})
    return row


def _stored_path(settings: Settings, row: PublicUploadFile) -> Path:
    """The absolute path of a stored file, verified to stay inside the root."""
    root = settings.resolved_public_upload_dir.resolve(strict=False)
    path = (uploader_dir(settings, row.uploader_key) / row.filename).resolve(strict=False)
    if not path.is_relative_to(root):
        raise BadRequestError("Stored path escapes the upload root")
    return path


def delete_file(
    session: Session,
    settings: Settings,
    *,
    file_id: int,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    """Delete one stored file and decrement its uploader's usage (audited).

    The file is removed from disk first; the accounting row is deleted and
    the usage ledger decremented only after the file is gone, so a failed
    unlink never leaves the ledger below what exists on disk.
    """
    row = file_row_or_404(session, file_id)
    path = _stored_path(settings, row)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - permissions/IO on the host
        raise BadRequestError(
            "Could not remove the stored file", details={"file_id": file_id}
        ) from exc

    ctx = context or {}
    audit.record_audit_event(
        session,
        event=audit.PUBLIC_UPLOAD_FILE_DELETED,
        category=audit.CATEGORY_PUBLIC_UPLOAD,
        actor=actor,
        target_type="file",
        target_id=str(file_id),
        details={
            "uploader_key": row.uploader_key,
            "filename": row.filename,
            "size": row.size_bytes,
        },
        ip_address=ctx.get("ip_address"),
        user_agent=ctx.get("user_agent"),
        request_id=ctx.get("request_id"),
        commit=False,
    )
    quota = session.get(PublicUploadQuota, row.uploader_key)
    if quota is not None:
        quota.used_bytes = max(0, quota.used_bytes - row.size_bytes)
    session.delete(row)
    session.commit()


def cleanup(
    session: Session,
    settings: Settings,
    *,
    older_than_days: int | None = None,
    actor: User | None = None,
    context: dict[str, str | None] | None = None,
) -> dict[str, object]:
    """Remove stale temp files, optionally purge files older than N days, and
    reconcile every uploader's usage ledger with the remaining file records.

    Returns counts for reporting and audit. Temp removal never touches
    tracked files; the age purges only files that have a database record.
    """
    root = settings.resolved_public_upload_dir
    removed_temp = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if name.startswith(_UPLOAD_TMP_PREFIX):
                target = Path(dirpath) / name
                if target.is_file():
                    try:
                        target.unlink()
                        removed_temp += 1
                    except OSError:  # pragma: no cover - host I/O during race
                        continue

    removed_files = 0
    removed_bytes = 0
    if older_than_days is not None:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=older_than_days)
        rows = (
            session.execute(select(PublicUploadFile).where(PublicUploadFile.created_at < cutoff))
            .scalars()
            .all()
        )
        for row in rows:
            try:
                _stored_path(settings, row).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - host I/O during race
                continue
            session.delete(row)
            removed_files += 1
            removed_bytes += row.size_bytes

    # The quota rows are the only thing left to reconcile: recompute their
    # ledger from the remaining file records (NULL-safe: 0 bytes when empty).
    used_by_key = dict(
        session.execute(
            select(
                PublicUploadFile.uploader_key,
                func.coalesce(func.sum(PublicUploadFile.size_bytes), 0),
            ).group_by(PublicUploadFile.uploader_key)
        ).all()
    )
    reconciled = 0
    for quota in session.execute(select(PublicUploadQuota)).scalars().all():
        quota.used_bytes = int(used_by_key.get(quota.uploader_key, 0))
        reconciled += 1

    ctx = context or {}
    audit.record_audit_event(
        session,
        event=audit.PUBLIC_UPLOAD_CLEANUP,
        category=audit.CATEGORY_PUBLIC_UPLOAD,
        actor=actor,
        target_type="folder",
        target_id="public_uploads",
        details={
            "removed_temp_files": removed_temp,
            "removed_files": removed_files,
            "removed_bytes": removed_bytes,
            "older_than_days": older_than_days,
            "reconciled_uploaders": reconciled,
        },
        ip_address=ctx.get("ip_address"),
        user_agent=ctx.get("user_agent"),
        request_id=ctx.get("request_id"),
        commit=False,
    )
    session.commit()
    return {
        "removed_temp_files": removed_temp,
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "reconciled_uploaders": reconciled,
        "message": "Cleanup finished",
    }
