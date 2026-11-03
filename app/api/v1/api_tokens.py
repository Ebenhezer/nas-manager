"""API routes for personal API tokens (Phase 6).

``/tokens`` manages the caller's own tokens. Creating and revoking require an
interactive session (``require_session_auth``) so a leaked token cannot mint
more tokens. ``/admin/tokens`` lets holders of ``api_tokens:read`` /
``api_tokens:write`` oversee and revoke every user's tokens.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    CurrentUser,
    get_current_user,
    request_audit_context,
    require_permission,
    require_session_auth,
)
from app.core.config import Settings
from app.db.dependencies import get_db
from app.models import ApiToken
from app.schemas.api_tokens import (
    ApiTokenAdminOut,
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenOut,
    GrantableScopes,
)
from app.services import api_tokens

router = APIRouter(prefix="/tokens", tags=["api-tokens"])
admin_router = APIRouter(prefix="/admin/tokens", tags=["api-tokens-admin"])


def _out(row: ApiToken) -> ApiTokenOut:
    return ApiTokenOut(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        token_prefix=row.token_prefix,
        scopes=row.scopes,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        last_used_ip=row.last_used_ip,
        revoked_at=row.revoked_at,
        active=not row.is_revoked and not row.is_expired,
    )


def _admin_out(row: ApiToken) -> ApiTokenAdminOut:
    return ApiTokenAdminOut(**_out(row).model_dump(), username=row.user.username)


@router.get("", response_model=list[ApiTokenOut])
def list_own_tokens(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ApiTokenOut]:
    return [_out(row) for row in api_tokens.list_tokens(db, user=current.user)]


@router.get("/scopes", response_model=GrantableScopes)
def grantable_scopes(
    request: Request,
    current: CurrentUser = Depends(require_session_auth),
    db: Session = Depends(get_db),
) -> GrantableScopes:
    settings: Settings = request.app.state.settings
    return GrantableScopes(
        scopes=api_tokens.grantable_scopes(db, current.user),
        default_lifetime_days=settings.api_token_default_lifetime_days,
        max_lifetime_days=settings.api_token_max_lifetime_days,
        max_per_user=settings.api_token_max_per_user,
    )


@router.post("", response_model=ApiTokenCreated, status_code=201)
def create_token(
    payload: ApiTokenCreate,
    request: Request,
    current: CurrentUser = Depends(require_session_auth),
    db: Session = Depends(get_db),
) -> ApiTokenCreated:
    settings: Settings = request.app.state.settings
    raw, row = api_tokens.create_token(
        db,
        current.user,
        name=payload.name,
        scopes=payload.scopes,
        expires_in_days=payload.expires_in_days,
        settings=settings,
        context=request_audit_context(request),
    )
    return ApiTokenCreated(**_out(row).model_dump(), token=raw)


@router.delete("/{token_id}", status_code=204)
def revoke_own_token(
    token_id: int,
    request: Request,
    current: CurrentUser = Depends(require_session_auth),
    db: Session = Depends(get_db),
) -> None:
    row = api_tokens.get_token_or_404(db, token_id, owner=current.user)
    api_tokens.revoke_token(db, row, actor=current.user, context=request_audit_context(request))


@admin_router.get("", response_model=list[ApiTokenAdminOut])
def list_all_tokens(
    user_id: int | None = Query(default=None, ge=1),
    current: CurrentUser = Depends(require_permission("api_tokens:read")),
    db: Session = Depends(get_db),
) -> list[ApiTokenAdminOut]:
    return [_admin_out(row) for row in api_tokens.list_tokens(db, user_id=user_id)]


@admin_router.delete("/{token_id}", status_code=204)
def revoke_any_token(
    token_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("api_tokens:write")),
    db: Session = Depends(get_db),
) -> None:
    row = api_tokens.get_token_or_404(db, token_id)
    api_tokens.revoke_token(db, row, actor=current.user, context=request_audit_context(request))
