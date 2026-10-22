"""Effective permissions and guards shared by the auth services."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Role, User
from app.services.settings_service import operator_files_samba_view_enabled


def effective_permissions_for_user(session: Session, user: User) -> frozenset[str]:
    """Union of the permissions granted directly to the user's roles and to
    their groups' roles. Nothing else counts — except the runtime policy
    toggle that lets holders of the built-in `operator` role view the Files
    and Samba pages read-only (admin controls it in Settings)."""
    role_names = {role.name for role in user.roles}
    for group in user.groups:
        role_names.update(role.name for role in group.roles)
    if not role_names:
        return frozenset()
    rows = session.execute(select(Role.id).where(Role.name.in_(role_names))).scalars().all()
    if not rows:
        return frozenset()
    from app.models.role import RolePermission

    permission_rows = (
        session.execute(select(RolePermission.permission).where(RolePermission.role_id.in_(rows)))
        .scalars()
        .all()
    )
    permissions = set(permission_rows)
    if (
        "operator" in role_names
        and "files:read" not in permissions
        and operator_files_samba_view_enabled(session)
    ):
        # Operators may view but never write Files/Samba. Write tokens are
        # deliberately not granted here; the last-administrator and other
        # guards query role_permissions directly and are unaffected.
        permissions.update(("files:read", "samba:read"))
    return frozenset(permissions)


def username_is_taken(
    session: Session, username: str, *, exclude_user_id: int | None = None
) -> bool:
    stmt = select(User.id).where(User.username == username.lower())
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return session.execute(stmt.limit(1)).first() is not None
