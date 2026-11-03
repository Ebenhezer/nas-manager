"""Resumable upload routes (Phase 6); the protocol is described in
``app/services/resumable_uploads.py``.

Authentication is a session or an API token. The ``files`` target requires
``files:write`` at every step (so revoking the permission stops an upload in
flight); the ``public`` target is charged to the caller's drop-folder quota.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    CurrentUser,
    get_current_user,
    request_audit_context,
    require_permission,
)
from app.core.config import Settings
from app.core.errors import BadRequestError, ForbiddenError
from app.db.dependencies import get_db
from app.models import UploadSession
from app.models.upload_session import TARGET_FILES
from app.schemas.files import FileEntry
from app.schemas.public_uploads import PublicUploadResult
from app.schemas.uploads import (
    UploadCleanupResult,
    UploadComplete,
    UploadCompleted,
    UploadCreate,
    UploadStatus,
)
from app.services import resumable_uploads

router = APIRouter(prefix="/uploads", tags=["uploads"])


def _status(row: UploadSession, settings: Settings) -> UploadStatus:
    return UploadStatus(
        id=row.id,
        target=row.target,
        filename=row.filename,
        offset=row.received_bytes,
        total_bytes=row.total_bytes,
        chunk_max_bytes=settings.resumable_chunk_max_bytes,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def _offset_headers(response: Response, row: UploadSession) -> None:
    response.headers["Upload-Offset"] = str(row.received_bytes)
    response.headers["Upload-Length"] = str(row.total_bytes)
    response.headers["Cache-Control"] = "no-store"


def _require_target_permission(current: CurrentUser, target: str) -> None:
    if target == TARGET_FILES and "files:write" not in current.permissions:
        raise ForbiddenError(
            "Missing required permission: files:write", details={"permission": "files:write"}
        )


def _owned(request: Request, upload_id: str, current: CurrentUser, db: Session) -> UploadSession:
    row = resumable_uploads.get_owned(db, upload_id, current.user)
    _require_target_permission(current, row.target)
    return row


@router.post("", response_model=UploadStatus, status_code=201)
def create_upload(
    payload: UploadCreate,
    request: Request,
    response: Response,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UploadStatus:
    settings: Settings = request.app.state.settings
    _require_target_permission(current, payload.target)
    row = resumable_uploads.create_upload(
        db,
        settings,
        user=current.user,
        token=current.token,
        target=payload.target,
        filename=payload.filename,
        size=payload.size,
        root=payload.root,
        path=payload.path,
        overwrite=payload.overwrite,
        sha256=payload.sha256,
        context=request_audit_context(request),
    )
    response.headers["Location"] = f"/api/v1/uploads/{row.id}"
    _offset_headers(response, row)
    return _status(row, settings)


@router.post("/cleanup", response_model=UploadCleanupResult)
def cleanup_uploads(
    request: Request,
    current: CurrentUser = Depends(require_permission("files:write")),
    db: Session = Depends(get_db),
) -> UploadCleanupResult:
    settings: Settings = request.app.state.settings
    return UploadCleanupResult(**resumable_uploads.sweep_expired(db, settings))


@router.head("/{upload_id}")
def upload_offset(
    upload_id: str,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    row = _owned(request, upload_id, current, db)
    response = Response(status_code=200)
    _offset_headers(response, row)
    return response


@router.get("/{upload_id}", response_model=UploadStatus)
def upload_status(
    upload_id: str,
    request: Request,
    response: Response,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UploadStatus:
    row = _owned(request, upload_id, current, db)
    _offset_headers(response, row)
    return _status(row, request.app.state.settings)


@router.patch("/{upload_id}", response_model=UploadStatus)
async def upload_chunk(
    upload_id: str,
    request: Request,
    response: Response,
    upload_offset_header: str = Header(alias="Upload-Offset"),
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UploadStatus:
    """Append raw bytes (any content type; ``application/offset+octet-stream``
    is conventional) at ``Upload-Offset``."""
    if not upload_offset_header.isdigit():
        raise BadRequestError("Upload-Offset must be a non-negative integer")
    settings: Settings = request.app.state.settings
    row = _owned(request, upload_id, current, db)
    row = await resumable_uploads.append_chunk(
        db, settings, row, offset=int(upload_offset_header), chunks=request.stream()
    )
    _offset_headers(response, row)
    return _status(row, settings)


@router.post("/{upload_id}/complete", response_model=UploadCompleted)
def complete_upload(
    upload_id: str,
    request: Request,
    payload: UploadComplete | None = None,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UploadCompleted:
    settings: Settings = request.app.state.settings
    row = _owned(request, upload_id, current, db)
    target = row.target
    result = resumable_uploads.complete_upload(
        db,
        settings,
        row,
        actor=current.user,
        sha256=payload.sha256 if payload is not None else None,
        context=request_audit_context(request),
    )
    if isinstance(result, FileEntry):
        return UploadCompleted(target=target, file=result)
    return UploadCompleted(target=target, public=PublicUploadResult.model_validate(result))


@router.delete("/{upload_id}", status_code=204)
def abort_upload(
    upload_id: str,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    row = _owned(request, upload_id, current, db)
    resumable_uploads.abort_upload(
        db,
        request.app.state.settings,
        row,
        actor=current.user,
        context=request_audit_context(request),
    )
