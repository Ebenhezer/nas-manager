"""Per-user favorites (Phase 15): private stars on items the user can browse."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import MediaFavorite, MediaItem, MediaLibrary
from app.services.media.libraries import MediaActor, browsable_clause


def set_favorite(session: Session, actor: MediaActor, item_ids: list[int], favorite: bool) -> int:
    """Star (or unstar) items; returns how many actually changed.

    Starring silently skips items the caller cannot browse; unstarring
    works on any of the caller's own stars.
    """
    ids = list(dict.fromkeys(item_ids))
    existing = set(
        session.execute(
            select(MediaFavorite.item_id).where(
                MediaFavorite.user_id == actor.user.id, MediaFavorite.item_id.in_(ids)
            )
        )
        .scalars()
        .all()
    )
    if not favorite:
        if existing:
            session.execute(
                delete(MediaFavorite).where(
                    MediaFavorite.user_id == actor.user.id, MediaFavorite.item_id.in_(existing)
                )
            )
            session.commit()
        return len(existing)
    visible = (
        session.execute(
            select(MediaItem.id)
            .join(MediaLibrary, MediaLibrary.id == MediaItem.library_id)
            .where(MediaItem.id.in_(ids), browsable_clause(actor))
        )
        .scalars()
        .all()
    )
    added = [item_id for item_id in visible if item_id not in existing]
    session.add_all(MediaFavorite(user_id=actor.user.id, item_id=item_id) for item_id in added)
    session.commit()
    return len(added)
