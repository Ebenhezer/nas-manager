"""Uploads into (and deletions from) a user's personal library.

This is the server side of phone photo backup: a client asks which hashes
the server already has (``sync.lookup``), then uploads the rest here, either
in one multipart request or through the resumable ``media`` target. Files
land in ``YYYY/MM`` folders (from the client's ``taken_at`` when given) and
are never overwritten: a colliding name becomes ``name (1).ext``. The final
placement uses a hard link where the filesystem supports it, so two
concurrent uploads of the same name cannot overwrite each other.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, NotFoundError
from app.models import MediaItem, MediaLibrary, User
from app.services import audit, files, public_uploads
from app.services.media import indexer, libraries
from app.services.media.libraries import MediaActor

_CHUNK_SIZE = 1024 * 1024
_MAX_SUBFOLDER_DEPTH = 8


def clean_filename(filename: str) -> str:
    name = files.sanitize_entry_name(filename)
    if name.startswith("."):
        raise BadRequestError("Hidden file names are not accepted")
    if indexer.classify(name) is None:
        raise BadRequestError(
            "Not a supported photo or video type",
            details={"image": sorted(indexer.IMAGE_TYPES), "video": sorted(indexer.VIDEO_TYPES)},
        )
    return name


def subfolder_parts(subfolder: str | None, taken_at: datetime | None) -> list[str]:
    """Validated folder components; ``YYYY/MM`` of ``taken_at`` (or now) by default."""
    if subfolder is None or not subfolder.strip():
        moment = taken_at or datetime.now(UTC)
        return [f"{moment.year:04d}", f"{moment.month:02d}"]
    parts = [p for p in subfolder.strip().strip("/").split("/") if p]
    if len(parts) > _MAX_SUBFOLDER_DEPTH:
        raise BadRequestError(f"At most {_MAX_SUBFOLDER_DEPTH} folder levels")
    cleaned = []
    for part in parts:
        safe = files.sanitize_entry_name(part)
        if safe.startswith("."):
            raise BadRequestError("Hidden folders are not accepted")
        cleaned.append(safe)
    return cleaned


def target_directory(settings: Settings, library: MediaLibrary, parts: list[str]) -> Path:
    base = libraries.library_path(settings, library)
    directory = files.resolve_in_root(base, "/".join(parts)) if parts else base
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    return directory


def personal_folder(settings: Settings, username: str) -> Path:
    """The personal library folder of ``username`` (resumable staging)."""
    return files.resolve_in_root(settings.resolved_media_dir, username)


def _place(staged: Path, directory: Path, name: str) -> Path:
    """Move ``staged`` to a free name in ``directory`` without ever overwriting."""
    while True:
        final = public_uploads.unique_path(directory, name)
        try:
            os.link(staged, final)
        except FileExistsError:
            continue  # lost a race for this name: pick the next one
        except OSError:
            # No hard links here (FAT, some network filesystems).
            os.replace(staged, final)
            return final
        staged.unlink(missing_ok=True)
        return final


def commit_staged(
    session: Session,
    settings: Settings,
    *,
    library: MediaLibrary,
    directory: Path,
    filename: str,
    staged: Path,
    size: int,
    taken_at: datetime | None,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> MediaItem:
    """Place a fully written temp file, index it, audit and journal it."""
    base = libraries.library_path(settings, library)
    try:
        if not directory.resolve().is_relative_to(base):
            raise BadRequestError("Path escapes the library")
        final = _place(staged, directory, filename)
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    if taken_at is not None:
        stamp = taken_at.replace(tzinfo=taken_at.tzinfo or UTC).timestamp()
        with contextlib.suppress(OSError):
            os.utime(final, (stamp, stamp))
    rel = final.relative_to(base).as_posix()
    stale = session.execute(
        select(MediaItem).where(MediaItem.library_id == library.id, MediaItem.rel_path == rel)
    ).scalar_one_or_none()
    item = indexer.index_file(session, settings, library, base, final, final.lstat(), stale)
    if item is None:  # pragma: no cover - clean_filename checked the type
        raise BadRequestError("Not a supported photo or video type")
    audit.record_audit_event(
        session,
        event=audit.MEDIA_ITEM_UPLOADED,
        category=audit.CATEGORY_MEDIA,
        actor=actor,
        target_type="media_item",
        target_id=item.id,
        details={"library_id": library.id, "path": rel, "size": size, "sha256": item.sha256},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return item


def upload(
    session: Session,
    settings: Settings,
    actor: MediaActor,
    *,
    filename: str,
    source: BinaryIO,
    subfolder: str | None,
    taken_at: datetime | None,
    context: dict[str, str | None] | None = None,
) -> MediaItem:
    library = libraries.ensure_personal_library(session, settings, actor.user)
    libraries.require_writable(library, actor)
    name = clean_filename(filename)
    directory = target_directory(settings, library, subfolder_parts(subfolder, taken_at))
    temporary = directory / f".nas-upload-{uuid.uuid4().hex}.part"
    total = 0
    try:
        with temporary.open("wb") as out:
            while chunk := source.read(_CHUNK_SIZE):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise BadRequestError(
                        f"Upload exceeds the limit of {settings.max_upload_bytes} bytes",
                        details={"max_bytes": settings.max_upload_bytes},
                    )
                out.write(chunk)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return commit_staged(
        session,
        settings,
        library=library,
        directory=directory,
        filename=name,
        staged=temporary,
        size=total,
        taken_at=taken_at,
        actor=actor.user,
        context=context,
    )


def delete_item(
    session: Session,
    settings: Settings,
    actor: MediaActor,
    item: MediaItem,
    *,
    context: dict[str, str | None] | None = None,
) -> None:
    """Delete a photo/video from the owner's personal library (file and index)."""
    library = session.get(MediaLibrary, item.library_id)
    if library is None:  # pragma: no cover - cascades make this unreachable
        raise NotFoundError("Media item not found")
    libraries.require_writable(library, actor)
    path = libraries.item_path(settings, library, item)
    if path.is_file() and not path.is_symlink():
        path.unlink()
    details = {"library_id": library.id, "path": item.rel_path, "size": item.size}
    item_id = item.id
    indexer.remove_item(session, settings, item)
    audit.record_audit_event(
        session,
        event=audit.MEDIA_ITEM_DELETED,
        category=audit.CATEGORY_MEDIA,
        actor=actor.user,
        target_type="media_item",
        target_id=item_id,
        details=details,
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
