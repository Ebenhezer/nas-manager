"""Photo gallery and media library endpoints (Phase 8).

Every route needs ``media:read``; uploads, deletions and album changes need
``media:write``; shared-library management needs ``media:admin``. Video
streaming and on-demand transcodes (Phase 9) need only ``media:read`` plus
access to the item's library. Item and
album lookups are filtered by what the caller may browse, so anything else
is a 404. API tokens work through the usual scope intersection, which is how
a phone backup client authenticates.
"""

from __future__ import annotations

import re
from datetime import datetime

import PIL
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, request_audit_context, require_permission
from app.core.config import Settings
from app.core.errors import BadRequestError, ForbiddenError, NotFoundError
from app.db.dependencies import get_db
from app.models import MediaAlbum, MediaItem, MediaLibrary
from app.schemas.media import (
    AlbumCreate,
    AlbumDetail,
    AlbumItems,
    AlbumItemsChanged,
    AlbumOut,
    AlbumUpdate,
    HashLookup,
    HashLookupResult,
    LibraryCounts,
    LibraryCreate,
    LibraryGroups,
    LibraryOut,
    LibraryUpdate,
    MediaChangeOut,
    MediaChanges,
    MediaCursor,
    MediaItemOut,
    MediaItemPage,
    MediaStatus,
    SortOrder,
    ThumbnailSize,
    TranscodeOut,
    TranscodeQueued,
)
from app.services import audit, remote_sync
from app.services.media import (
    albums,
    changes,
    ffmpeg,
    jobs,
    libraries,
    search,
    thumbnails,
    transcode,
    uploads,
)
from app.services.media.libraries import MediaActor

router = APIRouter(prefix="/media", tags=["media"])

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_THUMB_CACHE_SECONDS = 86400


def actor_of(current: CurrentUser) -> MediaActor:
    return MediaActor(user=current.user, permissions=current.permissions)


def _library_out(
    session: Session, actor: MediaActor, library: MediaLibrary, counts: LibraryCounts
) -> LibraryOut:
    own = library.is_personal and library.owner_user_id == actor.user.id
    browsable = libraries.is_browsable(session, actor, library.id)
    return LibraryOut(
        id=library.id,
        name=library.name,
        kind=library.kind,
        owner_user_id=library.owner_user_id,
        is_own=own,
        browsable=browsable,
        writable=own and actor.can_write,
        root=library.root,
        path=library.rel_path,
        enabled=library.enabled,
        include_videos=library.include_videos,
        group_ids=libraries.library_group_ids(session, library.id),
        scan_state=library.scan_state,
        last_scan_at=library.last_scan_at,
        last_scan_status=library.last_scan_status,
        last_scan_error=library.last_scan_error,
        last_scan_summary=libraries.scan_summary(library),
        next_scan_at=library.next_scan_at,
        counts=counts,
        created_at=library.created_at,
    )


def _one_library_out(session: Session, actor: MediaActor, library: MediaLibrary) -> LibraryOut:
    counts = libraries.counts_by_library(session, [library.id])[library.id]
    return _library_out(session, actor, library, LibraryCounts(**counts))


# ---------------------------------------------------------------------------
# Libraries
# ---------------------------------------------------------------------------


@router.get("/libraries", response_model=list[LibraryOut])
def list_libraries(
    request: Request,
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> list[LibraryOut]:
    """Browsable libraries (the caller's own first is created on demand);
    administrators also see every other library's record."""
    settings: Settings = request.app.state.settings
    actor = actor_of(current)
    if actor.can_write:
        libraries.ensure_personal_library(db, settings, current.user)
    rows = libraries.visible_libraries(db, actor)
    counts = libraries.counts_by_library(db, [row.id for row in rows])
    result = [_library_out(db, actor, row, LibraryCounts(**counts[row.id])) for row in rows]
    result.sort(key=lambda lib: (not lib.is_own, lib.kind != "shared", lib.name.lower()))
    return result


@router.post("/libraries", response_model=LibraryOut, status_code=201)
def create_library(
    payload: LibraryCreate,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:admin")),
    db: Session = Depends(get_db),
) -> LibraryOut:
    library = libraries.create_shared_library(
        db,
        request.app.state.settings,
        name=payload.name,
        root=payload.root,
        path=payload.path,
        include_videos=payload.include_videos,
        enabled=payload.enabled,
        group_ids=payload.group_ids,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _one_library_out(db, actor_of(current), library)


@router.get("/libraries/{library_id}", response_model=LibraryOut)
def get_library(
    library_id: int,
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> LibraryOut:
    actor = actor_of(current)
    if actor.is_admin:
        library = libraries.get_library_for_admin(db, library_id)
    else:
        library = libraries.get_browsable_library(db, actor, library_id)
    return _one_library_out(db, actor, library)


@router.patch("/libraries/{library_id}", response_model=LibraryOut)
def update_library(
    library_id: int,
    payload: LibraryUpdate,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:admin")),
    db: Session = Depends(get_db),
) -> LibraryOut:
    library = libraries.update_library(
        db,
        libraries.get_library_for_admin(db, library_id),
        name=payload.name,
        enabled=payload.enabled,
        include_videos=payload.include_videos,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _one_library_out(db, actor_of(current), library)


@router.delete("/libraries/{library_id}", status_code=204)
def delete_library(
    library_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:admin")),
    db: Session = Depends(get_db),
) -> None:
    libraries.delete_library(
        db,
        request.app.state.settings,
        libraries.get_library_for_admin(db, library_id),
        actor=current.user,
        context=request_audit_context(request),
    )


@router.put("/libraries/{library_id}/groups", response_model=LibraryOut)
def set_library_groups(
    library_id: int,
    payload: LibraryGroups,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:admin")),
    db: Session = Depends(get_db),
) -> LibraryOut:
    library = libraries.get_library_for_admin(db, library_id)
    libraries.set_library_groups(
        db, library, payload.group_ids, actor=current.user, context=request_audit_context(request)
    )
    return _one_library_out(db, actor_of(current), library)


@router.post("/libraries/{library_id}/scan", response_model=LibraryOut, status_code=202)
def scan_library(
    library_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> LibraryOut:
    """Queue a rescan (the worker runs it). Owner or ``media:admin``."""
    actor = actor_of(current)
    if actor.is_admin:
        library = libraries.get_library_for_admin(db, library_id)
    else:
        library = libraries.get_browsable_library(db, actor, library_id)
    if not libraries.can_scan(library, actor):
        raise ForbiddenError("Only the owner or an administrator can rescan this library")
    libraries.queue_scan(db, library, actor=current.user, context=request_audit_context(request))
    return _one_library_out(db, actor, library)


@router.get("/status", response_model=MediaStatus)
def media_status(
    request: Request,
    current: CurrentUser = Depends(require_permission("media:admin")),
    db: Session = Depends(get_db),
) -> MediaStatus:
    settings: Settings = request.app.state.settings
    worker = remote_sync.worker_status(db, settings)
    cache = settings.resolved_media_cache_dir
    cache_bytes = sum(p.stat().st_size for p in cache.rglob("*") if p.is_file())
    return MediaStatus(
        libraries=int(db.execute(select(func.count(MediaLibrary.id))).scalar_one()),
        items=int(db.execute(select(func.count(MediaItem.id))).scalar_one()),
        **jobs.status(db),
        worker_online=bool(worker["worker_online"]),
        worker_heartbeat_at=worker["worker_heartbeat_at"],
        pillow_version=PIL.__version__,
        media_dir=str(settings.resolved_media_dir),
        cache_dir=str(cache),
        cache_bytes=cache_bytes,
        ffmpeg_available=ffmpeg.available(settings),
        ffmpeg_version=ffmpeg.version(settings),
        transcoding_available=transcode.is_available(settings),
        transcode_cache_bytes=transcode.cache_bytes(db),
        transcode_cache_max_bytes=settings.media_transcode_cache_max_bytes,
    )


@router.post("/libraries/{library_id}/transcode", response_model=TranscodeQueued, status_code=202)
def transcode_library(
    library_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:admin")),
    db: Session = Depends(get_db),
) -> TranscodeQueued:
    """Pre-transcode every video in the library that cannot play directly."""
    queued = transcode.queue_library(
        db,
        request.app.state.settings,
        libraries.get_library_for_admin(db, library_id),
        actor=current.user,
        context=request_audit_context(request),
    )
    return TranscodeQueued(queued=queued)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


@router.get("/items", response_model=MediaItemPage)
def search_items(
    request: Request,
    library_id: int | None = Query(default=None),
    q: str | None = Query(default=None, max_length=255),
    kind: str | None = Query(default=None, pattern="^(image|video)$"),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    camera: str | None = Query(default=None, max_length=64),
    album_id: int | None = Query(default=None),
    has_gps: bool | None = Query(default=None),
    sort: SortOrder = Query(default="taken_desc"),
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> MediaItemPage:
    settings: Settings = request.app.state.settings
    limit = min(limit, settings.media_search_limit)
    filters = search.SearchFilters(
        library_id=library_id,
        q=q or None,
        kind=kind,
        date_from=date_from.replace(tzinfo=None) if date_from else None,
        date_to=date_to.replace(tzinfo=None) if date_to else None,
        camera=camera or None,
        album_id=album_id,
        has_gps=has_gps,
        sort=sort,
    )
    rows, total = search.search_items(db, actor_of(current), filters, limit=limit, offset=offset)
    return MediaItemPage(
        items=[MediaItemOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/items/{item_id}", response_model=MediaItemOut)
def get_item(
    item_id: int,
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> MediaItemOut:
    return MediaItemOut.model_validate(libraries.get_browsable_item(db, actor_of(current), item_id))


@router.get("/items/{item_id}/thumbnail", response_class=FileResponse)
def get_thumbnail(
    item_id: int,
    request: Request,
    size: ThumbnailSize = Query(default="small"),
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> FileResponse:
    item = libraries.get_browsable_item(db, actor_of(current), item_id)
    path = thumbnails.ensure_thumbnail(db, request.app.state.settings, item, size)
    if path is None:
        raise NotFoundError(
            "No thumbnail is available for this item",
            details={"item_id": item_id, "thumb_state": item.thumb_state},
        )
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": f"private, max-age={_THUMB_CACHE_SECONDS}"},
    )


@router.get("/items/{item_id}/original", response_class=FileResponse)
def get_original(
    item_id: int,
    request: Request,
    download: bool = Query(default=False),
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> FileResponse:
    """The original file (``Range`` supported). Its type comes from the
    extension allowlist; only explicit downloads are audited."""
    item = libraries.get_browsable_item(db, actor_of(current), item_id)
    library = db.get(MediaLibrary, item.library_id)
    assert library is not None  # noqa: S101 - the join above guarantees it
    path = libraries.item_path(request.app.state.settings, library, item)
    if not path.is_file():
        raise NotFoundError("The file is no longer on disk", details={"item_id": item_id})
    if download:
        audit.record_audit_event(
            db,
            event=audit.MEDIA_ITEM_DOWNLOADED,
            category=audit.CATEGORY_MEDIA,
            actor=current.user,
            target_type="media_item",
            target_id=item.id,
            details={"library_id": library.id, "path": item.rel_path, "size": item.size},
            **audit.request_context(request_audit_context(request)),
        )
    return FileResponse(
        path,
        media_type=item.mime,
        filename=item.name,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-cache"},
    )


def _transcode_out(db: Session, settings: Settings, item: MediaItem) -> TranscodeOut:
    row = transcode.get_row(db, item.id)
    if row is not None and row.source_fingerprint != thumbnails.fingerprint(item):
        row = None  # made from older content: as good as absent
    return TranscodeOut(
        item_id=item.id,
        state=row.state if row else None,
        progress_percent=row.progress_percent if row else 0.0,
        error=row.error if row else None,
        output_size=row.output_size if row else None,
        playback=item.playback,
        transcoding_available=transcode.is_available(settings),
        created_at=row.created_at if row else None,
        finished_at=row.finished_at if row else None,
    )


@router.get("/items/{item_id}/stream", response_class=FileResponse)
def stream_item(
    item_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Stream a video (``Range`` supported, so players can seek).

    Browser-compatible originals are served as they are; anything else is
    served from its cached transcode, or answered with 409
    ``transcode_required`` (``details`` carries the transcode state).
    """
    item = libraries.get_browsable_item(db, actor_of(current), item_id)
    if item.kind != "video":
        raise BadRequestError("Only videos can be streamed", details={"item_id": item_id})
    path, media_type = transcode.stream_target(db, request.app.state.settings, item)
    if not path.is_file():
        raise NotFoundError("The file is no longer on disk", details={"item_id": item_id})
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "private, no-cache", "Accept-Ranges": "bytes"},
    )


@router.get("/items/{item_id}/transcode", response_model=TranscodeOut)
def get_transcode(
    item_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> TranscodeOut:
    item = libraries.get_browsable_item(db, actor_of(current), item_id)
    return _transcode_out(db, request.app.state.settings, item)


@router.post("/items/{item_id}/transcode", response_model=TranscodeOut, status_code=202)
def request_transcode(
    item_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> TranscodeOut:
    """Queue a browser-compatible transcode (anyone who can watch the video)."""
    settings: Settings = request.app.state.settings
    item = libraries.get_browsable_item(db, actor_of(current), item_id)
    transcode.request(
        db, settings, item, actor=current.user, context=request_audit_context(request)
    )
    return _transcode_out(db, settings, item)


@router.delete("/items/{item_id}", status_code=204)
def delete_item(
    item_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:write")),
    db: Session = Depends(get_db),
) -> None:
    actor = actor_of(current)
    item = libraries.get_browsable_item(db, actor, item_id)
    uploads.delete_item(
        db, request.app.state.settings, actor, item, context=request_audit_context(request)
    )


@router.post("/upload", response_model=MediaItemOut, status_code=201)
def upload_item(
    request: Request,
    file: UploadFile = File(...),
    path: str | None = Form(default=None, max_length=1024),
    taken_at: datetime | None = Form(default=None),
    current: CurrentUser = Depends(require_permission("media:write")),
    db: Session = Depends(get_db),
) -> MediaItemOut:
    """Upload into the caller's personal library (``YYYY/MM`` by default)."""
    item = uploads.upload(
        db,
        request.app.state.settings,
        actor_of(current),
        filename=file.filename or "",
        source=file.file,
        subfolder=path,
        taken_at=taken_at,
        context=request_audit_context(request),
    )
    return MediaItemOut.model_validate(item)


# ---------------------------------------------------------------------------
# Mobile synchronization
# ---------------------------------------------------------------------------


@router.post("/sync/lookup", response_model=HashLookupResult)
def lookup_hashes(
    payload: HashLookup,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> HashLookupResult:
    """Which photos the caller's personal library already holds (by SHA-256)."""
    digests = list(dict.fromkeys(value.strip().lower() for value in payload.sha256))
    bad = [value for value in digests if not _SHA256_RE.match(value)]
    if bad:
        raise BadRequestError("sha256 values must be 64 hexadecimal characters")
    library = libraries.ensure_personal_library(db, request.app.state.settings, current.user)
    present = changes.lookup_hashes(db, library_id=library.id, hashes=digests)
    return HashLookupResult(
        library_id=library.id,
        present=present,
        missing=[value for value in digests if value not in present],
    )


@router.get("/sync/cursor", response_model=MediaCursor)
def get_cursor(
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> MediaCursor:
    """Take this *before* listing a library, then follow ``/media/sync/changes``."""
    return MediaCursor(cursor=changes.current_cursor(db))


@router.get("/sync/changes", response_model=MediaChanges)
def get_changes(
    library_id: int = Query(...),
    cursor: str = Query(min_length=1, max_length=32),
    limit: int = Query(default=500, ge=1, le=1000),
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> MediaChanges:
    libraries.get_browsable_library(db, actor_of(current), library_id)
    page = changes.changes_since(db, library_id=library_id, cursor=cursor, limit=limit)
    return MediaChanges(
        library_id=library_id,
        cursor=page.cursor,
        has_more=page.has_more,
        reset=page.reset,
        changes=[MediaChangeOut.model_validate(row) for row in page.changes],
    )


# ---------------------------------------------------------------------------
# Albums (private to their owner)
# ---------------------------------------------------------------------------


def _album_out(album: MediaAlbum, count: int) -> AlbumOut:
    return AlbumOut(
        id=album.id,
        name=album.name,
        description=album.description,
        cover_item_id=album.cover_item_id,
        item_count=count,
        created_at=album.created_at,
        updated_at=album.updated_at,
    )


def _album_detail(db: Session, actor: MediaActor, album: MediaAlbum) -> AlbumDetail:
    item_ids = albums.visible_item_ids(db, actor, album.id)
    base = _album_out(album, len(item_ids))
    return AlbumDetail(**base.model_dump(), item_ids=item_ids)


@router.get("/albums", response_model=list[AlbumOut])
def list_albums(
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> list[AlbumOut]:
    return [_album_out(album, count) for album, count in albums.list_albums(db, actor_of(current))]


@router.post("/albums", response_model=AlbumDetail, status_code=201)
def create_album(
    payload: AlbumCreate,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:write")),
    db: Session = Depends(get_db),
) -> AlbumDetail:
    actor = actor_of(current)
    album = albums.create_album(
        db,
        actor,
        name=payload.name,
        description=payload.description,
        context=request_audit_context(request),
    )
    return _album_detail(db, actor, album)


@router.get("/albums/{album_id}", response_model=AlbumDetail)
def get_album(
    album_id: int,
    current: CurrentUser = Depends(require_permission("media:read")),
    db: Session = Depends(get_db),
) -> AlbumDetail:
    actor = actor_of(current)
    return _album_detail(db, actor, albums.get_album(db, actor, album_id))


@router.patch("/albums/{album_id}", response_model=AlbumDetail)
def update_album(
    album_id: int,
    payload: AlbumUpdate,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:write")),
    db: Session = Depends(get_db),
) -> AlbumDetail:
    actor = actor_of(current)
    album = albums.update_album(
        db,
        actor,
        albums.get_album(db, actor, album_id),
        name=payload.name,
        description=payload.description,
        cover_item_id=payload.cover_item_id,
        clear_cover=payload.clear_cover,
        context=request_audit_context(request),
    )
    return _album_detail(db, actor, album)


@router.delete("/albums/{album_id}", status_code=204)
def delete_album(
    album_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:write")),
    db: Session = Depends(get_db),
) -> None:
    actor = actor_of(current)
    albums.delete_album(
        db, actor, albums.get_album(db, actor, album_id), context=request_audit_context(request)
    )


@router.post("/albums/{album_id}/items", response_model=AlbumItemsChanged)
def add_album_items(
    album_id: int,
    payload: AlbumItems,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:write")),
    db: Session = Depends(get_db),
) -> AlbumItemsChanged:
    actor = actor_of(current)
    added = albums.add_items(
        db,
        actor,
        albums.get_album(db, actor, album_id),
        payload.item_ids,
        context=request_audit_context(request),
    )
    return AlbumItemsChanged(changed=added)


@router.delete("/albums/{album_id}/items", response_model=AlbumItemsChanged)
def remove_album_items(
    album_id: int,
    payload: AlbumItems,
    request: Request,
    current: CurrentUser = Depends(require_permission("media:write")),
    db: Session = Depends(get_db),
) -> AlbumItemsChanged:
    actor = actor_of(current)
    removed = albums.remove_items(
        db,
        actor,
        albums.get_album(db, actor, album_id),
        payload.item_ids,
        context=request_audit_context(request),
    )
    return AlbumItemsChanged(changed=removed)
