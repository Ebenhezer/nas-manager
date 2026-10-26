"""API routes for the public upload folder and its admin controls (Phase 4).

Two routers share the ``/public/uploads`` prefix:

* ``router_public`` — reachable by *unauthenticated* clients. There is an
  upload endpoint (write) and a policy endpoint (whether uploads are enabled
  and how large a file may be); there is deliberately **no** public listing,
  download or search.
* ``router_admin`` — permission-guarded (``uploads:read`` / ``uploads:write``):
  per-uploader status and quotas, file records, single-file deletion and
  cleanup.

An upload counts against the authenticated user's quota (``user:<username>``)
when the request carries a valid session, otherwise against the client IP
(``ip:<address>``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    CurrentUser,
    get_current_user,
    request_audit_context,
    require_permission,
)
from app.core.config import Settings
from app.core.errors import UnauthorizedError
from app.db.dependencies import get_db
from app.models import User
from app.schemas.public_uploads import (
    CleanupRequest,
    CleanupResult,
    PublicUploadFileOut,
    PublicUploadFilesPage,
    PublicUploadPolicy,
    PublicUploadResult,
    PublicUploadStatus,
    QuotaUpdateRequest,
    UploaderSummary,
)
from app.services import public_uploads
from app.services.settings_service import public_uploads_enabled

router_public = APIRouter(prefix="/public/uploads", tags=["public-uploads"])
router_admin = APIRouter(prefix="/public/uploads", tags=["public-uploads-admin"])


def _identity(request: Request, db: Session) -> tuple[str, User | None]:
    """``(uploader_key, actor)`` for this request: the identified session user
    when logged in, otherwise the client IP address. Invalid/expired sessions
    fall back to the IP key rather than failing the public upload."""
    try:
        current = get_current_user(request, db)
    except UnauthorizedError:
        current = None
    if current is not None:
        user = current.user
        return public_uploads.uploader_key_for_user(user.username), user
    client = request.client
    return public_uploads.uploader_key_for_ip(client.host if client is not None else None), None


@router_public.get("", response_model=PublicUploadPolicy)
def public_policy(
    request: Request,
    db: Session = Depends(get_db),
) -> PublicUploadPolicy:
    """Lightweight discovery for clients: may I upload, and how big may files be?"""
    settings: Settings = request.app.state.settings
    return PublicUploadPolicy(
        enabled=public_uploads_enabled(db, settings),
        max_file_bytes=settings.public_upload_max_file_bytes,
    )


@router_public.post("", response_model=PublicUploadResult, status_code=201)
def public_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> PublicUploadResult:
    """Accept one file into the drop folder (upload-only; no auth required)."""
    settings: Settings = request.app.state.settings
    client = request.client
    key, actor = _identity(request, db)
    row = public_uploads.upload(
        db,
        settings,
        uploader_key=key,
        filename=file.filename or "",
        source=file.file,
        ip_address=client.host if client is not None else None,
        actor=actor,
        context=request_audit_context(request),
    )
    return PublicUploadResult.model_validate(row)


@router_admin.get("/status", response_model=PublicUploadStatus)
def admin_status(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current: CurrentUser = Depends(require_permission("uploads:read")),
    db: Session = Depends(get_db),
) -> PublicUploadStatus:
    settings: Settings = request.app.state.settings
    return PublicUploadStatus(**public_uploads.status(db, settings, limit=limit, offset=offset))


@router_admin.get("/files", response_model=PublicUploadFilesPage)
def admin_files(
    uploader_key: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current: CurrentUser = Depends(require_permission("uploads:read")),
    db: Session = Depends(get_db),
) -> PublicUploadFilesPage:
    rows, total = public_uploads.list_files(
        db, uploader_key=uploader_key, limit=limit, offset=offset
    )
    return PublicUploadFilesPage(
        files=[PublicUploadFileOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router_admin.patch("/quotas/{uploader_key}", response_model=UploaderSummary)
def admin_set_quota(
    uploader_key: str,
    payload: QuotaUpdateRequest,
    request: Request,
    current: CurrentUser = Depends(require_permission("uploads:write")),
    db: Session = Depends(get_db),
) -> UploaderSummary:
    row = public_uploads.set_quota(
        db,
        uploader_key=uploader_key,
        quota_bytes=payload.quota_bytes,
        actor=current.user,
        context=request_audit_context(request),
    )
    return public_uploads.summary_for(db, row)


@router_admin.delete("/files/{file_id}", status_code=204)
def admin_delete_file(
    file_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("uploads:write")),
    db: Session = Depends(get_db),
) -> None:
    settings: Settings = request.app.state.settings
    public_uploads.delete_file(
        db,
        settings,
        file_id=file_id,
        actor=current.user,
        context=request_audit_context(request),
    )


@router_admin.post("/cleanup", response_model=CleanupResult)
def admin_cleanup(
    payload: CleanupRequest,
    request: Request,
    current: CurrentUser = Depends(require_permission("uploads:write")),
    db: Session = Depends(get_db),
) -> CleanupResult:
    settings: Settings = request.app.state.settings
    result = public_uploads.cleanup(
        db,
        settings,
        older_than_days=payload.older_than_days,
        actor=current.user,
        context=request_audit_context(request),
    )
    return CleanupResult(**result)
