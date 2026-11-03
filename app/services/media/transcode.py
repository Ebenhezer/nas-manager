"""Cached browser-compatible transcodes of videos (Phase 9).

A video whose codecs or container a browser cannot play is converted once,
by the worker, to an H.264/AAC MP4 with ``+faststart`` and cached under
``<NAS_MEDIA_CACHE_DIR>/transcodes/``. The output name carries the source
fingerprint, so a changed source never streams a stale rendition; the
request simply queues a fresh transcode. The cache is evicted least recently
streamed first above ``NAS_MEDIA_TRANSCODE_CACHE_MAX_BYTES``.

Claims use the same conditional ``UPDATE`` + lease scheme as scans and sync
runs. A transcode interrupted by a worker shutdown, or whose worker died, is
re-queued rather than failed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, ServiceUnavailableError
from app.db.base import utcnow_naive
from app.models import MediaItem, MediaLibrary, MediaTranscode, User
from app.models.media import (
    KIND_VIDEO,
    PLAYBACK_DIRECT,
    PLAYBACK_TRANSCODE,
    TRANSCODE_FAILED,
    TRANSCODE_QUEUED,
    TRANSCODE_READY,
    TRANSCODE_RUNNING,
)
from app.services import audit
from app.services.media import ffmpeg, libraries, thumbnails, video

logger = logging.getLogger(__name__)

_ACTIVE = (TRANSCODE_QUEUED, TRANSCODE_RUNNING)
_STALE_TMP_SECONDS = 24 * 3600


class TranscodeRequiredError(ConflictError):
    code = "transcode_required"


class TranscodingUnavailableError(ServiceUnavailableError):
    code = "transcoding_unavailable"


def is_available(settings: Settings) -> bool:
    return settings.media_transcode_enabled and ffmpeg.available(settings)


def cache_dir(settings: Settings) -> Path:
    return settings.resolved_media_cache_dir / "transcodes"


def output_path(settings: Settings, item_id: int, fingerprint: str) -> Path:
    return cache_dir(settings) / f"{item_id % 256:02x}" / f"{item_id}-{fingerprint}.mp4"


def remove_cached(settings: Settings, item_id: int) -> None:
    folder = cache_dir(settings) / f"{item_id % 256:02x}"
    if folder.is_dir():
        for path in folder.glob(f"{item_id}-*.mp4"):
            path.unlink(missing_ok=True)


def get_row(session: Session, item_id: int) -> MediaTranscode | None:
    return session.execute(
        select(MediaTranscode).where(MediaTranscode.item_id == item_id)
    ).scalar_one_or_none()


def _is_current(settings: Settings, item: MediaItem, row: MediaTranscode | None) -> bool:
    """A ready row for the item's current content whose file still exists."""
    return (
        row is not None
        and row.state == TRANSCODE_READY
        and row.source_fingerprint == thumbnails.fingerprint(item)
        and output_path(settings, item.id, row.source_fingerprint).is_file()
    )


def request(
    session: Session,
    settings: Settings,
    item: MediaItem,
    *,
    actor: User | None,
    context: dict[str, str | None] | None = None,
) -> MediaTranscode:
    """Queue a transcode of ``item`` unless a current one exists or is underway."""
    if item.kind != KIND_VIDEO:
        raise BadRequestError("Only videos can be transcoded", details={"item_id": item.id})
    if item.playback == PLAYBACK_DIRECT:
        raise ConflictError(
            "This video plays directly; no transcode is needed", details={"item_id": item.id}
        )
    if item.playback != PLAYBACK_TRANSCODE:
        raise BadRequestError(
            "This file has no playable video stream (or has not been probed yet)",
            details={"item_id": item.id, "playback": item.playback},
        )
    if not is_available(settings):
        raise TranscodingUnavailableError(
            "Transcoding is not available on this server (ffmpeg missing or disabled)"
        )
    fingerprint = thumbnails.fingerprint(item)
    row = get_row(session, item.id)
    unchanged = row is not None and row.source_fingerprint == fingerprint
    if unchanged and row is not None and (row.state in _ACTIVE or _is_current(settings, item, row)):
        return row
    if row is None:
        row = MediaTranscode(item_id=item.id)
        session.add(row)
    row.state = TRANSCODE_QUEUED
    row.source_fingerprint = fingerprint
    row.progress_percent = 0.0
    row.error = None
    row.output_size = None
    row.started_at = row.finished_at = None
    row.lease_owner = None
    row.lease_expires_at = None
    row.requested_by = actor.id if actor is not None else None
    audit.record_audit_event(
        session,
        event=audit.MEDIA_TRANSCODE_REQUESTED,
        category=audit.CATEGORY_MEDIA,
        actor=actor,
        target_type="media_item",
        target_id=item.id,
        details={"library_id": item.library_id, "path": item.rel_path},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def queue_library(
    session: Session,
    settings: Settings,
    library: MediaLibrary,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> int:
    """Pre-transcode every video of ``library`` that needs it; returns how many were queued."""
    if not is_available(settings):
        raise TranscodingUnavailableError(
            "Transcoding is not available on this server (ffmpeg missing or disabled)"
        )
    items = session.execute(
        select(MediaItem).where(
            MediaItem.library_id == library.id, MediaItem.playback == PLAYBACK_TRANSCODE
        )
    ).scalars()
    queued = 0
    for item in list(items):
        before = get_row(session, item.id)
        state_before = (before.state, before.source_fingerprint) if before else None
        row = request(session, settings, item, actor=actor, context=context)
        if (row.state, row.source_fingerprint) != state_before:
            queued += 1
    return queued


def stream_target(session: Session, settings: Settings, item: MediaItem) -> tuple[Path, str]:
    """The file and type to stream for ``item``, or ``TranscodeRequiredError``."""
    library = session.get(MediaLibrary, item.library_id)
    assert library is not None  # noqa: S101 - callers looked the item up via its library
    if item.playback == PLAYBACK_DIRECT:
        return libraries.item_path(settings, library, item), video.stream_media_type(item)
    row = get_row(session, item.id)
    if _is_current(settings, item, row):
        assert row is not None  # noqa: S101 - _is_current checked it
        row.last_accessed_at = utcnow_naive()
        session.commit()
        return output_path(settings, item.id, row.source_fingerprint), "video/mp4"
    stale = row is not None and row.source_fingerprint != thumbnails.fingerprint(item)
    raise TranscodeRequiredError(
        "This video must be transcoded before it can be streamed",
        details={
            "item_id": item.id,
            "playback": item.playback,
            "state": None if row is None or stale else row.state,
            "progress_percent": 0.0 if row is None or stale else row.progress_percent,
            "transcoding_available": is_available(settings),
        },
    )


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------


def claim(session: Session, *, owner: str, lease_seconds: int) -> MediaTranscode | None:
    candidates = (
        session.execute(
            select(MediaTranscode.id)
            .where(MediaTranscode.state == TRANSCODE_QUEUED)
            .order_by(MediaTranscode.id)
            .limit(20)
        )
        .scalars()
        .all()
    )
    for row_id in candidates:
        now = utcnow_naive()
        result = session.execute(
            update(MediaTranscode)
            .where(MediaTranscode.id == row_id, MediaTranscode.state == TRANSCODE_QUEUED)
            .values(
                state=TRANSCODE_RUNNING,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                started_at=now,
            )
        )
        session.commit()
        if result.rowcount == 1:  # type: ignore[attr-defined]
            return session.get(MediaTranscode, row_id, populate_existing=True)
    return None


def renew_leases(session: Session, *, owner: str, lease_seconds: int) -> None:
    session.execute(
        update(MediaTranscode)
        .where(MediaTranscode.state == TRANSCODE_RUNNING, MediaTranscode.lease_owner == owner)
        .values(lease_expires_at=utcnow_naive() + timedelta(seconds=lease_seconds))
    )
    session.commit()


def reclaim(session: Session, now: datetime) -> int:
    result = session.execute(
        update(MediaTranscode)
        .where(
            MediaTranscode.state == TRANSCODE_RUNNING,
            MediaTranscode.lease_expires_at.is_not(None),
            MediaTranscode.lease_expires_at < now,
        )
        .values(state=TRANSCODE_QUEUED, lease_owner=None, lease_expires_at=None)
    )
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def _fail(session: Session, row: MediaTranscode, item: MediaItem | None, message: str) -> None:
    row.state = TRANSCODE_FAILED
    row.error = message[:2000]
    row.finished_at = utcnow_naive()
    row.lease_owner = row.lease_expires_at = None
    audit.record_audit_event(
        session,
        event=audit.MEDIA_TRANSCODE_FAILED,
        category=audit.CATEGORY_MEDIA,
        outcome=audit.OUTCOME_FAILURE,
        target_type="media_item",
        target_id=row.item_id,
        details={"path": item.rel_path if item else None, "error": row.error},
        commit=False,
    )
    session.commit()


def run(
    session: Session,
    settings: Settings,
    row: MediaTranscode,
    *,
    stop: threading.Event | None = None,
) -> str:
    """Execute a claimed transcode; returns the resulting state."""
    item = session.get(MediaItem, row.item_id)
    library = session.get(MediaLibrary, item.library_id) if item is not None else None
    if item is None or library is None:  # pragma: no cover - cascades delete the row
        _fail(session, row, None, "the video is no longer indexed")
        return row.state
    if row.source_fingerprint != thumbnails.fingerprint(item):
        # The source changed since the request: transcode the new content.
        row.source_fingerprint = thumbnails.fingerprint(item)
    target = output_path(settings, item.id, row.source_fingerprint)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = target.parent / f".tmp-{uuid.uuid4().hex}.mp4"

    last_write = [0.0]

    def progress(percent: float) -> None:
        now = time.monotonic()
        if percent < 100 and now - last_write[0] < 2:
            return
        last_write[0] = now
        row.progress_percent = percent
        session.commit()

    try:
        source = libraries.item_path(settings, library, item)
        ffmpeg.transcode(
            settings,
            source,
            temporary,
            duration=item.duration_seconds,
            on_progress=progress,
            stop=stop,
        )
        os.replace(temporary, target)
    except ffmpeg.FFmpegStopped:
        temporary.unlink(missing_ok=True)
        row.state = TRANSCODE_QUEUED
        row.progress_percent = 0.0
        row.lease_owner = row.lease_expires_at = None
        session.commit()
        return row.state
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if not isinstance(exc, ffmpeg.FFmpegError | OSError):
            logger.exception("transcode failed unexpectedly", extra={"item_id": item.id})
        _fail(session, row, item, str(exc))
        return row.state
    for old in target.parent.glob(f"{item.id}-*.mp4"):
        if old != target:
            old.unlink(missing_ok=True)
    now = utcnow_naive()
    row.state = TRANSCODE_READY
    row.output_size = target.stat().st_size
    row.progress_percent = 100.0
    row.finished_at = now
    row.last_accessed_at = now
    row.lease_owner = row.lease_expires_at = None
    session.commit()
    enforce_cache_limit(session, settings)
    return TRANSCODE_READY


def enforce_cache_limit(session: Session, settings: Settings) -> int:
    """Evict the least recently streamed transcodes above the cache budget."""
    budget = settings.media_transcode_cache_max_bytes
    rows = list(
        session.execute(
            select(MediaTranscode)
            .where(MediaTranscode.state == TRANSCODE_READY)
            .order_by(
                func.coalesce(MediaTranscode.last_accessed_at, MediaTranscode.finished_at).asc()
            )
        ).scalars()
    )
    total = sum(row.output_size or 0 for row in rows)
    evicted = 0
    for row in rows:
        if total <= budget:
            break
        output_path(settings, row.item_id, row.source_fingerprint).unlink(missing_ok=True)
        total -= row.output_size or 0
        session.delete(row)
        evicted += 1
    session.commit()
    return evicted


def cache_bytes(session: Session) -> int:
    value = session.execute(
        select(func.coalesce(func.sum(MediaTranscode.output_size), 0)).where(
            MediaTranscode.state == TRANSCODE_READY
        )
    ).scalar_one()
    return int(value or 0)


def prune_orphans(session: Session, settings: Settings) -> int:
    """Delete transcode files no ready row points at (and abandoned temps)."""
    base = cache_dir(settings)
    if not base.is_dir():
        return 0
    keep = {
        output_path(settings, item_id, fp)
        for item_id, fp in session.execute(
            select(MediaTranscode.item_id, MediaTranscode.source_fingerprint).where(
                MediaTranscode.state == TRANSCODE_READY
            )
        ).all()
    }
    removed = 0
    stale_before = time.time() - _STALE_TMP_SECONDS
    for path in base.glob("*/*"):
        with contextlib.suppress(OSError):
            if path.name.startswith(".tmp-"):
                if path.stat().st_mtime < stale_before:
                    path.unlink()
                    removed += 1
            elif path not in keep:
                path.unlink()
                removed += 1
    return removed


def counts(session: Session) -> dict[str, int]:
    states = dict(
        session.execute(
            select(MediaTranscode.state, func.count(MediaTranscode.id)).group_by(
                MediaTranscode.state
            )
        ).all()
    )
    return {
        "queued_transcodes": int(states.get(TRANSCODE_QUEUED, 0)),
        "running_transcodes": int(states.get(TRANSCODE_RUNNING, 0)),
    }
