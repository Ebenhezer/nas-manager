"""Photo gallery and personal media libraries (Phase 8).

A ``MediaLibrary`` is a directory whose images (and videos) are indexed.
Every user has one *personal* library (``<NAS_MEDIA_DIR>/<username>``) that
only they can browse; administrators may register *shared* libraries over a
folder inside a managed file root and grant groups read access to them.

``MediaItem`` rows are the index (one per file, metadata extracted from EXIF
and, in Phase 9, ffprobe). Generated thumbnails live in the disposable cache
directory, keyed by item id and a fingerprint of the file's size and mtime.
``MediaAlbum`` rows are per-user collections of items, and ``MediaChange``
is the journal mobile clients follow to mirror a library. ``MediaTranscode``
(Phase 9) tracks the cached browser-compatible MP4 of a video that cannot be
played directly. ``MediaFavorite`` (Phase 15) marks the items a user starred.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow_naive

LIBRARY_PERSONAL = "personal"
LIBRARY_SHARED = "shared"

SCAN_IDLE = "idle"
SCAN_QUEUED = "queued"
SCAN_RUNNING = "running"

SCAN_RESULT_OK = "succeeded"
SCAN_RESULT_FAILED = "failed"

KIND_IMAGE = "image"
KIND_VIDEO = "video"

THUMB_PENDING = "pending"
THUMB_READY = "ready"
THUMB_FAILED = "failed"
THUMB_UNSUPPORTED = "unsupported"

# How a video reaches the browser (Phase 9).
PLAYBACK_DIRECT = "direct"
PLAYBACK_TRANSCODE = "transcode"
PLAYBACK_UNSUPPORTED = "unsupported"
PLAYBACK_UNKNOWN = "unknown"

TRANSCODE_QUEUED = "queued"
TRANSCODE_RUNNING = "running"
TRANSCODE_READY = "ready"
TRANSCODE_FAILED = "failed"

CHANGE_CREATED = "created"
CHANGE_MODIFIED = "modified"
CHANGE_DELETED = "deleted"

# Personal library names are "~<username>"; shared names may not start with "~".
PERSONAL_NAME_PREFIX = "~"

media_library_groups = Table(
    "media_library_groups",
    Base.metadata,
    Column("library_id", ForeignKey("media_libraries.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


class MediaLibrary(Base):
    __tablename__ = "media_libraries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Set for personal libraries only (at most one per user).
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=True
    )
    # Shared: a configured file root and a folder inside it. Personal: the
    # media directory and the username (informational; recomputed on use).
    root: Mapped[str] = mapped_column(String(1024), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    include_videos: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    scan_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SCAN_IDLE, server_default=SCAN_IDLE
    )
    scan_lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scan_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scan_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_scan_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_scan_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
        server_default=func.now(),
    )

    @property
    def is_personal(self) -> bool:
        return self.kind == LIBRARY_PERSONAL


class MediaItem(Base):
    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint("library_id", "rel_path"),
        Index("ix_media_items_library_taken", "library_id", "taken_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library_id: Mapped[int] = mapped_column(
        ForeignKey("media_libraries.id", ondelete="CASCADE"), nullable=False
    )
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    mime: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # EXIF DateTimeOriginal (camera wall clock, no zone) or the file mtime.
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    camera_make: Mapped[str | None] = mapped_column(String(64), nullable=True)
    camera_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    orientation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Video metadata (filled by ffprobe in Phase 9).
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    container: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrate: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rotation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    playback: Mapped[str | None] = mapped_column(String(16), nullable=True)
    thumb_state: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, default=THUMB_PENDING, server_default=THUMB_PENDING
    )
    thumb_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )


class MediaAlbum(Base):
    __tablename__ = "media_albums"
    __table_args__ = (UniqueConstraint("owner_user_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    cover_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
        server_default=func.now(),
    )


class MediaAlbumItem(Base):
    __tablename__ = "media_album_items"

    album_id: Mapped[int] = mapped_column(
        ForeignKey("media_albums.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )


class MediaFavorite(Base):
    """An item one user starred; private to that user, like albums."""

    __tablename__ = "media_favorites"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )


class MediaChange(Base):
    """Journal of index changes; the id is the cursor of ``/media/sync/changes``."""

    __tablename__ = "media_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    library_id: Mapped[int] = mapped_column(
        ForeignKey("media_libraries.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # No foreign key: deleted items keep their journal rows.
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
        default=utcnow_naive,
        server_default=func.now(),
    )


class MediaTranscode(Base):
    """One cached H.264/AAC MP4 rendition of a video (Phase 9)."""

    __tablename__ = "media_transcodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    # Size/mtime fingerprint of the source the output was (or is being) made from.
    source_fingerprint: Mapped[str] = mapped_column(String(32), nullable=False)
    output_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    progress_percent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_naive, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
