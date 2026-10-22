from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, request_audit_context, require_permission
from app.core.config import Settings
from app.db.dependencies import get_db
from app.schemas.samba import (
    SambaApplyResult,
    SambaShareCreate,
    SambaShareDetail,
    SambaShareUpdate,
    SambaStatus,
)
from app.services import samba as samba_service

router = APIRouter(prefix="/samba", tags=["samba"])


@router.get("/shares", response_model=list[SambaShareDetail])
def list_shares(
    current: CurrentUser = Depends(require_permission("samba:read")),
    db: Session = Depends(get_db),
) -> list[SambaShareDetail]:
    return [SambaShareDetail.model_validate(share) for share in samba_service.list_shares(db)]


@router.post("/shares", response_model=SambaShareDetail, status_code=201)
def create_share(
    payload: SambaShareCreate,
    request: Request,
    current: CurrentUser = Depends(require_permission("samba:write")),
    db: Session = Depends(get_db),
) -> SambaShareDetail:
    share = samba_service.create_share(
        db,
        name=payload.name,
        path=payload.path,
        comment=payload.comment,
        read_only=payload.read_only,
        browseable=payload.browseable,
        guest_ok=payload.guest_ok,
        valid_users=payload.valid_users,
        write_list=payload.write_list,
        enabled=payload.enabled,
        actor=current.user,
        context=request_audit_context(request),
    )
    return SambaShareDetail.model_validate(share)


@router.get("/shares/{share_id}", response_model=SambaShareDetail)
def get_share(
    share_id: int,
    current: CurrentUser = Depends(require_permission("samba:read")),
    db: Session = Depends(get_db),
) -> SambaShareDetail:
    return SambaShareDetail.model_validate(samba_service.get_share_or_404(db, share_id))


@router.patch("/shares/{share_id}", response_model=SambaShareDetail)
def update_share(
    share_id: int,
    payload: SambaShareUpdate,
    request: Request,
    current: CurrentUser = Depends(require_permission("samba:write")),
    db: Session = Depends(get_db),
) -> SambaShareDetail:
    share = samba_service.get_share_or_404(db, share_id)
    updated = samba_service.update_share(
        db,
        share,
        path=payload.path,
        comment=payload.comment,
        read_only=payload.read_only,
        browseable=payload.browseable,
        guest_ok=payload.guest_ok,
        valid_users=payload.valid_users,
        write_list=payload.write_list,
        enabled=payload.enabled,
        actor=current.user,
        context=request_audit_context(request),
    )
    return SambaShareDetail.model_validate(updated)


@router.delete("/shares/{share_id}", status_code=204)
def delete_share(
    share_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("samba:write")),
    db: Session = Depends(get_db),
) -> None:
    share = samba_service.get_share_or_404(db, share_id)
    samba_service.delete_share(
        db, share, actor=current.user, context=request_audit_context(request)
    )


@router.get("/status", response_model=SambaStatus)
def get_status(
    request: Request,
    current: CurrentUser = Depends(require_permission("samba:read")),
    db: Session = Depends(get_db),
) -> SambaStatus:
    settings: Settings = request.app.state.settings
    return SambaStatus(**samba_service.samba_status(db, settings))


@router.post("/apply", response_model=SambaApplyResult)
def apply_config(
    request: Request,
    current: CurrentUser = Depends(require_permission("samba:write")),
    db: Session = Depends(get_db),
) -> SambaApplyResult:
    settings: Settings = request.app.state.settings
    result = samba_service.apply_config(
        db, settings, actor=current.user, context=request_audit_context(request)
    )
    return SambaApplyResult(**result)
