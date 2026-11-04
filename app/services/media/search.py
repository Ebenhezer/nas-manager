"""Gallery search: always limited to the caller's browsable libraries.

Besides the free-text and metadata filters, a search can be scoped to one
folder of a library (``folder`` + ``recursive``), which is how the
explorer-style gallery lists a directory, and to the caller's favorites.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, Select, and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, NotFoundError
from app.models import MediaAlbum, MediaAlbumItem, MediaFavorite, MediaItem, MediaLibrary
from app.services.media.libraries import MediaActor, browsable_clause

SortOrder = Literal[
    "taken_desc",
    "taken_asc",
    "name",
    "name_desc",
    "size_desc",
    "size_asc",
    "modified_desc",
    "modified_asc",
    "type",
    "duration_desc",
    "duration_asc",
]
Orientation = Literal["landscape", "portrait", "square"]

# Image sizes are stored as displayed (EXIF orientation applied); video sizes
# are the coded ones, which a rotation of 90/270 degrees swaps on screen.
_SWAPPED_ROTATIONS = (90, 270, -90, -270)


@dataclass
class SearchFilters:
    library_id: int | None = None
    q: str | None = None
    kind: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    camera: str | None = None
    album_id: int | None = None
    has_gps: bool | None = None
    # Folder inside the library ("" = top level); needs ``library_id``.
    folder: str | None = None
    recursive: bool = False
    favorites: bool = False
    min_size: int | None = None
    max_size: int | None = None
    orientation: Orientation | None = None
    indexed_after: datetime | None = None
    sort: SortOrder = "taken_desc"


def clean_folder(folder: str) -> str:
    """``folder`` as a "/"-separated relative path without empty or dot parts."""
    parts = [part for part in folder.replace("\\", "/").split("/") if part]
    if any(part in {".", ".."} or "\x00" in part for part in parts):
        raise BadRequestError("Invalid folder", details={"folder": folder})
    return "/".join(parts)


def folder_clause(folder: str, recursive: bool) -> ColumnElement[bool]:
    """Items directly in ``folder`` (or anywhere below it when ``recursive``).

    Prefix tests use ``substr`` rather than ``LIKE``, which SQLite compares
    case-insensitively.
    """
    if not folder:
        return MediaItem.rel_path.is_not(None) if recursive else ~MediaItem.rel_path.contains("/")
    prefix = f"{folder}/"
    inside = func.substr(MediaItem.rel_path, 1, len(prefix)) == prefix
    if recursive:
        return inside
    rest = func.substr(MediaItem.rel_path, len(prefix) + 1)
    return and_(inside, ~rest.contains("/"))


def _display_size() -> tuple[ColumnElement[int], ColumnElement[int]]:
    swapped = MediaItem.rotation.in_(_SWAPPED_ROTATIONS)
    width = case((swapped, MediaItem.height), else_=MediaItem.width)
    height = case((swapped, MediaItem.width), else_=MediaItem.height)
    return width, height


def camera_label(make: str | None, model: str | None) -> str:
    """One display name for a camera ("Canon EOS R5", not "Canon Canon EOS R5")."""
    make = (make or "").strip()
    model = (model or "").strip()
    if make and model.lower().startswith(make.lower()):
        return model
    return " ".join(part for part in (make, model) if part)


def _like(text: str) -> str:
    escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _conditions(session: Session, actor: MediaActor, f: SearchFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [browsable_clause(actor)]
    if f.library_id is not None:
        conditions.append(MediaItem.library_id == f.library_id)
    if f.kind is not None:
        conditions.append(MediaItem.kind == f.kind)
    if f.date_from is not None:
        conditions.append(MediaItem.taken_at >= f.date_from)
    if f.date_to is not None:
        conditions.append(MediaItem.taken_at <= f.date_to)
    if f.has_gps is not None:
        conditions.append(
            MediaItem.gps_lat.is_not(None) if f.has_gps else MediaItem.gps_lat.is_(None)
        )
    if f.q:
        pattern = _like(f.q.strip())
        conditions.append(
            or_(
                func.lower(MediaItem.rel_path).like(pattern, escape="\\"),
                func.lower(func.coalesce(MediaItem.camera_make, "")).like(pattern, escape="\\"),
                func.lower(func.coalesce(MediaItem.camera_model, "")).like(pattern, escape="\\"),
            )
        )
    if f.camera:
        pattern = _like(f.camera.strip())
        make = func.coalesce(MediaItem.camera_make, "")
        model = func.coalesce(MediaItem.camera_model, "")
        conditions.append(
            or_(
                func.lower(make).like(pattern, escape="\\"),
                func.lower(model).like(pattern, escape="\\"),
                func.lower(make + " " + model).like(pattern, escape="\\"),
            )
        )
    if f.folder is not None:
        if f.library_id is None:
            raise BadRequestError("A folder filter needs a library_id")
        conditions.append(folder_clause(clean_folder(f.folder), f.recursive))
    if f.favorites:
        conditions.append(
            MediaItem.id.in_(
                select(MediaFavorite.item_id).where(MediaFavorite.user_id == actor.user.id)
            )
        )
    if f.min_size is not None:
        conditions.append(MediaItem.size >= f.min_size)
    if f.max_size is not None:
        conditions.append(MediaItem.size <= f.max_size)
    if f.orientation is not None:
        width, height = _display_size()
        if f.orientation == "landscape":
            conditions.append(width > height)
        elif f.orientation == "portrait":
            conditions.append(width < height)
        else:
            conditions.append(and_(MediaItem.width.is_not(None), width == height))
    if f.indexed_after is not None:
        conditions.append(MediaItem.indexed_at >= f.indexed_after)
    if f.album_id is not None:
        owned = session.execute(
            select(MediaAlbum.id).where(
                MediaAlbum.id == f.album_id, MediaAlbum.owner_user_id == actor.user.id
            )
        ).first()
        if owned is None:
            raise NotFoundError("Album not found", details={"album_id": f.album_id})
        conditions.append(
            MediaItem.id.in_(
                select(MediaAlbumItem.item_id).where(MediaAlbumItem.album_id == f.album_id)
            )
        )
    return conditions


def _ordered(query: Select[MediaItem], sort: SortOrder) -> Select[MediaItem]:
    name = func.lower(MediaItem.name)
    # Items without a duration (photos) sort after the videos either way.
    no_duration = MediaItem.duration_seconds.is_(None)
    orders = {
        "taken_asc": (MediaItem.taken_at.asc(), MediaItem.id.asc()),
        "name": (name, MediaItem.id),
        "name_desc": (name.desc(), MediaItem.id.desc()),
        "size_desc": (MediaItem.size.desc(), MediaItem.id.desc()),
        "size_asc": (MediaItem.size.asc(), MediaItem.id.asc()),
        "modified_desc": (MediaItem.mtime_ns.desc(), MediaItem.id.desc()),
        "modified_asc": (MediaItem.mtime_ns.asc(), MediaItem.id.asc()),
        "type": (MediaItem.kind, MediaItem.mime, name, MediaItem.id),
        "duration_desc": (no_duration, MediaItem.duration_seconds.desc(), MediaItem.id.desc()),
        "duration_asc": (no_duration, MediaItem.duration_seconds.asc(), MediaItem.id.asc()),
    }
    return query.order_by(*orders.get(sort, (MediaItem.taken_at.desc(), MediaItem.id.desc())))


def search_items(
    session: Session, actor: MediaActor, filters: SearchFilters, *, limit: int, offset: int
) -> tuple[list[MediaItem], int]:
    """One page of matching items and the total number of matches."""
    where = and_(*_conditions(session, actor, filters))
    joined = select(MediaItem).join(MediaLibrary, MediaLibrary.id == MediaItem.library_id)
    total = int(
        session.execute(
            select(func.count(MediaItem.id))
            .join(MediaLibrary, MediaLibrary.id == MediaItem.library_id)
            .where(where)
        ).scalar_one()
    )
    rows = (
        session.execute(_ordered(joined.where(where), filters.sort).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return list(rows), total


def favorite_ids(session: Session, actor: MediaActor, item_ids: list[int]) -> set[int]:
    """Which of ``item_ids`` the caller starred."""
    if not item_ids:
        return set()
    return set(
        session.execute(
            select(MediaFavorite.item_id).where(
                MediaFavorite.user_id == actor.user.id, MediaFavorite.item_id.in_(item_ids)
            )
        )
        .scalars()
        .all()
    )


def facets(session: Session, actor: MediaActor, filters: SearchFilters) -> dict[str, object]:
    """Totals, date range and cameras of the matching items (filter menus)."""
    where = and_(*_conditions(session, actor, filters))
    images = func.sum(case((MediaItem.kind == "image", 1), else_=0))
    videos = func.sum(case((MediaItem.kind == "video", 1), else_=0))
    total, image_count, video_count, total_bytes, first, last = session.execute(
        select(
            func.count(MediaItem.id),
            images,
            videos,
            func.sum(MediaItem.size),
            func.min(MediaItem.taken_at),
            func.max(MediaItem.taken_at),
        )
        .join(MediaLibrary, MediaLibrary.id == MediaItem.library_id)
        .where(where)
    ).one()
    cameras: dict[str, int] = {}
    rows = session.execute(
        select(MediaItem.camera_make, MediaItem.camera_model, func.count(MediaItem.id))
        .join(MediaLibrary, MediaLibrary.id == MediaItem.library_id)
        .where(where, or_(MediaItem.camera_make.is_not(None), MediaItem.camera_model.is_not(None)))
        .group_by(MediaItem.camera_make, MediaItem.camera_model)
    ).all()
    for make, model, count in rows:
        label = camera_label(make, model)
        if label:
            cameras[label] = cameras.get(label, 0) + int(count)
    return {
        "total": int(total or 0),
        "images": int(image_count or 0),
        "videos": int(video_count or 0),
        "total_bytes": int(total_bytes or 0),
        "first_taken_at": first,
        "last_taken_at": last,
        "cameras": [
            {"name": name, "count": count}
            for name, count in sorted(cameras.items(), key=lambda pair: (-pair[1], pair[0].lower()))
        ],
    }
