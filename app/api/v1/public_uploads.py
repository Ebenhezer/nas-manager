"""API routes for the public upload folder and its admin controls (Phase 4).

Two routers share the ``/public/uploads`` prefix:

* ``router_public`` — reachable by *unauthenticated* clients. There is an
  upload endpoint (write) and a policy endpoint (whether uploads are enabled
  and how large a file may be); there is deliberately **no** public listing,
  download or search.
* ``router_admin`` — permission-guarded (``uploads:read`` / ``uploads:write``):
  per-uploader status and quotas, file records, single-file deletion and
  cleanup.

An upload counts against the API token's own quota (``token:<id>``) when the
request carries a bearer token, against the authenticated user's quota
(``user:<username>``) when it carries a valid session, otherwise against the
client IP (``ip:<address>``).

``PublicUploadGuardMiddleware`` (Phase 14) admits ``POST /public/uploads``
before FastAPI parses the multipart body, which it would otherwise spool to
disk in full: a declared ``Content-Length`` above the per-file cap, an
uploader over its request rate or with no quota left is refused without
reading the body. The route's own checks stay authoritative.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.api.v1.deps import (
    CurrentUser,
    DenialThrottle,
    bearer_token,
    get_current_user,
    request_audit_context,
    require_permission,
)
from app.core.config import Settings
from app.core.errors import (
    AppError,
    PayloadTooLargeError,
    QuotaExceededError,
    TooManyRequestsError,
    UnauthorizedError,
    error_response,
)
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
from app.services import audit, public_uploads
from app.services.settings_service import public_uploads_enabled
from app.services.throttle import SlidingWindowThrottle

router_public = APIRouter(prefix="/public/uploads", tags=["public-uploads"])
router_admin = APIRouter(prefix="/public/uploads", tags=["public-uploads-admin"])

PUBLIC_UPLOAD_PATH = "/api/v1/public/uploads"
# Multipart framing around the file (boundaries, part headers, a filename of
# at most 255 bytes) is far below this; a larger declared body cannot hold
# a file within the per-file cap.
_MULTIPART_ALLOWANCE = 64 * 1024


def _identity(request: Request, db: Session) -> tuple[str, User | None]:
    """``(uploader_key, actor)`` for this request: the API token or the
    session user when identified, otherwise the client IP address.
    Invalid/expired sessions fall back to the IP key rather than failing the
    public upload; an invalid bearer token is an error (401), because a
    client that sent one expects to be charged to that token's quota."""
    try:
        current = get_current_user(request, db)
    except UnauthorizedError:
        if bearer_token(request) is not None:
            raise
        current = None
    if current is not None:
        user = current.user
        if current.token is not None:
            return public_uploads.uploader_key_for_token(current.token.id), user
        return public_uploads.uploader_key_for_user(user.username), user
    client = request.client
    return public_uploads.uploader_key_for_ip(client.host if client is not None else None), None


def _content_length(request: Request) -> int | None:
    value = request.headers.get("content-length", "")
    return int(value) if value.isdigit() else None


class PublicUploadGuardMiddleware(BaseHTTPMiddleware):
    """Pre-body admission for the public drop endpoint (see the module docstring).

    The rate limiter lives in ``app.state.public_upload_throttle`` (``None``
    when disabled). Each uploader's first rejection per window is audited, so
    a flood cannot fill the audit log.
    """

    def __init__(self, app: object, *, audit_window_seconds: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._audited = DenialThrottle(window_seconds=audit_window_seconds)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == "POST" and request.url.path == PUBLIC_UPLOAD_PATH:
            rejection = await run_in_threadpool(self._admit, request)
            if rejection is not None:
                return error_response(
                    request,
                    rejection.status_code,
                    rejection.code,
                    rejection.message,
                    rejection.details,
                    rejection.headers,
                )
        return await call_next(request)

    def _admit(self, request: Request) -> AppError | None:
        settings: Settings = request.app.state.settings
        cap = settings.public_upload_max_file_bytes
        declared = _content_length(request)
        if declared is not None and declared > cap + _MULTIPART_ALLOWANCE:
            return PayloadTooLargeError(
                f"File exceeds the folder limit of {cap} bytes",
                details={"max_file_bytes": cap},
            )
        throttle: SlidingWindowThrottle | None = request.app.state.public_upload_throttle
        with request.app.state.db.session_factory() as db:
            try:
                key, actor = _identity(request, db)
            except AppError as exc:  # an invalid bearer token: 401 (or 429)
                return exc
            if not public_uploads_enabled(db, settings):
                return None  # the route answers 403
            ctx = request_audit_context(request)
            if throttle is not None:
                state = throttle.check(key)
                if not state.allowed:
                    if self._audited.should_record(("rate", key)):
                        audit.record_audit_event(
                            db,
                            event=audit.PUBLIC_UPLOAD_RATE_LIMITED,
                            category=audit.CATEGORY_PUBLIC_UPLOAD,
                            outcome=audit.OUTCOME_FAILURE,
                            actor=actor,
                            target_type="uploader",
                            target_id=key,
                            details={
                                "uploader_key": key,
                                "max_requests": throttle.max_attempts,
                                "window_seconds": throttle.window_seconds,
                            },
                            **audit.request_context(ctx),
                        )
                    return TooManyRequestsError(
                        "too many uploads; try again later",
                        details={"retry_after_seconds": state.retry_after_seconds},
                        headers={"Retry-After": str(state.retry_after_seconds)},
                    )
            if public_uploads.remaining_quota(db, settings, key) <= 0:
                audit.record_audit_event(
                    db,
                    event=audit.PUBLIC_UPLOAD_REJECTED,
                    category=audit.CATEGORY_PUBLIC_UPLOAD,
                    outcome=audit.OUTCOME_FAILURE,
                    actor=actor,
                    target_type="uploader",
                    target_id=key,
                    details={"reason": "quota", "stage": "admission", "uploader_key": key},
                    **audit.request_context(ctx),
                )
                return QuotaExceededError(
                    "Upload rejected: the per-uploader quota is exhausted",
                    details={"uploader_key": key},
                )
        return None


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
