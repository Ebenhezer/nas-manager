"""Sync cursors (``files:read``) and the public client discovery document (Phase 6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app import __version__
from app.api.v1.deps import CurrentUser, require_permission
from app.core.config import Settings
from app.db.dependencies import get_db
from app.schemas.sync import FileChangeOut, ServerInfo, ServerLimits, SyncChanges, SyncCursor
from app.services import files, sync
from app.services.media import transcode

router = APIRouter(prefix="/sync", tags=["sync"])
info_router = APIRouter(tags=["info"])

API_VERSION = "1"
FEATURES = [
    "api_tokens",
    "resumable_uploads",
    "sync_cursors",
    "range_downloads",
    "remote_sync",
    "media_gallery",
    "video_streaming",
]


@router.get("/cursor", response_model=SyncCursor)
def get_cursor(
    current: CurrentUser = Depends(require_permission("files:read")),
    db: Session = Depends(get_db),
) -> SyncCursor:
    """Take this *before* listing a root; then poll ``/sync/changes`` with it."""
    return SyncCursor(cursor=sync.current_cursor(db))


@router.get("/changes", response_model=SyncChanges)
def get_changes(
    request: Request,
    root: str = Query(...),
    cursor: str = Query(min_length=1, max_length=32),
    limit: int = Query(default=500, ge=1, le=1000),
    current: CurrentUser = Depends(require_permission("files:read")),
    db: Session = Depends(get_db),
) -> SyncChanges:
    settings: Settings = request.app.state.settings
    managed_root = str(files.resolve_root(settings, root))
    page = sync.changes_since(db, root=managed_root, cursor=cursor, limit=limit)
    return SyncChanges(
        root=managed_root,
        cursor=page.cursor,
        has_more=page.has_more,
        reset=page.reset,
        changes=[FileChangeOut.model_validate(row) for row in page.changes],
    )


@info_router.get("/info", response_model=ServerInfo)
def server_info(request: Request) -> ServerInfo:
    settings: Settings = request.app.state.settings
    return ServerInfo(
        name=settings.app_name,
        version=__version__,
        api_version=API_VERSION,
        features=[*FEATURES, *(["video_transcoding"] if transcode.is_available(settings) else [])],
        auth_schemes=["session", "bearer"],
        limits=ServerLimits(
            max_upload_bytes=settings.max_upload_bytes,
            resumable_chunk_max_bytes=settings.resumable_chunk_max_bytes,
            resumable_upload_ttl_hours=settings.resumable_upload_ttl_hours,
            sync_change_retention_days=settings.sync_change_retention_days,
        ),
    )
