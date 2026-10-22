from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, request_audit_context, require_permission
from app.db.dependencies import get_db
from app.schemas.settings import RuntimeSettings, RuntimeSettingsUpdate
from app.services import settings_service

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=RuntimeSettings)
def get_runtime_settings(
    current: CurrentUser = Depends(require_permission("settings:read")),
    db: Session = Depends(get_db),
) -> RuntimeSettings:
    return RuntimeSettings(
        operator_view_files_samba=settings_service.operator_files_samba_view_enabled(db)
    )


@router.put("", response_model=RuntimeSettings)
def update_runtime_settings(
    payload: RuntimeSettingsUpdate,
    request: Request,
    current: CurrentUser = Depends(require_permission("settings:write")),
    db: Session = Depends(get_db),
) -> RuntimeSettings:
    settings_service.set_operator_files_samba_view(
        db,
        enabled=payload.operator_view_files_samba,
        actor=current.user,
        **request_audit_context(request),
    )
    return RuntimeSettings(operator_view_files_samba=payload.operator_view_files_samba)
