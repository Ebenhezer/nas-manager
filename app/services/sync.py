"""Sync cursors over the file change journal (Phase 6).

Mobile and desktop clients keep a local mirror of a managed root. They take
a cursor (``GET /sync/cursor``), list the tree once, and from then on ask for
the changes after their cursor (``GET /sync/changes``). Every file-manager
mutation appends a ``file_changes`` row in the same transaction as its audit
event, so a committed change is always visible to the next poll.

A cursor is the id of the last journal row the client has seen. When that row
has been pruned (older than ``NAS_SYNC_CHANGE_RETENTION_DAYS``) the server
cannot prove the client missed nothing, so it answers ``reset: true`` and the
client must re-list. Changes made outside the application (Samba, a shell on
the NAS) are not journaled; clients should re-list periodically if those
matter to them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError
from app.db.base import utcnow_naive
from app.models import FileChange, User

_CURSOR_PREFIX = "c"


@dataclass
class ChangePage:
    cursor: str
    has_more: bool
    reset: bool
    changes: list[FileChange]


def encode_cursor(change_id: int) -> str:
    return f"{_CURSOR_PREFIX}{change_id}"


def decode_cursor(cursor: str) -> int:
    """Parse an opaque cursor; malformed values are a client error."""
    value = cursor.strip()
    if not value.startswith(_CURSOR_PREFIX) or not value[1:].isdigit():
        raise BadRequestError("Invalid sync cursor", details={"cursor": cursor})
    return int(value[1:])


def record_change(
    session: Session,
    *,
    root: str,
    path: str,
    kind: str,
    is_dir: bool = False,
    size_bytes: int | None = None,
    old_path: str | None = None,
    actor: User | None = None,
) -> FileChange:
    """Append one change; the caller commits it with its own transaction."""
    row = FileChange(
        root=root,
        path=path,
        old_path=old_path,
        kind=kind,
        is_dir=is_dir,
        size_bytes=size_bytes,
        actor_user_id=actor.id if actor is not None else None,
    )
    session.add(row)
    return row


def _head_id(session: Session) -> int:
    return int(session.execute(select(func.coalesce(func.max(FileChange.id), 0))).scalar_one())


def current_cursor(session: Session) -> str:
    """The cursor that names "everything committed so far" (across roots)."""
    return encode_cursor(_head_id(session))


def changes_since(session: Session, *, root: str, cursor: str, limit: int) -> ChangePage:
    """Changes to ``root`` after ``cursor``, oldest first, at most ``limit``.

    Cursors are global ids, so one cursor works for any root; the filter just
    skips other roots' rows. ``reset`` is set when rows the client may not
    have seen have already been pruned.
    """
    after = decode_cursor(cursor)
    head = _head_id(session)
    if after > head:
        # A cursor from the future: another server, or a restored database.
        return ChangePage(cursor=encode_cursor(head), has_more=False, reset=True, changes=[])
    oldest = session.execute(select(func.min(FileChange.id))).scalar_one()
    if after > 0 and oldest is not None and after < oldest - 1:
        return ChangePage(cursor=encode_cursor(head), has_more=False, reset=True, changes=[])
    if after == 0 and oldest is not None and oldest > 1:
        # The client never synced and early history is gone: re-list.
        return ChangePage(cursor=encode_cursor(head), has_more=False, reset=True, changes=[])
    rows = list(
        session.execute(
            select(FileChange)
            .where(FileChange.id > after, FileChange.root == root)
            .order_by(FileChange.id)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    # Without more rows for this root the client may jump straight to the
    # head, skipping other roots' entries it would only filter out again.
    next_cursor = encode_cursor(rows[-1].id) if has_more else encode_cursor(head)
    return ChangePage(cursor=next_cursor, has_more=has_more, reset=False, changes=rows)


def prune(session: Session, settings: Settings) -> int:
    """Drop journal rows older than the retention window; returns the count.

    The newest row is always kept so the oldest-retained check in
    ``changes_since`` stays meaningful even after a quiet period.
    """
    cutoff = utcnow_naive() - timedelta(days=settings.sync_change_retention_days)
    head = _head_id(session)
    result = session.execute(
        delete(FileChange).where(FileChange.created_at < cutoff, FileChange.id < head)
    )
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
