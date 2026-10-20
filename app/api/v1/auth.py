from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, get_current_user, request_audit_context
from app.core.config import Settings
from app.core.errors import UnauthorizedError
from app.db.dependencies import get_db
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    SessionUser,
)
from app.schemas.identity import GroupSummary
from app.schemas.user import UserDetail
from app.services import auth_service
from app.services.security import effective_permissions_for_user
from app.services.users_service import change_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookies(
    response: Response, request: Request, raw_token: str, csrf_token: str
) -> None:
    settings: Settings = request.app.state.settings
    response.set_cookie(
        settings.session_cookie_name,
        raw_token,
        max_age=settings.session_idle_max_seconds,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.session_cookie_samesite,
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=settings.csrf_max_age_seconds,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=False,
        samesite=settings.session_cookie_samesite,
    )


def _clear_auth_cookies(response: Response, request: Request) -> None:
    settings: Settings = request.app.state.settings
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    settings: Settings = request.app.state.settings
    ctx = request_audit_context(request)
    result = auth_service.authenticate(
        db,
        username=payload.username,
        password=payload.password,
        ip_address=ctx.get("ip_address"),
        user_agent=ctx.get("user_agent"),
        settings=settings,
        throttle=request.app.state.throttle,
    )
    if not result.ok or result.user is None or result.raw_session_token is None:
        raise UnauthorizedError(result.error_message or "Invalid username or password")
    user = result.user
    _set_auth_cookies(response, request, result.raw_session_token, result.csrf_token or "")
    permissions = effective_permissions_for_user(db, user)
    roles = sorted(role.name for role in user.roles)
    return LoginResponse(
        user=UserDetail.model_validate(user),
        roles=roles,
        permissions=sorted(permissions),
        csrf_token=result.csrf_token or "",
    )


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    auth_service.logout(db, current.session, actor=current.user)
    _clear_auth_cookies(response, request)
    response.status_code = 204


@router.get("/me", response_model=MeResponse)
def me(current: CurrentUser = Depends(get_current_user)) -> MeResponse:
    user = current.user
    return MeResponse(
        user=SessionUser(id=user.id, username=user.username, display_name=user.display_name),
        roles=sorted(role.name for role in user.roles),
        groups=[GroupSummary.model_validate(g) for g in user.groups],
        permissions=sorted(current.permissions),
        session_expires_at=current.session.expires_at.isoformat(),
    )


@router.post("/change-password", response_model=MeResponse)
def change_own_password(
    payload: ChangePasswordRequest,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MeResponse:
    settings: Settings = request.app.state.settings
    user = change_password(
        db,
        current.user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        settings=settings,
    )
    return MeResponse(
        user=SessionUser(id=user.id, username=user.username, display_name=user.display_name),
        roles=sorted(role.name for role in user.roles),
        groups=[GroupSummary.model_validate(g) for g in user.groups],
        permissions=sorted(current.permissions),
        session_expires_at=current.session.expires_at.isoformat(),
    )
