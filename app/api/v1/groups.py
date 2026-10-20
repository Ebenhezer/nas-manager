from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, require_permission
from app.db.dependencies import get_db
from app.schemas.group import (
    GroupCreate,
    GroupDetail,
    GroupMembersUpdate,
    GroupRolesUpdate,
    GroupUpdate,
)
from app.services import groups_service

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get("", response_model=list[GroupDetail])
def list_groups(
    current: CurrentUser = Depends(require_permission("groups:read")),
    db: Session = Depends(get_db),
) -> list[GroupDetail]:
    return [GroupDetail.model_validate(group) for group in groups_service.list_groups(db)]


@router.post("", response_model=GroupDetail, status_code=201)
def create_group(
    payload: GroupCreate,
    current: CurrentUser = Depends(require_permission("groups:write")),
    db: Session = Depends(get_db),
) -> GroupDetail:
    group = groups_service.create_group(
        db, name=payload.name, description=payload.description, actor=current.user
    )
    return GroupDetail.model_validate(group)


@router.get("/{group_id}", response_model=GroupDetail)
def get_group(
    group_id: int,
    current: CurrentUser = Depends(require_permission("groups:read")),
    db: Session = Depends(get_db),
) -> GroupDetail:
    return GroupDetail.model_validate(groups_service.get_group_or_404(db, group_id))


@router.patch("/{group_id}", response_model=GroupDetail)
def update_group(
    group_id: int,
    payload: GroupUpdate,
    current: CurrentUser = Depends(require_permission("groups:write")),
    db: Session = Depends(get_db),
) -> GroupDetail:
    group = groups_service.get_group_or_404(db, group_id)
    return GroupDetail.model_validate(
        groups_service.update_group(db, group, description=payload.description, actor=current.user)
    )


@router.patch("/{group_id}/members", response_model=GroupDetail)
def replace_group_members(
    group_id: int,
    payload: GroupMembersUpdate,
    current: CurrentUser = Depends(require_permission("groups:write")),
    db: Session = Depends(get_db),
) -> GroupDetail:
    group = groups_service.get_group_or_404(db, group_id)
    return GroupDetail.model_validate(
        groups_service.replace_members(db, group, user_ids=payload.user_ids, actor=current.user)
    )


@router.patch("/{group_id}/roles", response_model=GroupDetail)
def replace_group_roles(
    group_id: int,
    payload: GroupRolesUpdate,
    current: CurrentUser = Depends(require_permission("groups:write")),
    db: Session = Depends(get_db),
) -> GroupDetail:
    group = groups_service.get_group_or_404(db, group_id)
    return GroupDetail.model_validate(
        groups_service.replace_group_roles(db, group, role_names=payload.roles, actor=current.user)
    )


@router.delete("/{group_id}", status_code=204)
def delete_group(
    group_id: int,
    current: CurrentUser = Depends(require_permission("groups:write")),
    db: Session = Depends(get_db),
) -> None:
    group = groups_service.get_group_or_404(db, group_id)
    groups_service.delete_group(db, group, actor=current.user)
