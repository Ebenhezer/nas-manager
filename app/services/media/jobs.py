"""Background media work, run by the worker process (``python -m app.worker``).

Scans use the same claim/lease scheme as sync runs: a library moves from
``queued`` to ``running`` with a conditional ``UPDATE`` (so two workers
never scan one library at once) and holds a lease the worker renews every
tick. A scan whose worker died is put back in the queue rather than failed,
because a scan is idempotent. Thumbnails are rendered in batches between
scans; rendering one twice is harmless (the file is replaced atomically).
Transcodes (Phase 9) have their own claim/lease functions in ``transcode``.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.models import MediaItem, MediaLibrary
from app.models.media import (
    SCAN_IDLE,
    SCAN_QUEUED,
    SCAN_RESULT_FAILED,
    SCAN_RESULT_OK,
    SCAN_RUNNING,
    THUMB_PENDING,
)
from app.services.media import changes, indexer, libraries, thumbnails, transcode

logger = logging.getLogger(__name__)


def enqueue_due_scans(session: Session, settings: Settings, now: datetime) -> int:
    """Queue idle, enabled libraries whose periodic rescan is due."""
    if not settings.media_scan_interval_minutes:
        return 0
    result = session.execute(
        update(MediaLibrary)
        .where(
            MediaLibrary.enabled.is_(True),
            MediaLibrary.scan_state == SCAN_IDLE,
            or_(MediaLibrary.next_scan_at.is_(None), MediaLibrary.next_scan_at <= now),
        )
        .values(scan_state=SCAN_QUEUED)
    )
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def claim_scan(session: Session, *, owner: str, lease_seconds: int) -> MediaLibrary | None:
    """Atomically move one queued library to ``running`` for ``owner``."""
    candidates = (
        session.execute(
            select(MediaLibrary.id)
            .where(MediaLibrary.scan_state == SCAN_QUEUED, MediaLibrary.enabled.is_(True))
            .order_by(MediaLibrary.id)
            .limit(20)
        )
        .scalars()
        .all()
    )
    for library_id in candidates:
        result = session.execute(
            update(MediaLibrary)
            .where(MediaLibrary.id == library_id, MediaLibrary.scan_state == SCAN_QUEUED)
            .values(
                scan_state=SCAN_RUNNING,
                scan_lease_owner=owner,
                scan_lease_expires_at=utcnow_naive() + timedelta(seconds=lease_seconds),
            )
        )
        session.commit()
        if result.rowcount == 1:  # type: ignore[attr-defined]
            return session.get(MediaLibrary, library_id, populate_existing=True)
    return None


def renew_scan_leases(session: Session, *, owner: str, lease_seconds: int) -> None:
    session.execute(
        update(MediaLibrary)
        .where(MediaLibrary.scan_state == SCAN_RUNNING, MediaLibrary.scan_lease_owner == owner)
        .values(scan_lease_expires_at=utcnow_naive() + timedelta(seconds=lease_seconds))
    )
    session.commit()


def reclaim_scans(session: Session, now: datetime) -> int:
    """Re-queue scans whose worker stopped renewing the lease."""
    result = session.execute(
        update(MediaLibrary)
        .where(
            MediaLibrary.scan_state == SCAN_RUNNING,
            MediaLibrary.scan_lease_expires_at.is_not(None),
            MediaLibrary.scan_lease_expires_at < now,
        )
        .values(scan_state=SCAN_QUEUED, scan_lease_owner=None, scan_lease_expires_at=None)
    )
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def run_scan(
    session: Session,
    settings: Settings,
    library: MediaLibrary,
    *,
    stop: threading.Event | None = None,
) -> indexer.ScanResult | None:
    """Scan a claimed library and record the outcome; None when it failed."""
    outcome: indexer.ScanResult | None = None
    error: str | None = None
    try:
        outcome = indexer.scan_library(session, settings, library, stop=stop)
    except indexer.ScanError as exc:
        session.rollback()
        error = str(exc)
    except Exception as exc:
        session.rollback()
        logger.exception("media scan failed", extra={"library_id": library.id})
        error = f"unexpected error: {exc}"
    session.refresh(library)
    library.scan_lease_owner = None
    library.scan_lease_expires_at = None
    interrupted = outcome is not None and not outcome.complete and stop is not None
    if interrupted and stop is not None and stop.is_set():
        library.scan_state = SCAN_QUEUED  # finish it after the restart
    else:
        library.scan_state = SCAN_IDLE
        library.last_scan_at = utcnow_naive()
        library.last_scan_status = SCAN_RESULT_OK if error is None else SCAN_RESULT_FAILED
        library.last_scan_error = error
        if outcome is not None:
            library.last_scan_summary = json.dumps(outcome.as_dict())
        libraries.schedule_next_scan(settings, library)
    session.commit()
    return outcome


def process_thumbnails(
    session: Session, settings: Settings, *, stop: threading.Event | None = None
) -> int:
    return thumbnails.process_pending(
        session, settings, limit=settings.media_thumbnail_batch, stop=stop
    )


def prune(session: Session, settings: Settings) -> dict[str, int]:
    return {
        "changes": changes.prune(session, settings),
        "cache_files": thumbnails.prune_orphans(session, settings),
        "transcode_files": transcode.prune_orphans(session, settings),
        "transcodes_evicted": transcode.enforce_cache_limit(session, settings),
    }


def status(session: Session) -> dict[str, Any]:
    states = dict(
        session.execute(
            select(MediaLibrary.scan_state, func.count(MediaLibrary.id)).group_by(
                MediaLibrary.scan_state
            )
        ).all()
    )
    pending = session.execute(
        select(func.count(MediaItem.id)).where(MediaItem.thumb_state == THUMB_PENDING)
    ).scalar_one()
    return {
        "queued_scans": int(states.get(SCAN_QUEUED, 0)),
        "running_scans": int(states.get(SCAN_RUNNING, 0)),
        "pending_thumbnails": int(pending),
        **transcode.counts(session),
    }
