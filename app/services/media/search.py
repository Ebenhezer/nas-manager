"""Gallery search: always limited to the caller's browsable libraries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import MediaAlbum, MediaAlbumItem, MediaItem, MediaLibrary
from app.services.media.libraries import MediaActor, browsable_clause

SortOrder = Literal["taken_desc", "taken_asc", "name", "size_desc"]


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
    sort: SortOrder = "taken_desc"


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
        conditions.append(
            or_(
                func.lower(func.coalesce(MediaItem.camera_make, "")).like(pattern, escape="\\"),
                func.lower(func.coalesce(MediaItem.camera_model, "")).like(pattern, escape="\\"),
            )
        )
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
    if sort == "taken_asc":
        return query.order_by(MediaItem.taken_at.asc(), MediaItem.id.asc())
    if sort == "name":
        return query.order_by(func.lower(MediaItem.name), MediaItem.id)
    if sort == "size_desc":
        return query.order_by(MediaItem.size.desc(), MediaItem.id.desc())
    return query.order_by(MediaItem.taken_at.desc(), MediaItem.id.desc())


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
