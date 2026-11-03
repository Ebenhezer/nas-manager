"""Albums: per-user collections of items from any library the owner can browse.

Albums are private to their owner (another user, administrators included,
gets 404). An item can only be added while the owner can browse it, and
album listings filter out items the owner has since lost access to, so an
album never becomes a way around a revoked library grant.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import MediaAlbum, MediaAlbumItem, MediaItem, MediaLibrary
from app.services import audit
from app.services.media.libraries import MediaActor, browsable_clause

MAX_ITEMS_PER_REQUEST = 1000


def _clean_name(name: str) -> str:
    value = name.strip()
    if not value or len(value) > 128:
        raise BadRequestError("Album name must be 1-128 characters")
    if any(ord(c) < 32 for c in value):
        raise BadRequestError("Album name contains control characters")
    return value


def _ensure_unique(session: Session, owner_id: int, name: str, exclude_id: int | None) -> None:
    query = select(MediaAlbum.id).where(
        MediaAlbum.owner_user_id == owner_id, func.lower(MediaAlbum.name) == name.lower()
    )
    if exclude_id is not None:
        query = query.where(MediaAlbum.id != exclude_id)
    if session.execute(query).first() is not None:
        raise ConflictError("You already have an album with that name", details={"name": name})


def _audit(
    session: Session,
    event: str,
    album: MediaAlbum,
    actor: MediaActor,
    context: dict[str, str | None] | None,
    **details: object,
) -> None:
    audit.record_audit_event(
        session,
        event=event,
        category=audit.CATEGORY_MEDIA,
        actor=actor.user,
        target_type="media_album",
        target_id=album.id,
        details={"name": album.name, **details},
        **audit.request_context(context),
        commit=False,
    )


def get_album(session: Session, actor: MediaActor, album_id: int) -> MediaAlbum:
    album = session.get(MediaAlbum, album_id)
    if album is None or album.owner_user_id != actor.user.id:
        raise NotFoundError("Album not found", details={"album_id": album_id})
    return album


def visible_item_ids(session: Session, actor: MediaActor, album_id: int) -> list[int]:
    """The album's items the owner can still browse, in album order."""
    return list(
        session.execute(
            select(MediaAlbumItem.item_id)
            .join(MediaItem, MediaItem.id == MediaAlbumItem.item_id)
            .join(MediaLibrary, MediaLibrary.id == MediaItem.library_id)
            .where(MediaAlbumItem.album_id == album_id, browsable_clause(actor))
            .order_by(MediaAlbumItem.position, MediaAlbumItem.item_id)
        )
        .scalars()
        .all()
    )


def list_albums(session: Session, actor: MediaActor) -> list[tuple[MediaAlbum, int]]:
    albums = list(
        session.execute(
            select(MediaAlbum)
            .where(MediaAlbum.owner_user_id == actor.user.id)
            .order_by(func.lower(MediaAlbum.name))
        )
        .scalars()
        .all()
    )
    if not albums:
        return []
    counts = dict(
        session.execute(
            select(MediaAlbumItem.album_id, func.count(MediaAlbumItem.item_id))
            .join(MediaItem, MediaItem.id == MediaAlbumItem.item_id)
            .join(MediaLibrary, MediaLibrary.id == MediaItem.library_id)
            .where(MediaAlbumItem.album_id.in_([a.id for a in albums]), browsable_clause(actor))
            .group_by(MediaAlbumItem.album_id)
        ).all()
    )
    return [(album, int(counts.get(album.id, 0))) for album in albums]


def create_album(
    session: Session,
    actor: MediaActor,
    *,
    name: str,
    description: str,
    context: dict[str, str | None] | None = None,
) -> MediaAlbum:
    clean = _clean_name(name)
    _ensure_unique(session, actor.user.id, clean, None)
    album = MediaAlbum(owner_user_id=actor.user.id, name=clean, description=description.strip())
    session.add(album)
    session.flush()
    _audit(session, audit.MEDIA_ALBUM_CREATED, album, actor, context)
    session.commit()
    return album


def update_album(
    session: Session,
    actor: MediaActor,
    album: MediaAlbum,
    *,
    name: str | None,
    description: str | None,
    cover_item_id: int | None,
    clear_cover: bool,
    context: dict[str, str | None] | None = None,
) -> MediaAlbum:
    if name is not None:
        clean = _clean_name(name)
        _ensure_unique(session, actor.user.id, clean, album.id)
        album.name = clean
    if description is not None:
        album.description = description.strip()
    if clear_cover:
        album.cover_item_id = None
    elif cover_item_id is not None:
        if cover_item_id not in visible_item_ids(session, actor, album.id):
            raise BadRequestError(
                "The cover must be an item in this album", details={"item_id": cover_item_id}
            )
        album.cover_item_id = cover_item_id
    _audit(session, audit.MEDIA_ALBUM_UPDATED, album, actor, context)
    session.commit()
    return album


def delete_album(
    session: Session,
    actor: MediaActor,
    album: MediaAlbum,
    *,
    context: dict[str, str | None] | None = None,
) -> None:
    """Delete the album only; its photos stay where they are."""
    _audit(session, audit.MEDIA_ALBUM_DELETED, album, actor, context)
    session.delete(album)
    session.commit()


def _check_ids(item_ids: list[int]) -> list[int]:
    unique = list(dict.fromkeys(item_ids))
    if not unique:
        raise BadRequestError("item_ids must not be empty")
    if len(unique) > MAX_ITEMS_PER_REQUEST:
        raise BadRequestError(f"At most {MAX_ITEMS_PER_REQUEST} items per request")
    return unique


def add_items(
    session: Session,
    actor: MediaActor,
    album: MediaAlbum,
    item_ids: list[int],
    *,
    context: dict[str, str | None] | None = None,
) -> int:
    """Add browsable items (already present ones are skipped); returns the count added."""
    wanted = _check_ids(item_ids)
    allowed = set(
        session.execute(
            select(MediaItem.id)
            .join(MediaLibrary, MediaLibrary.id == MediaItem.library_id)
            .where(MediaItem.id.in_(wanted), browsable_clause(actor))
        )
        .scalars()
        .all()
    )
    missing = [item_id for item_id in wanted if item_id not in allowed]
    if missing:
        raise NotFoundError("Media item(s) not found", details={"item_ids": missing})
    present = set(
        session.execute(
            select(MediaAlbumItem.item_id).where(
                MediaAlbumItem.album_id == album.id, MediaAlbumItem.item_id.in_(wanted)
            )
        )
        .scalars()
        .all()
    )
    position = int(
        session.execute(
            select(func.coalesce(func.max(MediaAlbumItem.position), 0)).where(
                MediaAlbumItem.album_id == album.id
            )
        ).scalar_one()
    )
    added = 0
    for item_id in wanted:
        if item_id in present:
            continue
        position += 1
        session.add(MediaAlbumItem(album_id=album.id, item_id=item_id, position=position))
        added += 1
    if album.cover_item_id is None and wanted:
        album.cover_item_id = wanted[0]
    _audit(session, audit.MEDIA_ALBUM_ITEMS_CHANGED, album, actor, context, added=added)
    session.commit()
    return added


def remove_items(
    session: Session,
    actor: MediaActor,
    album: MediaAlbum,
    item_ids: list[int],
    *,
    context: dict[str, str | None] | None = None,
) -> int:
    wanted = _check_ids(item_ids)
    result = session.execute(
        delete(MediaAlbumItem).where(
            MediaAlbumItem.album_id == album.id, MediaAlbumItem.item_id.in_(wanted)
        )
    )
    removed = int(result.rowcount or 0)  # type: ignore[attr-defined]
    if album.cover_item_id in wanted:
        album.cover_item_id = None
    _audit(session, audit.MEDIA_ALBUM_ITEMS_CHANGED, album, actor, context, removed=removed)
    session.commit()
    return removed
