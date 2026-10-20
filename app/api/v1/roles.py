from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, require_permission
from app.db.dependencies import get_db
from app.schemas.role import (
    RoleCreate,
    RoleDescriptionUpdate,
    RoleDetail,
    RolePermissionsUpdate,
)
from app.services import roles_service

router = APIRouter(prefix="/roles", tags=["roles"])


@router.get("", response_model=list[RoleDetail])
def list_roles(
    current: CurrentUser = Depends(require_permission("roles:read")),
    db: Session = Depends(get_db),
) -> list[RoleDetail]:
    return [RoleDetail.model_validate(role) for role in roles_service.list_roles(db)]


@router.post("", response_model=RoleDetail, status_code=201)
def create_role(
    payload: RoleCreate,
    current: CurrentUser = Depends(require_permission("roles:write")),
    db: Session = Depends(get_db),
) -> RoleDetail:
    role = roles_service.create_role(
        db,
        name=payload.name,
        description=payload.description,
        permissions=payload.permissions,
        actor=current.user,
    )
    return RoleDetail.model_validate(role)


@router.patch("/{role_id}", response_model=RoleDetail)
def update_role_description(
    role_id: int,
    payload: RoleDescriptionUpdate,
    current: CurrentUser = Depends(require_permission("roles:write")),
    db: Session = Depends(get_db),
) -> RoleDetail:
    role = roles_service.get_role_or_404(db, role_id)
    return RoleDetail.model_validate(
        roles_service.update_role_description(
            db, role, description=payload.description, actor=current.user
        )
    )


@router.put("/{role_id}/permissions", response_model=RoleDetail)
def update_role_permissions(
    role_id: int,
    payload: RolePermissionsUpdate,
    current: CurrentUser = Depends(require_permission("roles:write")),
    db: Session = Depends(get_db),
) -> RoleDetail:
    role = roles_service.get_role_or_404(db, role_id)
    return RoleDetail.model_validate(
        roles_service.update_role_permissions(
            db, role, permissions=payload.permissions, actor=current.user
        )
    )


@router.delete("/{role_id}", status_code=204)
def delete_role(
    role_id: int,
    current: CurrentUser = Depends(require_permission("roles:write")),
    db: Session = Depends(get_db),
) -> None:
    role = roles_service.get_role_or_404(db, role_id)
    roles_service.delete_role(db, role, actor=current.user)
