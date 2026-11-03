"""Media libraries: records, access rules and path resolution.

Access model:

* A *personal* library belongs to one user and only that user can browse
  it, upload to it or delete from it. Administrators (``media:admin``) see
  that it exists (name, counts, scan state) but cannot browse its photos.
* A *shared* library is registered by an administrator over an existing
  folder inside a managed file root. Members of the groups it is granted to
  (and administrators) can browse it; nobody writes to it through the
  gallery - it is maintained with the file manager, Samba or remote sync.
* Every item lookup is filtered by the caller's browsable libraries, so an
  item the caller may not see is indistinguishable from one that does not
  exist (404, never 403).

Paths are recomputed and re-validated against the jail on every use, so a
library whose root is removed from configuration, or a folder replaced by a
symlink pointing elsewhere, is refused rather than followed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, false, func, insert, or_, select, true
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.db.base import utcnow_naive
from app.models import Group, MediaItem, MediaLibrary, User, media_library_groups, user_groups
from app.models.media import (
    LIBRARY_PERSONAL,
    LIBRARY_SHARED,
    PERSONAL_NAME_PREFIX,
    SCAN_IDLE,
    SCAN_QUEUED,
    THUMB_PENDING,
)
from app.services import audit, files

PERMISSION_READ = "media:read"
PERMISSION_WRITE = "media:write"
PERMISSION_ADMIN = "media:admin"


@dataclass(frozen=True)
class MediaActor:
    """The caller as the media services see it."""

    user: User
    permissions: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return PERMISSION_ADMIN in self.permissions

    @property
    def can_write(self) -> bool:
        return PERMISSION_WRITE in self.permissions


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def library_path(settings: Settings, library: MediaLibrary) -> Path:
    """The library's folder, re-validated against its jail."""
    if library.is_personal:
        return files.resolve_in_root(settings.resolved_media_dir, library.rel_path)
    root = files.resolve_root(settings, library.root)
    return files.resolve_in_root(root, library.rel_path)


def item_path(settings: Settings, library: MediaLibrary, item: MediaItem) -> Path:
    """The item's file, re-validated against the library folder."""
    return files.resolve_in_root(library_path(settings, library), item.rel_path)


# ---------------------------------------------------------------------------
# Access rules
# ---------------------------------------------------------------------------


def browsable_clause(actor: MediaActor) -> ColumnElement[bool]:
    """SQL condition on ``MediaLibrary`` for libraries ``actor`` may browse."""
    if PERMISSION_READ not in actor.permissions:
        return false()
    own = and_(MediaLibrary.kind == LIBRARY_PERSONAL, MediaLibrary.owner_user_id == actor.user.id)
    if actor.is_admin:
        shared: ColumnElement[bool] = MediaLibrary.kind == LIBRARY_SHARED
    else:
        member_groups = select(user_groups.c.group_id).where(user_groups.c.user_id == actor.user.id)
        granted = select(media_library_groups.c.library_id).where(
            media_library_groups.c.group_id.in_(member_groups)
        )
        shared = and_(MediaLibrary.kind == LIBRARY_SHARED, MediaLibrary.id.in_(granted))
    return and_(MediaLibrary.enabled.is_(True), or_(own, shared))


def browsable_libraries(session: Session, actor: MediaActor) -> list[MediaLibrary]:
    return list(
        session.execute(
            select(MediaLibrary).where(browsable_clause(actor)).order_by(MediaLibrary.name)
        )
        .scalars()
        .all()
    )


def visible_libraries(session: Session, actor: MediaActor) -> list[MediaLibrary]:
    """Browsable libraries, plus every library's record for administrators."""
    clause = true() if actor.is_admin else browsable_clause(actor)
    return list(
        session.execute(select(MediaLibrary).where(clause).order_by(MediaLibrary.name))
        .scalars()
        .all()
    )


def is_browsable(session: Session, actor: MediaActor, library_id: int) -> bool:
    found = session.execute(
        select(MediaLibrary.id).where(MediaLibrary.id == library_id, browsable_clause(actor))
    ).first()
    return found is not None


def get_browsable_library(session: Session, actor: MediaActor, library_id: int) -> MediaLibrary:
    library = session.execute(
        select(MediaLibrary).where(MediaLibrary.id == library_id, browsable_clause(actor))
    ).scalar_one_or_none()
    if library is None:
        raise NotFoundError("Library not found", details={"library_id": library_id})
    return library


def get_library_for_admin(session: Session, library_id: int) -> MediaLibrary:
    library = session.get(MediaLibrary, library_id)
    if library is None:
        raise NotFoundError("Library not found", details={"library_id": library_id})
    return library


def get_browsable_item(session: Session, actor: MediaActor, item_id: int) -> MediaItem:
    item = session.execute(
        select(MediaItem)
        .join(MediaLibrary, MediaLibrary.id == MediaItem.library_id)
        .where(MediaItem.id == item_id, browsable_clause(actor))
    ).scalar_one_or_none()
    if item is None:
        raise NotFoundError("Media item not found", details={"item_id": item_id})
    return item


def require_writable(library: MediaLibrary, actor: MediaActor) -> None:
    """Only the owner of a personal library (with ``media:write``) writes."""
    if not (library.is_personal and library.owner_user_id == actor.user.id and actor.can_write):
        raise ForbiddenError(
            "Only the owner can change a personal library; shared libraries are read-only here",
            details={"library_id": library.id},
        )


def can_scan(library: MediaLibrary, actor: MediaActor) -> bool:
    if actor.is_admin:
        return True
    return library.is_personal and library.owner_user_id == actor.user.id and actor.can_write


# ---------------------------------------------------------------------------
# Personal libraries
# ---------------------------------------------------------------------------


def personal_library(session: Session, user: User) -> MediaLibrary | None:
    return session.execute(
        select(MediaLibrary).where(
            MediaLibrary.kind == LIBRARY_PERSONAL, MediaLibrary.owner_user_id == user.id
        )
    ).scalar_one_or_none()


def ensure_personal_library(session: Session, settings: Settings, user: User) -> MediaLibrary:
    """Create the user's personal library (folder and record) if missing."""
    folder = files.resolve_in_root(settings.resolved_media_dir, user.username)
    folder.mkdir(parents=True, exist_ok=True, mode=0o750)
    library = personal_library(session, user)
    if library is not None:
        return library
    library = MediaLibrary(
        name=f"{PERSONAL_NAME_PREFIX}{user.username}",
        kind=LIBRARY_PERSONAL,
        owner_user_id=user.id,
        root=str(settings.resolved_media_dir),
        rel_path=user.username,
        created_by=user.id,
    )
    session.add(library)
    session.commit()
    return library


# ---------------------------------------------------------------------------
# Shared libraries (media:admin)
# ---------------------------------------------------------------------------


def _clean_name(name: str) -> str:
    value = name.strip()
    if not value or len(value) > 64:
        raise BadRequestError("Library name must be 1-64 characters")
    if value.startswith(PERSONAL_NAME_PREFIX):
        raise BadRequestError(f"Library names may not start with '{PERSONAL_NAME_PREFIX}'")
    if any(ord(c) < 32 for c in value):
        raise BadRequestError("Library name contains control characters")
    return value


def _ensure_unique_name(session: Session, name: str, *, exclude_id: int | None = None) -> None:
    query = select(MediaLibrary.id).where(func.lower(MediaLibrary.name) == name.lower())
    if exclude_id is not None:
        query = query.where(MediaLibrary.id != exclude_id)
    if session.execute(query).first() is not None:
        raise ConflictError("A library with that name already exists", details={"name": name})


def _clean_folder(settings: Settings, root_str: str, rel_path: str) -> tuple[str, str]:
    root = files.resolve_root(settings, root_str)
    folder = files.resolve_in_root(root, rel_path)
    if not folder.is_dir():
        raise BadRequestError("The library folder must be an existing directory")
    rel = folder.relative_to(root).as_posix()
    return str(root), "" if rel == "." else rel


def _validate_groups(session: Session, group_ids: list[int]) -> list[int]:
    unique = sorted(set(group_ids))
    if not unique:
        return []
    found = set(session.execute(select(Group.id).where(Group.id.in_(unique))).scalars().all())
    missing = [gid for gid in unique if gid not in found]
    if missing:
        raise BadRequestError("Unknown group id(s)", details={"group_ids": missing})
    return unique


def library_group_ids(session: Session, library_id: int) -> list[int]:
    return sorted(
        session.execute(
            select(media_library_groups.c.group_id).where(
                media_library_groups.c.library_id == library_id
            )
        )
        .scalars()
        .all()
    )


def _write_groups(session: Session, library_id: int, group_ids: list[int]) -> None:
    session.execute(
        delete(media_library_groups).where(media_library_groups.c.library_id == library_id)
    )
    if group_ids:
        session.execute(
            insert(media_library_groups),
            [{"library_id": library_id, "group_id": gid} for gid in group_ids],
        )


def _details(library: MediaLibrary, **extra: Any) -> dict[str, Any]:
    return {
        "name": library.name,
        "kind": library.kind,
        "root": library.root,
        "path": library.rel_path,
        **extra,
    }


def create_shared_library(
    session: Session,
    settings: Settings,
    *,
    name: str,
    root: str,
    path: str,
    include_videos: bool,
    enabled: bool,
    group_ids: list[int],
    actor: User,
    context: dict[str, str | None] | None = None,
) -> MediaLibrary:
    clean = _clean_name(name)
    _ensure_unique_name(session, clean)
    root_str, rel = _clean_folder(settings, root, path)
    groups = _validate_groups(session, group_ids)
    library = MediaLibrary(
        name=clean,
        kind=LIBRARY_SHARED,
        root=root_str,
        rel_path=rel,
        include_videos=include_videos,
        enabled=enabled,
        scan_state=SCAN_QUEUED,  # index it straight away
        created_by=actor.id,
    )
    session.add(library)
    session.flush()
    _write_groups(session, library.id, groups)
    audit.record_audit_event(
        session,
        event=audit.MEDIA_LIBRARY_CREATED,
        category=audit.CATEGORY_MEDIA,
        actor=actor,
        target_type="media_library",
        target_id=library.id,
        details=_details(library, group_ids=groups),
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return library


def update_library(
    session: Session,
    library: MediaLibrary,
    *,
    name: str | None,
    enabled: bool | None,
    include_videos: bool | None,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> MediaLibrary:
    changes: dict[str, Any] = {}
    if name is not None:
        if library.is_personal:
            raise BadRequestError("A personal library cannot be renamed")
        clean = _clean_name(name)
        _ensure_unique_name(session, clean, exclude_id=library.id)
        if clean != library.name:
            changes["name"] = {"from": library.name, "to": clean}
            library.name = clean
    if enabled is not None and enabled != library.enabled:
        changes["enabled"] = enabled
        library.enabled = enabled
    if include_videos is not None and include_videos != library.include_videos:
        changes["include_videos"] = include_videos
        library.include_videos = include_videos
        if library.scan_state == SCAN_IDLE:
            library.scan_state = SCAN_QUEUED  # pick videos up (or drop them)
    if changes:
        audit.record_audit_event(
            session,
            event=audit.MEDIA_LIBRARY_UPDATED,
            category=audit.CATEGORY_MEDIA,
            actor=actor,
            target_type="media_library",
            target_id=library.id,
            details=_details(library, changes=changes),
            **audit.request_context(context),
            commit=False,
        )
    session.commit()
    return library


def set_library_groups(
    session: Session,
    library: MediaLibrary,
    group_ids: list[int],
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> list[int]:
    if library.is_personal:
        raise BadRequestError("A personal library cannot be shared with groups")
    groups = _validate_groups(session, group_ids)
    before = library_group_ids(session, library.id)
    _write_groups(session, library.id, groups)
    audit.record_audit_event(
        session,
        event=audit.MEDIA_LIBRARY_GROUPS_CHANGED,
        category=audit.CATEGORY_MEDIA,
        actor=actor,
        target_type="media_library",
        target_id=library.id,
        details=_details(library, before=before, after=groups),
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return groups


def delete_library(
    session: Session,
    settings: Settings,
    library: MediaLibrary,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    """Forget a shared library: index, albums entries and cache. Files stay."""
    from app.services.media import thumbnails  # local import: avoids a cycle

    if library.is_personal:
        raise ConflictError("A personal library is removed only together with its user")
    item_ids = list(
        session.execute(select(MediaItem.id).where(MediaItem.library_id == library.id))
        .scalars()
        .all()
    )
    details = _details(library, items=len(item_ids))
    library_id = library.id
    session.delete(library)
    audit.record_audit_event(
        session,
        event=audit.MEDIA_LIBRARY_DELETED,
        category=audit.CATEGORY_MEDIA,
        actor=actor,
        target_type="media_library",
        target_id=library_id,
        details=details,
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    for item_id in item_ids:
        thumbnails.remove_cached(settings, item_id)


# ---------------------------------------------------------------------------
# Scans and summaries
# ---------------------------------------------------------------------------


def queue_scan(
    session: Session,
    library: MediaLibrary,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> MediaLibrary:
    """Ask the worker to rescan ``library`` (no-op when already queued/running)."""
    if library.scan_state == SCAN_IDLE:
        library.scan_state = SCAN_QUEUED
        audit.record_audit_event(
            session,
            event=audit.MEDIA_SCAN_QUEUED,
            category=audit.CATEGORY_MEDIA,
            actor=actor,
            target_type="media_library",
            target_id=library.id,
            details={"name": library.name},
            **audit.request_context(context),
            commit=False,
        )
    session.commit()
    return library


def schedule_next_scan(settings: Settings, library: MediaLibrary) -> None:
    minutes = settings.media_scan_interval_minutes
    library.next_scan_at = utcnow_naive() + timedelta(minutes=minutes) if minutes else None


def counts_by_library(session: Session, library_ids: list[int]) -> dict[int, dict[str, int]]:
    """``{library_id: {"image": n, "video": n, "pending_thumbnails": n}}``."""
    result: dict[int, dict[str, int]] = {
        lid: {"image": 0, "video": 0, "pending_thumbnails": 0} for lid in library_ids
    }
    if not library_ids:
        return result
    for library_id, kind, count in session.execute(
        select(MediaItem.library_id, MediaItem.kind, func.count(MediaItem.id))
        .where(MediaItem.library_id.in_(library_ids))
        .group_by(MediaItem.library_id, MediaItem.kind)
    ).all():
        result[library_id][kind] = int(count)
    for library_id, count in session.execute(
        select(MediaItem.library_id, func.count(MediaItem.id))
        .where(MediaItem.library_id.in_(library_ids), MediaItem.thumb_state == THUMB_PENDING)
        .group_by(MediaItem.library_id)
    ).all():
        result[library_id]["pending_thumbnails"] = int(count)
    return result


def scan_summary(library: MediaLibrary) -> dict[str, int] | None:
    if not library.last_scan_summary:
        return None
    try:
        data = json.loads(library.last_scan_summary)
    except ValueError:
        return None
    return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else None
