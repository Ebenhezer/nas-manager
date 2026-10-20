"""User management: CRUD, password workflows, role assignment and the
last-administrator guard that keeps the appliance manageable."""

from __future__ import annotations

import re

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.permissions import DEFAULT_USER_ROLE
from app.models import Role, User
from app.services import audit
from app.services.auth_service import revoke_all_user_sessions
from app.services.passwords import (
    hash_password,
    validate_password_policy,
    verify_password,
)
from app.services.security import username_is_taken

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ADMIN_PERMISSION = "users:write"

_ADMIN_COUNT_SQL = text(
    """
    SELECT COUNT(DISTINCT u.id) FROM users u WHERE u.is_active = 1 AND (
      u.id IN (SELECT ur.user_id FROM user_roles ur
                 JOIN role_permissions rp ON rp.role_id = ur.role_id
                WHERE rp.permission = :perm)
      OR
      u.id IN (SELECT ug.user_id FROM user_groups ug
                 JOIN group_roles gr ON gr.group_id = ug.group_id
                 JOIN role_permissions rp ON rp.role_id = gr.role_id
                WHERE rp.permission = :perm)
    )
    """
)


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_username(username: str) -> None:
    if not USERNAME_RE.fullmatch(username):
        raise ValueError(
            "username must be 1-64 characters: lowercase letters, digits, dots, "
            "underscores or hyphens, starting with a letter or digit"
        )


def validate_email(email: str | None) -> None:
    if email is not None and email.strip() and not EMAIL_RE.fullmatch(email.strip()):
        raise ValueError("email address is not valid")


def count_admin_users(session: Session) -> int:
    """Number of active users whose effective permissions include ``users:write``."""
    return int(session.execute(_ADMIN_COUNT_SQL, {"perm": ADMIN_PERMISSION}).scalar_one())


def get_user_or_404(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found", details={"user_id": user_id})
    return user


def list_users(session: Session) -> list[User]:
    return list(session.execute(select(User).order_by(User.username)).scalars().all())


def resolve_role_names(session: Session, role_names: list[str]) -> list[Role]:
    """Fetch roles by name, inventing nothing and preserving the request order.

    When nothing is requested the caller's default role is used.
    """
    names = [name.strip().lower() for name in role_names]
    rows = session.execute(select(Role).where(Role.name.in_(names))).scalars().all()
    by_name = {role.name: role for role in rows}
    missing = sorted(set(names) - set(by_name))
    if missing:
        raise NotFoundError("Unknown role", details={"roles": missing})
    return [by_name[name] for name in names]


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    display_name: str | None,
    email: str | None,
    role_names: list[str],
    groups: list[int],
    settings: Settings,
    actor: User,
) -> User:
    """Create a user. Always grants at least ``viewer``; never fails silently."""
    name = normalize_username(username)
    try:
        validate_username(name)
        validate_email(email)
        validate_password_policy(password, username=name, settings=settings)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if username_is_taken(session, name):
        raise ConflictError("Username is already taken", details={"username": name})

    names = [r.strip().lower() for r in role_names] or [DEFAULT_USER_ROLE]
    roles = resolve_role_names(session, names)
    user = User(
        username=name,
        display_name=(display_name or "").strip()[:128],
        email=(email or "").strip() or None,
        password_hash=hash_password(password, settings),
        is_active=True,
    )
    user.roles = roles
    session.add(user)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.USER_CREATED,
        category=audit.CATEGORY_USER,
        actor=actor,
        target_type="user",
        target_id=user.id,
        details={"username": name, "roles": sorted(r.name for r in roles)},
        commit=False,
    )
    session.commit()
    return user


def update_user_profile(
    session: Session, user: User, *, display_name: str | None, email: str | None, actor: User
) -> User:
    try:
        validate_email(email)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    new_display = (display_name or "").strip()[:128]
    new_email = (email or "").strip() or None
    changes: dict[str, object] = {}
    if new_display != user.display_name:
        changes["display_name"] = new_display
    if new_email != user.email:
        changes["email"] = new_email
    if not changes:
        return user
    user.display_name = new_display
    user.email = new_email
    audit.record_audit_event(
        session,
        event=audit.USER_UPDATED,
        category=audit.CATEGORY_USER,
        actor=actor,
        target_type="user",
        target_id=user.id,
        details=changes,
        commit=False,
    )
    session.commit()
    return user


def set_user_active(session: Session, user: User, *, active: bool, actor: User) -> User:
    if user.id == actor.id and not active:
        raise ConflictError("You cannot deactivate your own account")
    user.is_active = active
    if not active:
        revoke_all_user_sessions(session, user)
    audit.record_audit_event(
        session,
        event=audit.USER_ACTIVE_STATE_CHANGED,
        category=audit.CATEGORY_USER,
        actor=actor,
        target_type="user",
        target_id=user.id,
        details={"active": active},
        commit=False,
    )
    session.commit()
    return user


def unlock_user(session: Session, user: User, *, actor: User) -> User:
    user.locked_until = None
    user.failed_login_count = 0
    audit.record_audit_event(
        session,
        event=audit.USER_UNLOCKED,
        category=audit.CATEGORY_USER,
        actor=actor,
        target_type="user",
        target_id=user.id,
        commit=False,
    )
    session.commit()
    return user


def delete_user(session: Session, user: User, *, actor: User) -> None:
    if user.id == actor.id:
        raise ConflictError("You cannot delete your own account")
    if count_admin_users(session) <= 1 and user_is_admin(session, user):
        raise ConflictError("Refusing: this would leave the system without an administrator")
    session.delete(user)
    audit.record_audit_event(
        session,
        event=audit.USER_DELETED,
        category=audit.CATEGORY_USER,
        actor=actor,
        target_type="user",
        target_id=user.id,
        details={"username": user.username},
        commit=False,
    )
    session.commit()


def user_is_admin(session: Session, user: User) -> bool:
    """True when the user's effective permissions include ``users:write``."""
    count = int(
        session.execute(
            text(
                """
                SELECT COUNT(*) FROM (
                  SELECT ur.user_id AS uid FROM user_roles ur
                    JOIN role_permissions rp ON rp.role_id = ur.role_id
                   WHERE rp.permission = :perm AND ur.user_id = :uid
                  UNION
                  SELECT ug.user_id AS uid FROM user_groups ug
                    JOIN group_roles gr ON gr.group_id = ug.group_id
                    JOIN role_permissions rp ON rp.role_id = gr.role_id
                   WHERE rp.permission = :perm AND ug.user_id = :uid
                )
                """
            ),
            {"perm": ADMIN_PERMISSION, "uid": user.id},
        ).scalar_one()
    )
    return count > 0


def assign_user_roles(session: Session, user: User, *, role_names: list[str], actor: User) -> User:
    """Replace the user's direct roles (their group roles are unchanged)."""
    names = [r.strip().lower() for r in role_names]
    self_last_admin = (
        user.id == actor.id
        and "admin" not in names
        and user_is_admin(session, actor)
        and count_admin_users(session) <= 1
    )
    if self_last_admin:
        raise ConflictError("Refusing: this would leave the system without an administrator")
    resolved = resolve_role_names(session, names)
    user.roles = resolved
    session.flush()
    if count_admin_users(session) == 0:
        raise ConflictError("Refusing: this would leave the system without an administrator")
    audit.record_audit_event(
        session,
        event=audit.USER_ROLES_CHANGED,
        category=audit.CATEGORY_ROLE,
        actor=actor,
        target_type="user",
        target_id=user.id,
        details={"roles": sorted(r.name for r in resolved)},
        commit=False,
    )
    session.commit()
    return user


def change_password(
    session: Session, user: User, *, current_password: str, new_password: str, settings: Settings
) -> User:
    if not verify_password(current_password, user.password_hash, settings):
        raise BadRequestError("Current password is incorrect")
    try:
        validate_password_policy(new_password, username=user.username, settings=settings)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    user.password_hash = hash_password(new_password, settings)
    audit.record_audit_event(
        session,
        event=audit.USER_PASSWORD_CHANGED_SELF,
        category=audit.CATEGORY_USER,
        actor=user,
        target_type="user",
        target_id=user.id,
        commit=False,
    )
    session.commit()
    return user


def reset_password(
    session: Session, user: User, *, new_password: str, settings: Settings, actor: User
) -> User:
    try:
        validate_password_policy(new_password, username=user.username, settings=settings)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    user.password_hash = hash_password(new_password, settings)
    revoke_all_user_sessions(session, user)
    audit.record_audit_event(
        session,
        event=audit.USER_PASSWORD_RESET,
        category=audit.CATEGORY_USER,
        actor=actor,
        target_type="user",
        target_id=user.id,
        commit=False,
    )
    session.commit()
    return user
    return user
