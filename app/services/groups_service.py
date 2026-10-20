"""Group management: lifecycle, membership and group roles.

A group never carries permissions by itself — like users it holds roles — so
deleting a group or pulling a role off it can remove administrative power from
its members. Every mutation therefore re-checks the last-administrator guard.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Role, User
from app.models.group import Group
from app.services import audit
from app.services.security import (
    effective_permissions_for_user,
)
from app.services.users_service import count_admin_users

GROUP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def validate_group_name(name: str) -> str:
    value = name.strip().lower()
    if not GROUP_NAME_RE.fullmatch(value):
        raise ValueError(
            "group name must be 1-64 characters: lowercase letters, digits, dots, "
            "underscores or hyphens, starting with a letter or digit"
        )
    return value


def get_group_or_404(session: Session, group_id: int) -> Group:
    group = session.get(Group, group_id)
    if group is None:
        raise NotFoundError("Group not found", details={"group_id": group_id})
    return group


def list_groups(session: Session) -> list[Group]:
    return list(session.execute(select(Group).order_by(Group.name)).scalars().all())


def _ensure_admins_survive(session: Session) -> None:
    if count_admin_users(session) == 0:
        raise ConflictError("Refusing: this would leave the system without an administrator")


def create_group(session: Session, *, name: str, description: str | None, actor: User) -> Group:
    try:
        value = validate_group_name(name)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    existing = session.execute(select(Group).where(Group.name == value)).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("Group name is already taken", details={"name": value})
    group = Group(name=value, description=(description or "").strip())
    session.add(group)
    session.commit()
    audit.record_audit_event(
        session,
        event=audit.GROUP_CREATED,
        category=audit.CATEGORY_GROUP,
        actor=actor,
        target_type="group",
        target_id=group.id,
        details={"name": value},
        commit=False,
    )
    session.commit()
    return group


def update_group(session: Session, group: Group, *, description: str | None, actor: User) -> Group:
    new_desc = (description or "").strip()
    if new_desc == group.description:
        return group
    group.description = new_desc
    audit.record_audit_event(
        session,
        event=audit.GROUP_UPDATED,
        category=audit.CATEGORY_GROUP,
        actor=actor,
        target_type="group",
        target_id=group.id,
        commit=False,
    )
    session.commit()
    return group


def delete_group(session: Session, group: Group, *, actor: User) -> None:
    session.delete(group)
    session.flush()
    _ensure_admins_survive(session)
    audit.record_audit_event(
        session,
        event=audit.GROUP_DELETED,
        category=audit.CATEGORY_GROUP,
        actor=actor,
        target_type="group",
        target_id=group.id,
        details={"name": group.name},
        commit=False,
    )
    session.commit()


def replace_members(session: Session, group: Group, *, user_ids: list[int], actor: User) -> Group:
    users = session.execute(select(User).where(User.id.in_(user_ids))).scalars().all()
    if len(users) != len(set(user_ids)):
        raise NotFoundError("Some users do not exist")
    group.members = list(users)
    session.flush()
    _ensure_admins_survive(session)
    audit.record_audit_event(
        session,
        event=audit.GROUP_MEMBERS_CHANGED,
        category=audit.CATEGORY_GROUP,
        actor=actor,
        target_type="group",
        target_id=group.id,
        details={"user_ids": sorted(int(u.id) for u in users)},
        commit=False,
    )
    session.commit()
    return group


def replace_group_roles(
    session: Session, group: Group, *, role_names: list[str], actor: User
) -> Group:
    names = [name.strip().lower() for name in role_names]
    rows = session.execute(select(Role).where(Role.name.in_(names))).scalars().all()
    by_name = {role.name: role for role in rows}
    missing = sorted(set(names) - set(by_name))
    if missing:
        raise NotFoundError("Unknown role", details={"roles": missing})
    group.roles = [by_name[name] for name in names]
    session.flush()
    _ensure_admins_survive(session)
    audit.record_audit_event(
        session,
        event=audit.GROUP_ROLES_CHANGED,
        category=audit.CATEGORY_ROLE,
        actor=actor,
        target_type="group",
        target_id=group.id,
        details={"roles": sorted(by_name[n].name for n in names)},
        commit=False,
    )
    session.commit()
    return group


def permissions_of_group_members(session: Session, group: Group) -> dict[int, frozenset[str]]:
    """Effective permissions of every member (used by the UI/tests)."""
    return {user.id: effective_permissions_for_user(session, user) for user in group.members}
