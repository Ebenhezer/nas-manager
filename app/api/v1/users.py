from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, require_permission
from app.core.config import Settings
from app.db.dependencies import get_db
from app.models import User
from app.schemas.user import (
    PasswordResetRequest,
    UserActiveUpdate,
    UserCreate,
    UserDetail,
    UserRolesUpdate,
    UserUpdate,
)
from app.services import users_service

router = APIRouter(prefix="/users", tags=["users"])


def _detail(user: User) -> UserDetail:
    return UserDetail.model_validate(user)


@router.get("", response_model=list[UserDetail])
def list_users(
    current: CurrentUser = Depends(require_permission("users:read")),
    db: Session = Depends(get_db),
) -> list[UserDetail]:
    return [_detail(user) for user in users_service.list_users(db)]


@router.post("", response_model=UserDetail, status_code=201)
def create_user(
    payload: UserCreate,
    request: Request,
    current: CurrentUser = Depends(require_permission("users:write")),
    db: Session = Depends(get_db),
) -> UserDetail:
    settings: Settings = request.app.state.settings
    user = users_service.create_user(
        db,
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
        email=payload.email,
        role_names=payload.roles,
        groups=payload.groups,
        settings=settings,
        actor=current.user,
    )
    return _detail(user)


@router.get("/{user_id}", response_model=UserDetail)
def get_user(
    user_id: int,
    current: CurrentUser = Depends(require_permission("users:read")),
    db: Session = Depends(get_db),
) -> UserDetail:
    return _detail(users_service.get_user_or_404(db, user_id))


@router.patch("/{user_id}", response_model=UserDetail)
def update_user(
    user_id: int,
    payload: UserUpdate,
    current: CurrentUser = Depends(require_permission("users:write")),
    db: Session = Depends(get_db),
) -> UserDetail:
    user = users_service.get_user_or_404(db, user_id)
    return _detail(
        users_service.update_user_profile(
            db, user, display_name=payload.display_name, email=payload.email, actor=current.user
        )
    )


@router.patch("/{user_id}/active", response_model=UserDetail)
def set_user_active(
    user_id: int,
    payload: UserActiveUpdate,
    current: CurrentUser = Depends(require_permission("users:write")),
    db: Session = Depends(get_db),
) -> UserDetail:
    user = users_service.get_user_or_404(db, user_id)
    return _detail(
        users_service.set_user_active(db, user, active=payload.active, actor=current.user)
    )


@router.post("/{user_id}/unlock", response_model=UserDetail)
def unlock_user(
    user_id: int,
    current: CurrentUser = Depends(require_permission("users:write")),
    db: Session = Depends(get_db),
) -> UserDetail:
    user = users_service.get_user_or_404(db, user_id)
    return _detail(users_service.unlock_user(db, user, actor=current.user))


@router.post("/{user_id}/roles", response_model=UserDetail)
def assign_user_roles(
    user_id: int,
    payload: UserRolesUpdate,
    current: CurrentUser = Depends(require_permission("users:write")),
    db: Session = Depends(get_db),
) -> UserDetail:
    user = users_service.get_user_or_404(db, user_id)
    return _detail(
        users_service.assign_user_roles(db, user, role_names=payload.roles, actor=current.user)
    )


@router.post("/{user_id}/reset-password", response_model=UserDetail)
def reset_password(
    user_id: int,
    payload: PasswordResetRequest,
    request: Request,
    current: CurrentUser = Depends(require_permission("users:write")),
    db: Session = Depends(get_db),
) -> UserDetail:
    settings: Settings = request.app.state.settings
    user = users_service.get_user_or_404(db, user_id)
    return _detail(
        users_service.reset_password(
            db, user, new_password=payload.new_password, settings=settings, actor=current.user
        )
    )


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    current: CurrentUser = Depends(require_permission("users:write")),
    db: Session = Depends(get_db),
) -> None:
    user = users_service.get_user_or_404(db, user_id)
    users_service.delete_user(db, user, actor=current.user)
