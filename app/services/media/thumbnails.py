"""The thumbnail cache.

Thumbnails live under ``<NAS_MEDIA_CACHE_DIR>/thumbs/<id % 256>/`` as
``<item id>-<fingerprint>-<size>.webp``. The fingerprint is derived from the
source file's size and mtime, so a changed photo never gets a stale
thumbnail even before the next scan notices the change. Everything here is
disposable: deleting the cache directory only costs regeneration time.

The worker renders pending thumbnails in batches; the thumbnail endpoint
also renders a missing one on demand, so the gallery works without the
worker (just more slowly on first view). Video thumbnails (Phase 9) are
made from a poster frame that ffmpeg extracts.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
import time
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.models import MediaItem, MediaLibrary
from app.models.media import (
    KIND_IMAGE,
    KIND_VIDEO,
    THUMB_FAILED,
    THUMB_PENDING,
    THUMB_READY,
    THUMB_UNSUPPORTED,
)
from app.services.media import ffmpeg, images, libraries, video

logger = logging.getLogger(__name__)

SIZES = tuple(images.THUMB_SIZES)
# Formats Pillow is not built to decode here (HEIC needs a plugin).
_UNDECODABLE_MIME = {"image/heic", "image/heif"}


def fingerprint(item: MediaItem) -> str:
    raw = f"{item.size}:{item.mtime_ns}".encode()
    return hashlib.blake2s(raw, digest_size=6).hexdigest()


def thumb_dir(settings: Settings, item_id: int) -> Path:
    return settings.resolved_media_cache_dir / "thumbs" / f"{item_id % 256:02x}"


def thumb_path(settings: Settings, item: MediaItem, size: str) -> Path:
    return thumb_dir(settings, item.id) / f"{item.id}-{fingerprint(item)}-{size}.webp"


def remove_cached(settings: Settings, item_id: int) -> None:
    folder = thumb_dir(settings, item_id)
    if folder.is_dir():
        for path in folder.glob(f"{item_id}-*"):
            path.unlink(missing_ok=True)


def is_supported(item: MediaItem) -> bool:
    return item.kind == KIND_IMAGE and item.mime not in _UNDECODABLE_MIME


def _render_poster(
    settings: Settings, source: Path, item: MediaItem, targets: dict[str, Path]
) -> None:
    """Grab a frame with ffmpeg (Phase 9) and thumbnail it like a photo."""
    folder = thumb_dir(settings, item.id)
    folder.mkdir(parents=True, exist_ok=True, mode=0o750)
    poster = folder / f".tmp-{uuid.uuid4().hex}.jpg"
    try:
        offset = video.poster_offset(item.duration_seconds)
        ffmpeg.extract_poster(settings, source, at_seconds=offset, output=poster)
        images.make_thumbnails(poster, targets, max_pixels=settings.media_max_image_pixels)
    finally:
        poster.unlink(missing_ok=True)


def render(session: Session, settings: Settings, item: MediaItem, library: MediaLibrary) -> bool:
    """Render every size for ``item`` and record the outcome (caller commits)."""
    is_video = item.kind == KIND_VIDEO and ffmpeg.available(settings)
    if not (is_supported(item) or is_video):
        item.thumb_state = THUMB_UNSUPPORTED
        item.thumb_error = None
        return False
    targets = {size: thumb_path(settings, item, size) for size in SIZES}
    try:
        source = libraries.item_path(settings, library, item)
        if is_video:
            _render_poster(settings, source, item, targets)
        else:
            images.make_thumbnails(source, targets, max_pixels=settings.media_max_image_pixels)
    except (images.ImageError, ffmpeg.FFmpegError, AppError, OSError) as exc:
        item.thumb_state = THUMB_FAILED
        item.thumb_error = str(exc)[:500]
        return False
    item.thumb_state = THUMB_READY
    item.thumb_error = None
    return True


def ensure_thumbnail(
    session: Session, settings: Settings, item: MediaItem, size: str
) -> Path | None:
    """The cached thumbnail, rendered now if missing; None when unavailable."""
    path = thumb_path(settings, item, size)
    if path.is_file():
        return path
    if item.thumb_state in {THUMB_FAILED, THUMB_UNSUPPORTED}:
        return None
    library = session.get(MediaLibrary, item.library_id)
    if library is None:  # pragma: no cover - cascades make this unreachable
        return None
    render(session, settings, item, library)
    session.commit()
    return path if path.is_file() else None


def process_pending(
    session: Session, settings: Settings, *, limit: int, stop: threading.Event | None = None
) -> int:
    """Render up to ``limit`` pending thumbnails; returns how many were tried."""
    rows = list(
        session.execute(
            select(MediaItem, MediaLibrary)
            .join(MediaLibrary, MediaLibrary.id == MediaItem.library_id)
            .where(MediaItem.thumb_state == THUMB_PENDING)
            .order_by(MediaItem.id)
            .limit(limit)
        ).all()
    )
    done = 0
    for item, library in rows:
        if stop is not None and stop.is_set():
            break
        render(session, settings, item, library)
        session.commit()
        done += 1
    return done


def prune_orphans(session: Session, settings: Settings) -> int:
    """Delete cache files whose item is gone or whose source has changed."""
    base = settings.resolved_media_cache_dir / "thumbs"
    if not base.is_dir():
        return 0
    current = {
        item_id: fingerprint(MediaItem(size=size, mtime_ns=mtime_ns))
        for item_id, size, mtime_ns in session.execute(
            select(MediaItem.id, MediaItem.size, MediaItem.mtime_ns)
        ).all()
    }
    removed = 0
    stale_before = time.time() - 3600
    for path in base.glob("*/*"):
        if path.name.startswith(".tmp-"):
            # A render in progress writes here; only abandoned temps go.
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < stale_before:
                    path.unlink()
                    removed += 1
            continue
        item_id_text, _, rest = path.name.partition("-")
        fp = rest.split("-", 1)[0]
        keep = item_id_text.isdigit() and current.get(int(item_id_text)) == fp
        if not keep:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
