"""Role management: listing, custom roles and permission grants.

System roles (admin/operator/viewer) are seeded by migration 0002. The API
never renames or deletes them; their permission set is also immutable so the
guaranteed baseline cannot drift. Custom roles may be created and its
permissions edited from the validated tokens in ``app/core/permissions.py``.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.permissions import SYSTEM_ROLES, validate_permissions
from app.models import Role, RolePermission, User
from app.services import audit
from app.services.users_service import count_admin_users

ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def _role_last_admin_check(session: Session) -> None:
    if count_admin_users(session) == 0:
        raise ConflictError("Refusing: this would leave the system without an administrator")


def list_roles(session: Session) -> list[Role]:
    return list(session.execute(select(Role).order_by(Role.id)).scalars().all())


def get_role_or_404(session: Session, role_id: int) -> Role:
    role = session.get(Role, role_id)
    if role is None:
        raise NotFoundError("Role not found", details={"role_id": role_id})
    return role


def _same_or_system(role: Role, *, acting: str) -> None:
    if role.system:
        raise ForbiddenError(f"system role '{role.name}' cannot be {acting} through the API")


def create_role(
    session: Session,
    *,
    name: str,
    description: str | None,
    permissions: list[str],
    actor: User,
) -> Role:
    value = name.strip().lower()
    try:
        if not ROLE_NAME_RE.fullmatch(value):
            raise ValueError(
                "role name must be 2-32 characters: lowercase letters, digits and underscores, "
                "starting with a letter"
            )
        validate_permissions(permissions)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if value in SYSTEM_ROLES:
        raise ConflictError("That name is reserved for a system role", details={"name": value})
    existing = session.execute(select(Role).where(Role.name == value)).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("Role name is already taken", details={"name": value})
    validate_permissions(permissions)
    role = Role(
        name=value,
        description=(description or "").strip(),
        system=False,
        permissions=[RolePermission(permission=perm) for perm in sorted(set(permissions))],
    )
    session.add(role)
    session.commit()
    audit.record_audit_event(
        session,
        event=audit.ROLE_CREATED,
        category=audit.CATEGORY_ROLE,
        actor=actor,
        target_type="role",
        target_id=role.id,
        details={"name": value, "permissions": sorted(set(permissions))},
        commit=False,
    )
    session.commit()
    return role


def update_role_permissions(
    session: Session, role: Role, *, permissions: list[str], actor: User
) -> Role:
    _same_or_system(role, acting="reconfigured")
    try:
        validate_permissions(permissions)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    previous = sorted(p.permission for p in role.permissions)
    role.permissions = [RolePermission(permission=perm) for perm in sorted(set(permissions))]
    session.flush()
    _role_last_admin_check(session)
    audit.record_audit_event(
        session,
        event=audit.ROLE_PERMISSIONS_CHANGED,
        category=audit.CATEGORY_ROLE,
        actor=actor,
        target_type="role",
        target_id=role.id,
        details={"from": previous, "to": sorted(set(permissions))},
        commit=False,
    )
    session.commit()
    return role


def update_role_description(
    session: Session, role: Role, *, description: str | None, actor: User
) -> Role:
    _same_or_system(role, acting="reconfigured")
    new_desc = (description or "").strip()
    if new_desc == role.description:
        return role
    role.description = new_desc
    audit.record_audit_event(
        session,
        event=audit.ROLE_UPDATED,
        category=audit.CATEGORY_ROLE,
        actor=actor,
        target_type="role",
        target_id=role.id,
        commit=False,
    )
    session.commit()
    return role


def delete_role(session: Session, role: Role, *, actor: User) -> None:
    _same_or_system(role, acting="deleted")
    session.delete(role)
    session.flush()
    _role_last_admin_check(session)
    audit.record_audit_event(
        session,
        event=audit.ROLE_DELETED,
        category=audit.CATEGORY_ROLE,
        actor=actor,
        target_type="role",
        target_id=role.id,
        details={"name": role.name},
        commit=False,
    )
    session.commit()
