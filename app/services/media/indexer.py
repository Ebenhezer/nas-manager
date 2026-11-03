"""Indexing a library folder into ``media_items``.

A scan walks the library without following symlinks and skips hidden
entries (which covers the application's own ``.nas-*`` temp files and the
remote-sync archive) and anything outside the image/video allowlists. A
file whose size and mtime are unchanged is not read again; a new or changed
file is hashed and its metadata extracted; a file that disappeared loses its
index row and cached thumbnails. Every change is journaled.

Two guards keep a scan from wiping an index by mistake: a library folder
that is missing fails the scan, and a walk that hit unreadable directories,
was stopped early, or found no files at all where many were indexed (an
unmounted disk leaves an empty mount point) does not remove anything.
Removing index rows never touches files, but it does drop album entries.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.models import MediaItem, MediaLibrary
from app.models.media import (
    CHANGE_CREATED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    KIND_IMAGE,
    KIND_VIDEO,
    PLAYBACK_UNKNOWN,
    PLAYBACK_UNSUPPORTED,
    THUMB_FAILED,
    THUMB_PENDING,
    THUMB_UNSUPPORTED,
)
from app.services.media import (
    changes,
    ffmpeg,
    images,
    libraries,
    thumbnails,
    transcode,
    video,
)

logger = logging.getLogger(__name__)

IMAGE_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
}
VIDEO_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".3gp": "video/3gpp",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".ts": "video/mp2t",
    ".mts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".wmv": "video/x-ms-wmv",
}
_HASH_CHUNK = 1024 * 1024
_COMMIT_EVERY = 100
# An empty walk is suspicious only when this many items were indexed.
_EMPTY_GUARD_MIN_ITEMS = 10


class ScanError(Exception):
    """The library cannot be scanned (folder missing or outside its jail)."""


@dataclass
class ScanResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    failed: int = 0
    complete: bool = True

    def as_dict(self) -> dict[str, int]:
        return {k: int(v) for k, v in asdict(self).items()}


def classify(name: str) -> tuple[str, str] | None:
    """``(kind, mime)`` for an allowlisted extension, else None."""
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_TYPES:
        return KIND_IMAGE, IMAGE_TYPES[suffix]
    if suffix in VIDEO_TYPES:
        return KIND_VIDEO, VIDEO_TYPES[suffix]
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _reset_metadata(item: MediaItem) -> None:
    for field in (
        "width",
        "height",
        "camera_make",
        "camera_model",
        "orientation",
        "gps_lat",
        "gps_lon",
        "duration_seconds",
        "video_codec",
        "audio_codec",
        "container",
        "bitrate",
        "rotation",
        "thumb_error",
    ):
        setattr(item, field, None)


def _extract_image(settings: Settings, item: MediaItem, path: Path) -> None:
    if not thumbnails.is_supported(item):
        item.thumb_state = THUMB_UNSUPPORTED
        return
    try:
        meta = images.read_metadata(path, max_pixels=settings.media_max_image_pixels)
    except images.ImageError as exc:
        item.thumb_state = THUMB_FAILED
        item.thumb_error = str(exc)[:500]
        return
    item.width, item.height = meta.width, meta.height
    item.camera_make, item.camera_model = meta.camera_make, meta.camera_model
    item.orientation = meta.orientation
    item.gps_lat, item.gps_lon = meta.gps_lat, meta.gps_lon
    if meta.taken_at is not None:
        item.taken_at = meta.taken_at
    item.thumb_state = THUMB_PENDING


def _extract_video(settings: Settings, item: MediaItem, path: Path) -> None:
    """ffprobe metadata and the playback mode (Phase 9); posters come later."""
    if not ffmpeg.available(settings):
        item.playback = PLAYBACK_UNKNOWN
        item.thumb_state = THUMB_UNSUPPORTED
        return
    try:
        probe = ffmpeg.probe(settings, path)
    except ffmpeg.FFmpegError as exc:
        item.playback = PLAYBACK_UNSUPPORTED
        item.thumb_state = THUMB_FAILED
        item.thumb_error = str(exc)[:500]
        return
    video.apply_probe(item, probe)
    item.thumb_state = THUMB_PENDING if probe.has_video else THUMB_UNSUPPORTED


def needs_reprobe(settings: Settings, kind: str, playback: str | None) -> bool:
    """Videos indexed while ffprobe was missing are probed once it appears."""
    return (
        kind == KIND_VIDEO and playback in {None, PLAYBACK_UNKNOWN} and ffmpeg.available(settings)
    )


def index_file(
    session: Session,
    settings: Settings,
    library: MediaLibrary,
    base: Path,
    path: Path,
    st: os.stat_result,
    item: MediaItem | None = None,
) -> MediaItem | None:
    """Create or refresh the index row for ``path``; the caller commits.

    Returns None for files outside the allowlists.
    """
    kind_mime = classify(path.name)
    if kind_mime is None:
        return None
    # Read the file before touching the session: an unreadable file must
    # not leave a half-built row behind.
    digest = _sha256(path)
    created = item is None
    if item is None:
        item = MediaItem(library_id=library.id, rel_path=path.relative_to(base).as_posix())
        session.add(item)
    else:
        thumbnails.remove_cached(settings, item.id)
    _reset_metadata(item)
    item.playback = None
    item.name = path.name[:255]
    item.kind, item.mime = kind_mime
    item.size = st.st_size
    item.mtime_ns = st.st_mtime_ns
    item.sha256 = digest
    item.taken_at = datetime.fromtimestamp(st.st_mtime, tz=UTC).replace(tzinfo=None)
    item.indexed_at = utcnow_naive()
    if item.kind == KIND_IMAGE:
        _extract_image(settings, item, path)
    else:
        _extract_video(settings, item, path)
    session.flush()
    changes.record(session, item, CHANGE_CREATED if created else CHANGE_MODIFIED)
    return item


def remove_item(session: Session, settings: Settings, item: MediaItem) -> None:
    """Drop ``item`` from the index (never its file); the caller commits."""
    changes.record(session, item, CHANGE_DELETED)
    item_id = item.id
    session.delete(item)
    thumbnails.remove_cached(settings, item_id)
    transcode.remove_cached(settings, item_id)


def scan_library(
    session: Session,
    settings: Settings,
    library: MediaLibrary,
    *,
    stop: threading.Event | None = None,
) -> ScanResult:
    try:
        base = libraries.library_path(settings, library)
    except Exception as exc:
        raise ScanError(f"library folder is not available: {exc}") from exc
    if not base.is_dir():
        raise ScanError("library folder is missing (unmounted disk or renamed folder?)")

    existing = {
        rel: (item_id, size, mtime_ns)
        for item_id, rel, size, mtime_ns in session.execute(
            select(MediaItem.id, MediaItem.rel_path, MediaItem.size, MediaItem.mtime_ns).where(
                MediaItem.library_id == library.id
            )
        ).all()
    }
    reprobe = {
        item_id
        for item_id, kind, playback in session.execute(
            select(MediaItem.id, MediaItem.kind, MediaItem.playback).where(
                MediaItem.library_id == library.id, MediaItem.kind == KIND_VIDEO
            )
        ).all()
        if needs_reprobe(settings, kind, playback)
    }
    result = ScanResult()
    seen: set[str] = set()
    pending = 0

    def walk_error(error: OSError) -> None:
        result.complete = False
        logger.warning("media scan could not read a directory", extra={"error": str(error)})

    for dirpath, dirnames, filenames in os.walk(base, onerror=walk_error, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if stop is not None and stop.is_set():
            result.complete = False
            break
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            kind_mime = classify(name)
            if kind_mime is None or (kind_mime[0] == KIND_VIDEO and not library.include_videos):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(base).as_posix()
            try:
                st = path.lstat()
            except OSError:
                result.failed += 1
                continue
            if not stat.S_ISREG(st.st_mode):
                continue  # symlinks, sockets, devices
            seen.add(rel)
            known = existing.get(rel)
            unchanged = known is not None and known[1:] == (st.st_size, st.st_mtime_ns)
            if unchanged and known is not None and known[0] not in reprobe:
                result.unchanged += 1
                continue
            row = session.get(MediaItem, known[0]) if known is not None else None
            try:
                index_file(session, settings, library, base, path, st, row)
            except OSError as exc:
                result.failed += 1
                logger.warning("media file could not be indexed", extra={"error": str(exc)})
                continue
            if known is None:
                result.added += 1
            else:
                result.updated += 1
            pending += 1
            if pending >= _COMMIT_EVERY:
                session.commit()
                pending = 0
    session.commit()

    missing = [rel for rel in existing if rel not in seen]
    if not seen and len(existing) >= _EMPTY_GUARD_MIN_ITEMS:
        raise ScanError(
            f"the library folder is empty but {len(existing)} items are indexed; "
            "refusing to drop them (is the disk mounted?)"
        )
    if result.complete and missing:
        for rel in missing:
            row = session.get(MediaItem, existing[rel][0])
            if row is not None:
                remove_item(session, settings, row)
                result.removed += 1
        session.commit()
    return result
