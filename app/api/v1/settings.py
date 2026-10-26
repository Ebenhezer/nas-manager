from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, request_audit_context, require_permission
from app.core.config import Settings
from app.db.dependencies import get_db
from app.schemas.settings import RuntimeSettings, RuntimeSettingsUpdate
from app.services import settings_service

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=RuntimeSettings)
def get_runtime_settings(
    request: Request,
    current: CurrentUser = Depends(require_permission("settings:read")),
    db: Session = Depends(get_db),
) -> RuntimeSettings:
    settings: Settings = request.app.state.settings
    return RuntimeSettings(
        operator_view_files_samba=settings_service.operator_files_samba_view_enabled(db),
        public_upload_enabled=settings_service.public_uploads_enabled(db, settings),
    )


@router.put("", response_model=RuntimeSettings)
def update_runtime_settings(
    payload: RuntimeSettingsUpdate,
    request: Request,
    current: CurrentUser = Depends(require_permission("settings:write")),
    db: Session = Depends(get_db),
) -> RuntimeSettings:
    settings: Settings = request.app.state.settings
    if payload.operator_view_files_samba is not None:
        settings_service.set_operator_files_samba_view(
            db,
            enabled=payload.operator_view_files_samba,
            actor=current.user,
            **request_audit_context(request),
        )
    if payload.public_upload_enabled is not None:
        settings_service.set_public_uploads_enabled(
            db,
            enabled=payload.public_upload_enabled,
            actor=current.user,
            **request_audit_context(request),
        )
    return RuntimeSettings(
        operator_view_files_samba=settings_service.operator_files_samba_view_enabled(db),
        public_upload_enabled=settings_service.public_uploads_enabled(db, settings),
    )
