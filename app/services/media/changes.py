"""The media change journal (``/media/sync/changes``).

Every index change (a file found, changed or gone, an upload, a deletion
through the gallery) appends one ``media_changes`` row in the transaction
that changes the index. The row id is the cursor, with the same semantics as
the file-manager journal (``app/services/sync.py``): a cursor older than the
retained journal, or one from the future, answers ``reset: true`` and the
client re-lists the library.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError
from app.db.base import utcnow_naive
from app.models import MediaChange, MediaItem

_CURSOR_PREFIX = "m"


@dataclass
class MediaChangePage:
    cursor: str
    has_more: bool
    reset: bool
    changes: list[MediaChange]


def encode_cursor(change_id: int) -> str:
    return f"{_CURSOR_PREFIX}{change_id}"


def decode_cursor(cursor: str) -> int:
    value = cursor.strip()
    if not value.startswith(_CURSOR_PREFIX) or not value[1:].isdigit():
        raise BadRequestError("Invalid media sync cursor", details={"cursor": cursor})
    return int(value[1:])


def record(session: Session, item: MediaItem, kind: str) -> None:
    """Append one change for ``item``; the caller commits."""
    session.add(
        MediaChange(
            library_id=item.library_id,
            item_id=item.id,
            rel_path=item.rel_path,
            kind=kind,
            sha256=item.sha256,
            size=item.size,
        )
    )


def _head_id(session: Session) -> int:
    return int(session.execute(select(func.coalesce(func.max(MediaChange.id), 0))).scalar_one())


def current_cursor(session: Session) -> str:
    return encode_cursor(_head_id(session))


def changes_since(session: Session, *, library_id: int, cursor: str, limit: int) -> MediaChangePage:
    after = decode_cursor(cursor)
    head = _head_id(session)
    oldest = session.execute(select(func.min(MediaChange.id))).scalar_one()
    stale = after > head or (oldest is not None and after < oldest - 1 and after > 0)
    never_synced_gap = after == 0 and oldest is not None and oldest > 1
    if stale or never_synced_gap:
        return MediaChangePage(cursor=encode_cursor(head), has_more=False, reset=True, changes=[])
    rows = list(
        session.execute(
            select(MediaChange)
            .where(MediaChange.id > after, MediaChange.library_id == library_id)
            .order_by(MediaChange.id)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].id) if has_more else encode_cursor(head)
    return MediaChangePage(cursor=next_cursor, has_more=has_more, reset=False, changes=rows)


def prune(session: Session, settings: Settings) -> int:
    """Drop rows older than ``NAS_SYNC_CHANGE_RETENTION_DAYS`` (keeps the newest)."""
    cutoff = utcnow_naive() - timedelta(days=settings.sync_change_retention_days)
    head = _head_id(session)
    result = session.execute(
        delete(MediaChange).where(MediaChange.created_at < cutoff, MediaChange.id < head)
    )
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def lookup_hashes(session: Session, *, library_id: int, hashes: list[str]) -> dict[str, list[int]]:
    """Which of ``hashes`` already exist in the library: ``{sha256: [item ids]}``."""
    found: dict[str, list[int]] = {}
    if not hashes:
        return found
    for item_id, digest in session.execute(
        select(MediaItem.id, MediaItem.sha256)
        .where(MediaItem.library_id == library_id, MediaItem.sha256.in_(hashes))
        .order_by(MediaItem.id)
    ).all():
        if digest is not None:
            found.setdefault(digest, []).append(item_id)
    return found
