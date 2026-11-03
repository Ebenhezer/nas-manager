"""API contracts for the photo gallery and media libraries (Phase 8)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LibraryCreate(BaseModel):
    """A shared library over an existing folder inside a managed file root."""

    name: str = Field(min_length=1, max_length=64)
    root: str = Field(min_length=1, max_length=1024)
    path: str = Field(default="", max_length=4096)
    include_videos: bool = True
    enabled: bool = True
    group_ids: list[int] = Field(default_factory=list, max_length=500)


class LibraryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    include_videos: bool | None = None


class LibraryGroups(BaseModel):
    group_ids: list[int] = Field(max_length=500)


class LibraryCounts(BaseModel):
    image: int
    video: int
    pending_thumbnails: int


class LibraryOut(BaseModel):
    id: int
    name: str
    kind: str
    owner_user_id: int | None
    is_own: bool
    # False for other users' personal libraries an administrator manages.
    browsable: bool
    writable: bool
    root: str
    path: str
    enabled: bool
    include_videos: bool
    group_ids: list[int]
    scan_state: str
    last_scan_at: datetime | None
    last_scan_status: str | None
    last_scan_error: str | None
    last_scan_summary: dict[str, int] | None
    next_scan_at: datetime | None
    counts: LibraryCounts
    created_at: datetime


class MediaItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    library_id: int
    rel_path: str
    name: str
    kind: str
    mime: str
    size: int
    sha256: str | None
    width: int | None
    height: int | None
    taken_at: datetime
    camera_make: str | None
    camera_model: str | None
    orientation: int | None
    gps_lat: float | None
    gps_lon: float | None
    duration_seconds: float | None
    video_codec: str | None
    audio_codec: str | None
    container: str | None
    bitrate: int | None
    rotation: int | None
    # Videos (Phase 9): direct | transcode | unsupported | unknown.
    playback: str | None
    thumb_state: str
    thumb_error: str | None
    indexed_at: datetime


class MediaItemPage(BaseModel):
    items: list[MediaItemOut]
    total: int
    limit: int
    offset: int


class AlbumCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)


class AlbumUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    cover_item_id: int | None = None
    clear_cover: bool = False


class AlbumItems(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=1000)


class AlbumOut(BaseModel):
    id: int
    name: str
    description: str
    cover_item_id: int | None
    item_count: int
    created_at: datetime
    updated_at: datetime


class AlbumDetail(AlbumOut):
    item_ids: list[int]


class AlbumItemsChanged(BaseModel):
    changed: int


class HashLookup(BaseModel):
    """SHA-256 digests (lowercase hex) a client wants to back up."""

    sha256: list[str] = Field(min_length=1, max_length=1000)


class HashLookupResult(BaseModel):
    library_id: int
    # Digests already on the server, with the matching item ids.
    present: dict[str, list[int]]
    missing: list[str]


class MediaChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int
    rel_path: str
    kind: str
    sha256: str | None
    size: int | None
    created_at: datetime


class MediaChanges(BaseModel):
    library_id: int
    cursor: str
    has_more: bool
    reset: bool
    changes: list[MediaChangeOut]


class MediaCursor(BaseModel):
    cursor: str


class MediaStatus(BaseModel):
    libraries: int
    items: int
    queued_scans: int
    running_scans: int
    pending_thumbnails: int
    worker_online: bool
    worker_heartbeat_at: datetime | None
    pillow_version: str
    media_dir: str
    cache_dir: str
    cache_bytes: int
    ffmpeg_available: bool
    ffmpeg_version: str | None
    transcoding_available: bool
    queued_transcodes: int
    running_transcodes: int
    transcode_cache_bytes: int
    transcode_cache_max_bytes: int


class TranscodeOut(BaseModel):
    item_id: int
    # None when no transcode has been requested for the current content.
    state: str | None
    progress_percent: float
    error: str | None
    output_size: int | None
    playback: str | None
    transcoding_available: bool
    created_at: datetime | None
    finished_at: datetime | None


class TranscodeQueued(BaseModel):
    queued: int


ThumbnailSize = Literal["small", "large"]
SortOrder = Literal["taken_desc", "taken_asc", "name", "size_desc"]
